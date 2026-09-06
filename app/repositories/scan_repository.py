"""Persistence and row-locking for scans and prompt-run evidence."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.core.enums import PromptRunStatus, ProviderErrorCode, ScanStatus
from app.core.exceptions import ConflictError
from app.core.logging import get_logger
from app.models.scan import PromptRun, ResponseSource, Scan

logger = get_logger("app.scan_repository")


class ScanRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, scan: Scan) -> Scan:
        self._session.add(scan)
        self._session.flush()
        return scan

    def get_by_id(self, scan_id: uuid.UUID) -> Scan | None:
        return self._session.get(Scan, scan_id)

    def get_for_update(self, scan_id: uuid.UUID) -> Scan | None:
        return self._session.execute(
            select(Scan)
            .where(Scan.id == scan_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()

    def get_by_idempotency_key(self, workspace_id: uuid.UUID, idempotency_key: str) -> Scan | None:
        return self._session.execute(
            select(Scan).where(
                Scan.workspace_id == workspace_id,
                Scan.idempotency_key == idempotency_key,
            )
        ).scalar_one_or_none()

    def get_scoped(
        self, workspace_id: uuid.UUID, project_id: uuid.UUID, scan_id: uuid.UUID
    ) -> Scan | None:
        return self._session.execute(
            select(Scan).where(
                Scan.id == scan_id,
                Scan.workspace_id == workspace_id,
                Scan.project_id == project_id,
            )
        ).scalar_one_or_none()

    def list_scoped(
        self, workspace_id: uuid.UUID, project_id: uuid.UUID, offset: int, limit: int
    ) -> list[Scan]:
        return list(
            self._session.execute(
                select(Scan)
                .where(Scan.workspace_id == workspace_id, Scan.project_id == project_id)
                .order_by(Scan.created_at.desc(), Scan.id)
                .offset(offset)
                .limit(limit)
            ).scalars()
        )

    def list_stale_running(self, before: datetime) -> list[Scan]:
        return list(
            self._session.execute(
                select(Scan).where(
                    Scan.status == ScanStatus.RUNNING,
                    Scan.started_at.is_not(None),
                    Scan.started_at < before,
                )
            ).scalars()
        )

    def list_stale_pending(self, before: datetime) -> list[Scan]:
        """Return PENDING scans whose ``created_at`` predates ``before``.

        A PENDING scan older than the stale threshold was either never
        dispatched (broker/task lost under early acknowledgement) or
        dispatched but never claimed by a worker. Recovery may safely
        fail it without replaying providers.
        """
        return list(
            self._session.execute(
                select(Scan).where(
                    Scan.status == ScanStatus.PENDING,
                    Scan.created_at < before,
                )
            ).scalars()
        )


class PromptRunRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create_batch(self, runs: list[PromptRun]) -> list[PromptRun]:
        self._session.add_all(runs)
        self._session.flush()
        return runs

    def get_by_id(self, run_id: uuid.UUID) -> PromptRun | None:
        return self._session.get(PromptRun, run_id)

    def get_for_update(self, run_id: uuid.UUID) -> PromptRun | None:
        return self._session.execute(
            select(PromptRun)
            .where(PromptRun.id == run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()

    def get_scoped(
        self,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        scan_id: uuid.UUID,
        run_id: uuid.UUID,
        include_sources: bool = False,
    ) -> PromptRun | None:
        statement = (
            select(PromptRun)
            .join(Scan, PromptRun.scan_id == Scan.id)
            .where(
                PromptRun.id == run_id,
                PromptRun.scan_id == scan_id,
                Scan.workspace_id == workspace_id,
                Scan.project_id == project_id,
            )
        )
        if include_sources:
            statement = statement.options(selectinload(PromptRun.sources))
        return self._session.execute(statement).scalar_one_or_none()

    def list_by_scan(self, scan_id: uuid.UUID, include_sources: bool = False) -> list[PromptRun]:
        statement = (
            select(PromptRun)
            .where(PromptRun.scan_id == scan_id)
            .order_by(PromptRun.created_at, PromptRun.id)
        )
        if include_sources:
            statement = statement.options(selectinload(PromptRun.sources))
        return list(self._session.execute(statement).scalars())

    def list_ids_by_scan(self, scan_id: uuid.UUID) -> list[uuid.UUID]:
        return list(
            self._session.execute(
                select(PromptRun.id)
                .where(PromptRun.scan_id == scan_id)
                .order_by(PromptRun.created_at, PromptRun.id)
            ).scalars()
        )

    def terminal_counts(self, scan_id: uuid.UUID) -> tuple[int, int, int]:
        rows = self._session.execute(
            select(PromptRun.status, func.count(PromptRun.id))
            .where(PromptRun.scan_id == scan_id)
            .group_by(PromptRun.status)
        ).all()
        counts = {status: int(count) for status, count in rows}
        succeeded = counts.get(PromptRunStatus.SUCCEEDED, 0)
        failed = counts.get(PromptRunStatus.FAILED, 0)
        pending = counts.get(PromptRunStatus.PENDING, 0) + counts.get(PromptRunStatus.RUNNING, 0)
        return succeeded, failed, pending

    def count_by_scan(self, scan_id: uuid.UUID) -> int:
        return int(
            self._session.execute(
                select(func.count(PromptRun.id)).where(PromptRun.scan_id == scan_id)
            ).scalar_one()
        )

    def mark_unresolved_failed(
        self,
        scan_id: uuid.UUID,
        completed_at: datetime,
        error_message: str,
        error_code: ProviderErrorCode | None = None,
    ) -> int:
        runs = list(
            self._session.execute(
                select(PromptRun)
                .where(
                    PromptRun.scan_id == scan_id,
                    PromptRun.status.in_([PromptRunStatus.PENDING, PromptRunStatus.RUNNING]),
                )
                .with_for_update()
            ).scalars()
        )
        for run in runs:
            run.status = PromptRunStatus.FAILED
            if error_code is not None:
                run.error_code = error_code
            run.error_message = error_message
            run.completed_at = completed_at
        return len(runs)


class ResponseSourceRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create_batch(self, sources: list[ResponseSource]) -> list[ResponseSource]:
        self._session.add_all(sources)
        self._session.flush()
        return sources

    def create_batch_idempotent(self, sources: list[ResponseSource]) -> list[ResponseSource]:
        """Persist ResponseSource rows idempotently.

        For each (prompt_run_id, ordinal):
        - If no existing row: create it.
        - If existing row with same URL: no-op (same evidence).
        - If existing row with different URL: raise ConflictError
          (evidence conflict — fail-closed, do NOT silently overwrite).

        This makes citation persistence safe for future reconciliation
        over incident state where citations may already exist.
        """
        if not sources:
            return []

        # Collect all (prompt_run_id, ordinal) pairs to check.
        run_ids = {s.prompt_run_id for s in sources}
        existing: dict[tuple[uuid.UUID, int], ResponseSource] = {}
        for run_id in run_ids:
            rows = (
                self._session.execute(
                    select(ResponseSource).where(ResponseSource.prompt_run_id == run_id)
                )
                .scalars()
                .all()
            )
            for row in rows:
                existing[(row.prompt_run_id, row.ordinal)] = row

        new_sources: list[ResponseSource] = []
        for src in sources:
            key = (src.prompt_run_id, src.ordinal)
            if key not in existing:
                new_sources.append(src)
            else:
                existing_row = existing[key]
                # URL is the minimum identity check.
                if (existing_row.url or None) != (src.url or None):
                    logger.warning(
                        "citation_ordinal_conflict",
                        prompt_run_id=str(src.prompt_run_id),
                        ordinal=src.ordinal,
                        existing_url=existing_row.url,
                        incoming_url=src.url,
                    )
                    raise ConflictError(
                        f"Citation ordinal conflict for prompt_run={src.prompt_run_id} "
                        f"ordinal={src.ordinal}: existing_url={existing_row.url!r} "
                        f"incoming_url={src.url!r}."
                    )
                # Same URL — no-op for this source.

        if new_sources:
            self._session.add_all(new_sources)
            self._session.flush()
        return new_sources
