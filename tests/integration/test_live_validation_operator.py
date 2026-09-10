"""Integration coverage for the isolated validation operator.

The operator uses the real OpenAI adapter with an in-process HTTP transport.
No network transport or real provider credential is used.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.config import Settings
from app.core.enums import (
    BillingAccountStatus,
    BillingSource,
    LLMProvider,
    PromptRunStatus,
    ProviderSurface,
    QuotaReservationStatus,
    ScanStatus,
    WorkspaceRole,
    WorkspaceType,
)
from app.models import (
    BillingAccount,
    PlanDefinition,
    PlanProvider,
    Project,
    Prompt,
    PromptRun,
    PromptSet,
    ProviderPriceRule,
    QuotaReservation,
    Scan,
    UsageEvent,
    User,
    Workspace,
    WorkspaceMember,
    WorkspaceUsagePeriod,
)
from app.providers.evidence import EVIDENCE_BUNDLE_NAMES, validate_evidence_bundle
from app.providers.openai_adapter import OpenAIProviderAdapter
from app.providers.registry import ProviderRegistry
from app.services.live_accounting_validation_operator import (
    LIVE_PROVIDER_CALL_ACK,
    LIVE_VALIDATION_GENERATOR_KEY,
    WHITEBOARDMAKER_WORKSPACE_ID,
    LiveAccountingValidationOperator,
    LiveValidationError,
    LiveValidationRequest,
)

pytestmark = pytest.mark.integration


def _factory(db: Session) -> sessionmaker[Session]:
    return sessionmaker(
        bind=db.get_bind(),
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )


def _settings(*, model: str = "gpt-test", max_tool_calls: int = 3) -> Settings:
    return Settings(
        app_env="test",
        openai_api_key=SecretStr("offline-test-key"),
        openai_scan_model=model,
        openai_base_url="https://api.openai.com/v1",
        openai_web_search_max_tool_calls=max_tool_calls,
        pricing_require_rule_for_execution=True,
    )


def _response_payload(*, model: str = "gpt-test", mixed_actions: bool = False) -> dict[str, object]:
    actions: list[dict[str, object]] = [
        {"type": "web_search_call", "action": {"type": "search"}},
        {"type": "message", "content": [{"type": "output_text", "text": "The answer."}]},
        {"type": "web_search_call", "action": {"type": "open_page"}},
    ]
    if mixed_actions:
        actions.append({"type": "web_search_call", "action": {"type": "find_in_page"}})
    return {
        "id": "resp_operator",
        "model": model,
        "output_text": "A deterministic offline provider response.",
        "output": actions,
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    }


def _operator(
    db: Session,
    payload: dict[str, object],
    *,
    model: str = "gpt-test",
    max_tool_calls: int = 3,
    registry: ProviderRegistry | None = None,
) -> tuple[LiveAccountingValidationOperator, list[httpx.Request]]:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json=payload,
            headers={"x-request-id": "offline-request-1"},
        )

    settings = _settings(model=model, max_tool_calls=max_tool_calls)
    adapter = OpenAIProviderAdapter(
        settings=settings,
        transport=httpx.MockTransport(handler),
    )
    return (
        LiveAccountingValidationOperator(
            _factory(db),
            settings=settings,
            registry=registry or ProviderRegistry({LLMProvider.OPENAI: adapter}),
        ),
        calls,
    )


def _setup_entitled_workspace(db: Session) -> tuple[Workspace, str]:
    workspace = Workspace(name="Live validation test", workspace_type=WorkspaceType.AGENCY)
    model = f"gpt-test-{uuid.uuid4().hex[:12]}"
    user = User(email=f"live-validation-{uuid.uuid4().hex}@example.test", password_hash="hash")
    db.add_all([workspace, user])
    db.flush()
    db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role=WorkspaceRole.OWNER))
    plan = PlanDefinition(
        code=f"LIVE_TEST_{uuid.uuid4().hex[:12]}",
        name="Live validation test plan",
        is_active=True,
        max_projects=3,
        max_keywords_per_project=5,
        max_competitors_per_project=0,
        max_team_members=3,
        monthly_ai_checks=5,
    )
    db.add(plan)
    db.flush()
    db.add(PlanProvider(plan_id=plan.id, provider=LLMProvider.OPENAI))
    db.add(
        BillingAccount(
            workspace_id=workspace.id,
            source=BillingSource.ADMIN,
            status=BillingAccountStatus.ACTIVE,
            plan_code=plan.code,
            is_primary=True,
        )
    )
    now = datetime.now(UTC)
    db.add(
        ProviderPriceRule(
            pricing_key=f"live-test:{uuid.uuid4().hex}",
            provider=LLMProvider.OPENAI,
            provider_surface=ProviderSurface.OPENAI_RESPONSES_API,
            model=model,
            effective_from=now - timedelta(days=1),
            effective_to=now + timedelta(days=1),
            input_per_million_usd=Decimal("1"),
            cached_input_per_million_usd=None,
            cache_write_per_million_usd=None,
            output_per_million_usd=Decimal("2"),
            reasoning_per_million_usd=None,
            citation_per_million_usd=None,
            search_per_1000_usd=Decimal("3"),
            request_fee_usd=Decimal("0.01"),
            input_tokens_include_cached=False,
            output_tokens_include_reasoning=False,
            verified_at=now,
            source_url="https://example.test/pricing",
        )
    )
    db.commit()
    return workspace, model


def _request(
    workspace_id: uuid.UUID, prompt_path: Path, evidence_dir: Path, validation_id: str
) -> LiveValidationRequest:
    return LiveValidationRequest(
        workspace_id=workspace_id,
        validation_id=validation_id,
        prompt_file=prompt_path,
        evidence_dir=evidence_dir,
    )


def _write_prompt(
    tmp_path: Path, text: str = "Use web search and answer with cited sources."
) -> Path:
    path = tmp_path / "prompt.txt"
    path.write_bytes(text.encode("utf-8"))
    return path


def test_operator_uses_canonical_adapter_and_executes_once(
    db_session: Session, tmp_path: Path
) -> None:
    workspace, model = _setup_entitled_workspace(db_session)
    prompt_path = _write_prompt(tmp_path)
    operator, calls = _operator(db_session, _response_payload(model=model), model=model)
    request = _request(workspace.id, prompt_path, tmp_path / "evidence", "offline-once")
    prompt = operator.load_prompt_file(prompt_path)

    report = operator.execute(request, prompt, acknowledgement=LIVE_PROVIDER_CALL_ACK)

    assert report.outcome == "LIVE VALIDATION PASS"
    assert report.provider_calls == 1
    assert report.celery_dispatches == 0
    assert len(calls) == 1
    assert report.web_tool_call_count == 2
    assert report.search_action_count == 1
    assert report.open_page_action_count == 1
    assert report.unknown_web_action_count == 0
    assert report.evidence_dir.is_dir()
    manifest = json.loads((report.evidence_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["scan_id"] == str(report.scan_id)
    assert manifest["prompt_run_id"] == str(report.prompt_run_id)
    assert manifest["prompt_sha256"] == prompt.sha256
    assert manifest["request_sha256"]
    assert manifest["response_json_sha256"]
    assert manifest["response_transport_sha256"]
    assert len(manifest["artifacts"]) == 4
    assert (report.evidence_dir / "manifest.sha256").is_file()

    with _factory(db_session)() as check:
        scan = check.get(Scan, report.scan_id)
        runs = list(check.scalars(select(PromptRun).where(PromptRun.scan_id == report.scan_id)))
        assert scan is not None
        assert scan.prompt_count == 1
        assert scan.provider_count == 1
        assert scan.planned_ai_checks == 1
        assert len(runs) == 1
        assert runs[0].usage_event_id is not None
        assert check.scalar(select(func.count(Scan.id)).where(Scan.id == report.scan_id)) == 1
        prompt_set = check.get(type(scan.prompt_set), scan.prompt_set_id)
        assert prompt_set is not None
        assert prompt_set.generator_key == LIVE_VALIDATION_GENERATOR_KEY
        reservation = check.get(QuotaReservation, scan.quota_reservation_id)
        assert reservation is not None
        assert reservation.ai_checks_reserved == 1
        assert reservation.ai_checks_committed == 1
        usage_event = check.get(UsageEvent, runs[0].usage_event_id)
        assert usage_event is not None
        assert usage_event.web_tool_call_count == 2
        assert usage_event.search_action_count == 1

    reused = operator.execute(request, prompt, acknowledgement=LIVE_PROVIDER_CALL_ACK)
    assert reused.mode == "IDEMPOTENT_REUSE"
    assert reused.provider_calls == 0
    assert reused.outcome == "LIVE VALIDATION PASS"
    assert reused.evidence_dir == report.evidence_dir
    assert len(reused.evidence_artifacts) == 6
    assert len(calls) == 1


def test_operator_plan_only_has_no_durable_or_provider_side_effect(
    db_session: Session, tmp_path: Path
) -> None:
    workspace, model = _setup_entitled_workspace(db_session)
    prompt_path = _write_prompt(tmp_path)
    operator, calls = _operator(db_session, _response_payload(model=model), model=model)
    request = _request(workspace.id, prompt_path, tmp_path / "evidence", "plan-only")
    before = {
        model: db_session.scalar(select(func.count()).select_from(model))
        for model in (Scan, PromptRun, UsageEvent, QuotaReservation)
    }

    plan = operator.plan(request, operator.load_prompt_file(prompt_path))

    assert plan.provider == LLMProvider.OPENAI
    assert plan.as_dict()["provider_calls"] == 0
    assert calls == []
    assert not request.evidence_dir.exists()
    with _factory(db_session)() as check:
        after = {
            model: check.scalar(select(func.count()).select_from(model))
            for model in (Scan, PromptRun, UsageEvent, QuotaReservation)
        }
    assert after == before


def test_operator_rejects_ack_denylist_and_non_openai_before_side_effects(
    db_session: Session, tmp_path: Path
) -> None:
    prompt_path = _write_prompt(tmp_path)
    operator, calls = _operator(db_session, _response_payload())
    prompt = operator.load_prompt_file(prompt_path)

    with pytest.raises(LiveValidationError, match="exact paid"):
        operator.execute(
            _request(uuid.uuid4(), prompt_path, tmp_path / "ack", "ack-check"),
            prompt,
            acknowledgement="wrong acknowledgement",
        )

    with pytest.raises(LiveValidationError, match="protected"):
        operator.execute(
            _request(WHITEBOARDMAKER_WORKSPACE_ID, prompt_path, tmp_path / "deny", "deny-check"),
            prompt,
            acknowledgement=LIVE_PROVIDER_CALL_ACK,
        )

    non_openai = LiveValidationRequest(
        workspace_id=uuid.uuid4(),
        validation_id="provider-check",
        prompt_file=prompt_path,
        evidence_dir=tmp_path / "provider",
        provider=LLMProvider.ANTHROPIC,
    )
    with pytest.raises(LiveValidationError, match="Only OPENAI"):
        operator.execute(non_openai, prompt, acknowledgement=LIVE_PROVIDER_CALL_ACK)

    assert calls == []


def test_operator_blocks_ambiguous_running_validation_without_provider_call(
    db_session: Session, tmp_path: Path
) -> None:
    workspace, model = _setup_entitled_workspace(db_session)
    prompt_path = _write_prompt(tmp_path)
    operator, calls = _operator(db_session, _response_payload(model=model), model=model)
    request = _request(workspace.id, prompt_path, tmp_path / "evidence", "running-check")
    prompt = operator.load_prompt_file(prompt_path)
    target = operator._target(LLMProvider.OPENAI)
    pricing_rule_id = operator._read_only_preflight(request, target)
    scan_id = operator._create_durable_plan(request, prompt, target, pricing_rule_id)
    operator._reserve_exactly_one(scan_id, request)

    with _factory(db_session)() as check:
        scan = check.get(Scan, scan_id)
        run = check.scalar(select(PromptRun).where(PromptRun.scan_id == scan_id))
        assert scan is not None and run is not None
        scan.status = ScanStatus.RUNNING
        run.status = PromptRunStatus.RUNNING
        check.commit()

    with pytest.raises(LiveValidationError, match="ambiguous RUNNING"):
        operator.execute(request, prompt, acknowledgement=LIVE_PROVIDER_CALL_ACK)
    assert calls == []


def test_operator_reports_evidence_failure_without_retry(
    db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, model = _setup_entitled_workspace(db_session)
    prompt_path = _write_prompt(tmp_path)
    operator, calls = _operator(db_session, _response_payload(model=model), model=model)
    request = _request(workspace.id, prompt_path, tmp_path / "evidence", "evidence-failure")
    prompt = operator.load_prompt_file(prompt_path)

    from app.providers.evidence import FilesystemProviderEvidenceSink

    original = FilesystemProviderEvidenceSink._atomic_write_bytes

    def fail_response(
        sink: FilesystemProviderEvidenceSink,
        path: Path,
        payload: bytes,
        *,
        replace: bool = False,
    ) -> None:
        if path.name == "response.json":
            raise OSError("synthetic evidence failure")
        original(sink, path, payload, replace=replace)

    monkeypatch.setattr(FilesystemProviderEvidenceSink, "_atomic_write_bytes", fail_response)
    report = operator.execute(request, prompt, acknowledgement=LIVE_PROVIDER_CALL_ACK)

    assert len(calls) == 1
    assert report.outcome == "LIVE VALIDATION INCONCLUSIVE — EVIDENCE PERSISTENCE FAILURE"
    assert report.evidence_error is not None
    reused = operator.execute(request, prompt, acknowledgement=LIVE_PROVIDER_CALL_ACK)
    assert reused.provider_calls == 0
    assert reused.outcome == "LIVE VALIDATION INCONCLUSIVE — EVIDENCE PERSISTENCE FAILURE"
    assert len(calls) == 1
    with _factory(db_session)() as check:
        usage_event_count = check.scalar(select(func.count(UsageEvent.id)))
        assert usage_event_count is not None and usage_event_count >= 1


def test_operator_rejects_web_action_tamper_without_provider_rerun(
    db_session: Session, tmp_path: Path
) -> None:
    workspace, model = _setup_entitled_workspace(db_session)
    prompt_path = _write_prompt(tmp_path)
    operator, calls = _operator(db_session, _response_payload(model=model), model=model)
    request = _request(workspace.id, prompt_path, tmp_path / "evidence", "tamper-actions")
    prompt = operator.load_prompt_file(prompt_path)

    report = operator.execute(request, prompt, acknowledgement=LIVE_PROVIDER_CALL_ACK)
    action_path = report.evidence_dir / "web_actions.json"
    action_path.write_bytes(action_path.read_bytes() + b"\n")

    reused = operator.execute(request, prompt, acknowledgement=LIVE_PROVIDER_CALL_ACK)
    assert len(calls) == 1
    assert reused.provider_calls == 0
    assert reused.outcome == "LIVE VALIDATION INCONCLUSIVE — EVIDENCE PERSISTENCE FAILURE"


def test_operator_rejects_manifest_tamper_without_provider_rerun(
    db_session: Session, tmp_path: Path
) -> None:
    workspace, model = _setup_entitled_workspace(db_session)
    prompt_path = _write_prompt(tmp_path)
    operator, calls = _operator(db_session, _response_payload(model=model), model=model)
    request = _request(workspace.id, prompt_path, tmp_path / "evidence", "tamper-manifest")
    prompt = operator.load_prompt_file(prompt_path)

    report = operator.execute(request, prompt, acknowledgement=LIVE_PROVIDER_CALL_ACK)
    manifest_path = report.evidence_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["tampered"] = True
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True, separators=(",", ": ")),
        encoding="utf-8",
    )

    reused = operator.execute(request, prompt, acknowledgement=LIVE_PROVIDER_CALL_ACK)
    assert len(calls) == 1
    assert reused.provider_calls == 0
    assert reused.outcome == "LIVE VALIDATION INCONCLUSIVE — EVIDENCE PERSISTENCE FAILURE"


def test_operator_rejects_different_prompt_for_existing_validation_id(
    db_session: Session, tmp_path: Path
) -> None:
    workspace, model = _setup_entitled_workspace(db_session)
    first_prompt_path = _write_prompt(tmp_path, text="First exact validation prompt.")
    second_dir = tmp_path / "second"
    second_dir.mkdir()
    second_prompt_path = _write_prompt(second_dir, text="Different validation prompt.")
    operator, calls = _operator(db_session, _response_payload(model=model), model=model)
    first_request = _request(
        workspace.id, first_prompt_path, tmp_path / "evidence", "collision-check"
    )
    first_prompt = operator.load_prompt_file(first_prompt_path)
    operator.execute(first_request, first_prompt, acknowledgement=LIVE_PROVIDER_CALL_ACK)

    second_request = _request(
        workspace.id, second_prompt_path, tmp_path / "evidence", "collision-check"
    )
    second_prompt = operator.load_prompt_file(second_prompt_path)
    with pytest.raises(LiveValidationError, match="different prompt"):
        operator.execute(second_request, second_prompt, acknowledgement=LIVE_PROVIDER_CALL_ACK)
    assert len(calls) == 1


def test_operator_concurrent_same_validation_id_has_one_paid_execution(
    prepared_test_db: str, tmp_path: Path
) -> None:
    """Two PostgreSQL sessions contend on one logical validation operation."""

    engine = create_engine(prepared_test_db, poolclass=NullPool, future=True)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    setup = factory()
    try:
        workspace, model = _setup_entitled_workspace(setup)
        workspace_id = workspace.id
    finally:
        setup.close()

    prompt_path = _write_prompt(tmp_path)
    evidence_dir = tmp_path / "concurrent-evidence"
    validation_id = f"concurrent-{uuid.uuid4().hex[:12]}"
    calls: list[httpx.Request] = []
    calls_lock = threading.Lock()
    start_barrier = threading.Barrier(2)

    def handler(request: httpx.Request) -> httpx.Response:
        with calls_lock:
            calls.append(request)
        time.sleep(0.05)
        return httpx.Response(
            200,
            json=_response_payload(model=model),
            headers={"x-request-id": "concurrent-offline-request"},
        )

    def make_operator() -> LiveAccountingValidationOperator:
        settings = _settings(model=model)
        adapter = OpenAIProviderAdapter(
            settings=settings,
            transport=httpx.MockTransport(handler),
        )
        return LiveAccountingValidationOperator(
            factory,
            settings=settings,
            registry=ProviderRegistry({LLMProvider.OPENAI: adapter}),
        )

    requests = [
        _request(workspace_id, prompt_path, evidence_dir, validation_id),
        _request(workspace_id, prompt_path, evidence_dir, validation_id),
    ]
    operators = [make_operator(), make_operator()]
    prompt = LiveAccountingValidationOperator.load_prompt_file(prompt_path)
    results: list[tuple[str, object]] = []
    results_lock = threading.Lock()

    def attempt(index: int) -> None:
        try:
            start_barrier.wait(timeout=30)
            result = operators[index].execute(
                requests[index], prompt, acknowledgement=LIVE_PROVIDER_CALL_ACK
            )
            with results_lock:
                results.append(("report", result))
        except BaseException as exc:
            with results_lock:
                results.append(("error", exc))

    threads = [threading.Thread(target=attempt, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    engine.dispose()

    assert all(not thread.is_alive() for thread in threads), "concurrent attempt hung"
    assert len(results) == 2
    assert len(calls) == 1, f"Expected one provider call, got {len(calls)}"
    errors = [value for kind, value in results if kind == "error"]
    assert len(errors) == 1
    loser = errors[0]
    assert isinstance(loser, LiveValidationError)
    assert loser.code == "validation_evidence_collision"

    verify_engine = create_engine(prepared_test_db, poolclass=NullPool, future=True)
    verify = sessionmaker(bind=verify_engine, expire_on_commit=False)()
    try:
        scan_key = f"{LIVE_VALIDATION_GENERATOR_KEY}:{validation_id}"
        scans = list(
            verify.scalars(
                select(Scan).where(
                    Scan.workspace_id == workspace_id,
                    Scan.idempotency_key == scan_key,
                )
            )
        )
        assert len(scans) == 1
        scan = scans[0]
        projects = list(verify.scalars(select(Project).where(Project.workspace_id == workspace_id)))
        assert len(projects) == 1
        prompt_sets = list(
            verify.scalars(select(PromptSet).where(PromptSet.project_id == projects[0].id))
        )
        assert len(prompt_sets) == 1
        prompts = list(
            verify.scalars(select(Prompt).where(Prompt.prompt_set_id == prompt_sets[0].id))
        )
        assert len(prompts) == 1
        runs = list(verify.scalars(select(PromptRun).where(PromptRun.scan_id == scan.id)))
        assert len(runs) == 1
        reservations = list(
            verify.scalars(
                select(QuotaReservation).where(
                    QuotaReservation.workspace_id == workspace_id,
                    QuotaReservation.project_id == projects[0].id,
                )
            )
        )
        assert len(reservations) == 1
        reservation = reservations[0]
        assert reservation.status == QuotaReservationStatus.COMMITTED
        assert reservation.ai_checks_reserved == 1
        assert reservation.ai_checks_committed == 1
        events = list(
            verify.scalars(
                select(UsageEvent).where(
                    UsageEvent.workspace_id == workspace_id,
                    UsageEvent.prompt_run_id == runs[0].id,
                )
            )
        )
        assert len(events) == 1
        assert events[0].ai_checks == 1
        period = verify.scalar(
            select(WorkspaceUsagePeriod).where(WorkspaceUsagePeriod.workspace_id == workspace_id)
        )
        assert period is not None
        assert period.ai_checks_used == 1
        assert period.ai_checks_reserved == 0
    finally:
        verify.close()
        verify_engine.dispose()


def test_operator_persists_sanitized_response_and_validates_complete_bundle(
    db_session: Session, tmp_path: Path
) -> None:
    workspace, model = _setup_entitled_workspace(db_session)
    prompt_path = _write_prompt(tmp_path)
    payload = _response_payload(model=model)
    payload["nested_secret_data"] = {
        "Authorization": "Bearer TEST_SECRET",
        "openai_api_key": "sk-proj-TEST_SECRET_VALUE",
        "items": [{"secret": "TEST_SECRET"}],
    }
    operator, calls = _operator(db_session, payload, model=model)
    request = _request(workspace.id, prompt_path, tmp_path / "evidence", "response-redaction")
    prompt = operator.load_prompt_file(prompt_path)

    report = operator.execute(request, prompt, acknowledgement=LIVE_PROVIDER_CALL_ACK)
    assert len(calls) == 1
    assert (
        validate_evidence_bundle(
            report.evidence_dir,
            expected_fields={
                "validation_id": request.validation_id,
                "workspace_id": str(workspace.id),
                "scan_id": str(report.scan_id),
                "prompt_run_id": str(report.prompt_run_id),
            },
        )
        is None
    )
    for name in EVIDENCE_BUNDLE_NAMES:
        content = (report.evidence_dir / name).read_bytes()
        assert b"TEST_SECRET" not in content
        assert b"sk-proj-TEST_SECRET_VALUE" not in content


def test_operator_rejects_non_instrumentable_injected_provider(
    db_session: Session, tmp_path: Path
) -> None:
    class OfflineOnlyProvider:
        pass

    operator = LiveAccountingValidationOperator(
        _factory(db_session),
        settings=_settings(),
        registry=ProviderRegistry(  # type: ignore[arg-type,dict-item]
            {LLMProvider.OPENAI: OfflineOnlyProvider()}
        ),
    )
    prompt_path = _write_prompt(tmp_path)
    prompt = operator.load_prompt_file(prompt_path)
    request = _request(uuid.uuid4(), prompt_path, tmp_path / "evidence", "registry-check")

    with pytest.raises(LiveValidationError, match="instrumentable"):
        operator.execute(request, prompt, acknowledgement=LIVE_PROVIDER_CALL_ACK)


def test_operator_preserves_mixed_known_action_counters(
    db_session: Session, tmp_path: Path
) -> None:
    workspace, model = _setup_entitled_workspace(db_session)
    prompt_path = _write_prompt(tmp_path)
    operator, calls = _operator(
        db_session, _response_payload(model=model, mixed_actions=True), model=model
    )
    request = _request(workspace.id, prompt_path, tmp_path / "evidence", "mixed-actions")
    prompt = operator.load_prompt_file(prompt_path)

    report = operator.execute(request, prompt, acknowledgement=LIVE_PROVIDER_CALL_ACK)

    assert report.outcome == "LIVE VALIDATION PASS"
    assert len(calls) == 1
    assert report.web_tool_call_count == 3
    assert report.search_action_count == 1
    assert report.open_page_action_count == 1
    assert report.find_in_page_action_count == 1
    assert report.unknown_web_action_count == 0
