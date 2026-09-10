"""Integration tests for the provider pricing bootstrap operator.

Tests against the real PostgreSQL test database using the same Alembic
migration path as production.  Verifies DB semantics:
- --check reports MISSING on empty DB
- --apply creates exactly one rule
- second --apply is idempotent (zero new rows)
- exact existing rule -> READY
- conflicting values -> CONFLICT / non-zero exit
- overlapping rule -> fail closed
- PricingService.resolve() returns the seeded rule
- a different model fails resolution

No provider/network calls are made anywhere in these tests.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

import pytest
from scripts.seed_provider_pricing import EXIT_FAIL, EXIT_OK, main
from sqlalchemy import bindparam, create_engine, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.core.enums import LLMProvider, ProviderSurface
from app.core.exceptions import PricingConfigurationError, PricingRuleNotFoundError
from app.models.pricing import ProviderPriceRule
from app.services.pricing_service import PricingService

_SEED_TEST_PRICING_KEYS = (
    "openai:responses:gpt-5.6-terra:2026-07-30",
    "openai:responses:gpt-5.6-terra:earlier",
    "openai:responses:gpt-5.6-terra:historical",
    "openai:responses:gpt-5.6-terra:overlapping-end",
    "openai:responses:gpt-5.6-terra:open-earlier",
)


def _new_engine(prepared_test_db: str) -> Engine:
    """Create a new engine with NullPool for isolated verification."""
    return create_engine(prepared_test_db, poolclass=NullPool)


@pytest.fixture()
def provision_factory(prepared_test_db: str) -> sessionmaker[Session]:
    """Return a session factory with its own engine/connection.

    The operator script calls session.commit(), which would commit
    the db_session fixture's transaction if sharing the same connection.
    Using a separate engine ensures the operator's commits do not
    interfere with the test transaction.
    """
    engine = _new_engine(prepared_test_db)
    factory: sessionmaker[Session] = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        class_=Session,
    )
    return factory


@pytest.fixture(autouse=True)
def _cleanup_pricing_rules(prepared_test_db: str) -> Iterator[None]:
    """Clean only rows owned by this pricing test module.

    The feature/operator tests intentionally retain committed rows so their
    independent-session behavior is real.  A global pricing delete would
    both cross test ownership boundaries and violate the PromptRun FK.
    """

    def delete_owned_rows() -> None:
        engine = _new_engine(prepared_test_db)
        with engine.connect() as conn:
            referenced = conn.execute(
                text(
                    "SELECT COUNT(*) "
                    "FROM provider_price_rules AS p "
                    "JOIN prompt_runs AS r ON r.pricing_rule_id = p.id "
                    "WHERE p.pricing_key IN :pricing_keys"
                ).bindparams(bindparam("pricing_keys", expanding=True)),
                {"pricing_keys": _SEED_TEST_PRICING_KEYS},
            ).scalar_one()
            if referenced:
                pytest.fail(
                    "Seed pricing test rows are still referenced by PromptRuns; "
                    "refusing unsafe cleanup."
                )
            conn.execute(
                text(
                    "DELETE FROM provider_price_rules WHERE pricing_key IN :pricing_keys"
                ).bindparams(bindparam("pricing_keys", expanding=True)),
                {"pricing_keys": _SEED_TEST_PRICING_KEYS},
            )
            conn.commit()
        engine.dispose()

    delete_owned_rows()
    yield
    delete_owned_rows()


class TestCheckModeIntegration:
    """--check mode against real DB."""

    def test_check_missing_on_empty_db(
        self,
        db_session: Session,
        provision_factory: sessionmaker[Session],
    ) -> None:
        """Empty DB -> --check reports MISSING."""
        with patch("app.db.session.get_session_factory", return_value=provision_factory):
            ret = main(["--check"])
        assert ret == EXIT_OK

    def test_check_ready_after_apply(
        self,
        db_session: Session,
        provision_factory: sessionmaker[Session],
    ) -> None:
        """After --apply, --check reports READY."""
        with patch("app.db.session.get_session_factory", return_value=provision_factory):
            main(["--apply"])
        with patch("app.db.session.get_session_factory", return_value=provision_factory):
            ret = main(["--check"])
        assert ret == EXIT_OK


class TestApplyModeIntegration:
    """--apply mode against real DB."""

    def test_apply_creates_exactly_one_rule(
        self,
        db_session: Session,
        provision_factory: sessionmaker[Session],
        prepared_test_db: str,
    ) -> None:
        """Empty DB -> --apply creates exactly one rule (delta = 1)."""
        engine = _new_engine(prepared_test_db)
        with engine.connect() as conn:
            count_before = conn.execute(
                text("SELECT COUNT(*) FROM provider_price_rules")
            ).scalar_one()

        with patch("app.db.session.get_session_factory", return_value=provision_factory):
            ret = main(["--apply"])
        assert ret == EXIT_OK

        with engine.connect() as conn:
            count_after = conn.execute(
                text("SELECT COUNT(*) FROM provider_price_rules")
            ).scalar_one()
        engine.dispose()
        delta = count_after - count_before
        assert delta == 1, f"Exactly one rule must be created, delta={delta}"

    def test_second_apply_is_idempotent(
        self,
        db_session: Session,
        provision_factory: sessionmaker[Session],
        prepared_test_db: str,
    ) -> None:
        """Second --apply creates zero new rows."""
        with patch("app.db.session.get_session_factory", return_value=provision_factory):
            main(["--apply"])

        engine = _new_engine(prepared_test_db)
        with engine.connect() as conn:
            count_after_first = conn.execute(
                text("SELECT COUNT(*) FROM provider_price_rules")
            ).scalar_one()

        with patch("app.db.session.get_session_factory", return_value=provision_factory):
            ret = main(["--apply"])
        assert ret == EXIT_OK

        with engine.connect() as conn:
            count_after_second = conn.execute(
                text("SELECT COUNT(*) FROM provider_price_rules")
            ).scalar_one()
        engine.dispose()
        assert count_after_second == count_after_first, "No new rows on idempotent run"

    def test_apply_fails_on_conflicting_values(
        self,
        db_session: Session,
        provision_factory: sessionmaker[Session],
        prepared_test_db: str,
    ) -> None:
        """Same pricing_key with different values -> CONFLICT / non-zero exit."""
        # First apply to create the rule.
        with patch("app.db.session.get_session_factory", return_value=provision_factory):
            main(["--apply"])

        # Manually modify the rule to create a conflict using a separate connection.
        engine = _new_engine(prepared_test_db)
        with engine.connect() as conn:
            conn.execute(
                text(
                    "UPDATE provider_price_rules "
                    "SET input_per_million_usd = 99.00 "
                    "WHERE pricing_key = 'openai:responses:gpt-5.6-terra:2026-07-30'"
                )
            )
            conn.commit()
        engine.dispose()

        # Second apply should detect the conflict and fail.
        with patch("app.db.session.get_session_factory", return_value=provision_factory):
            ret = main(["--apply"])
        assert ret == EXIT_FAIL

    def test_apply_fails_on_overlapping_rule(
        self,
        db_session: Session,
        provision_factory: sessionmaker[Session],
        prepared_test_db: str,
    ) -> None:
        """Overlapping rule for same provider/surface/model -> fail closed."""
        # Create a pre-existing overlapping rule using a separate connection
        # so it is committed and visible to the operator's session.
        engine = _new_engine(prepared_test_db)
        overlap_factory = sessionmaker(bind=engine, expire_on_commit=False)
        with overlap_factory() as osession:
            overlap = ProviderPriceRule(
                pricing_key="openai:responses:gpt-5.6-terra:earlier",
                provider=LLMProvider.OPENAI,
                provider_surface=ProviderSurface.OPENAI_RESPONSES_API,
                model="gpt-5.6-terra",
                effective_from=datetime(2026, 1, 1, tzinfo=UTC),
                effective_to=None,
                input_per_million_usd=Decimal("1.00"),
                cached_input_per_million_usd=Decimal("0.10"),
                cache_write_per_million_usd=Decimal("1.25"),
                output_per_million_usd=Decimal("6.00"),
                reasoning_per_million_usd=Decimal("6.00"),
                citation_per_million_usd=None,
                search_per_1000_usd=Decimal("5.00"),
                request_fee_usd=None,
                input_tokens_include_cached=True,
                output_tokens_include_reasoning=True,
                verified_at=datetime(2026, 1, 1, tzinfo=UTC),
                source_url="https://example.test/earlier",
                notes="Earlier overlapping rule for testing",
            )
            osession.add(overlap)
            osession.commit()
        engine.dispose()

        # --apply should detect the overlap and fail.
        with patch("app.db.session.get_session_factory", return_value=provision_factory):
            ret = main(["--apply"])
        assert ret == EXIT_FAIL

    def test_created_rule_has_correct_values(
        self,
        db_session: Session,
        provision_factory: sessionmaker[Session],
        prepared_test_db: str,
    ) -> None:
        """The created rule must have the exact pinned values."""
        with patch("app.db.session.get_session_factory", return_value=provision_factory):
            main(["--apply"])

        # Use a separate session to read committed data.
        engine = _new_engine(prepared_test_db)
        verify_factory = sessionmaker(bind=engine, expire_on_commit=False)
        with verify_factory() as vsession:
            rule = vsession.execute(
                select(ProviderPriceRule).where(
                    ProviderPriceRule.pricing_key == "openai:responses:gpt-5.6-terra:2026-07-30"
                )
            ).scalar_one()

            assert rule.provider == LLMProvider.OPENAI
            assert rule.provider_surface == ProviderSurface.OPENAI_RESPONSES_API
            assert rule.model == "gpt-5.6-terra"
            assert rule.input_per_million_usd == Decimal("2.00")
            assert rule.cached_input_per_million_usd == Decimal("0.20")
            assert rule.cache_write_per_million_usd == Decimal("2.50")
            assert rule.output_per_million_usd == Decimal("12.00")
            assert rule.reasoning_per_million_usd == Decimal("12.00")
            assert rule.citation_per_million_usd is None
            assert rule.search_per_1000_usd == Decimal("10.00")
            assert rule.request_fee_usd is None
            assert rule.input_tokens_include_cached is True
            assert rule.output_tokens_include_reasoning is True
            assert rule.effective_from == datetime(2026, 7, 30, tzinfo=UTC)
            assert rule.effective_to is None
            assert rule.verified_at == datetime(2026, 9, 5, tzinfo=UTC)
            assert "openai.com" in rule.source_url
        engine.dispose()


class TestPricingResolutionIntegration:
    """PricingService.resolve() with the seeded rule."""

    def test_resolve_returns_seeded_rule(
        self,
        db_session: Session,
        provision_factory: sessionmaker[Session],
        prepared_test_db: str,
    ) -> None:
        """PricingService.resolve() returns exactly the seeded rule."""
        with patch("app.db.session.get_session_factory", return_value=provision_factory):
            main(["--apply"])

        # Use a separate session to verify resolution.
        engine = _new_engine(prepared_test_db)
        verify_factory = sessionmaker(bind=engine, expire_on_commit=False)
        with verify_factory() as vsession:
            service = PricingService(vsession)
            rule = service.resolve(
                LLMProvider.OPENAI,
                ProviderSurface.OPENAI_RESPONSES_API,
                "gpt-5.6-terra",
                datetime(2026, 9, 5, tzinfo=UTC),
            )
            assert rule.pricing_key == "openai:responses:gpt-5.6-terra:2026-07-30"
            assert rule.model == "gpt-5.6-terra"
        engine.dispose()

    def test_resolve_different_model_fails(
        self,
        db_session: Session,
        provision_factory: sessionmaker[Session],
        prepared_test_db: str,
    ) -> None:
        """A different exact model still fails resolution."""
        with patch("app.db.session.get_session_factory", return_value=provision_factory):
            main(["--apply"])

        engine = _new_engine(prepared_test_db)
        verify_factory = sessionmaker(bind=engine, expire_on_commit=False)
        with verify_factory() as vsession:
            service = PricingService(vsession)
            with pytest.raises(PricingRuleNotFoundError):
                service.resolve(
                    LLMProvider.OPENAI,
                    ProviderSurface.OPENAI_RESPONSES_API,
                    "gpt-5.6-terra-v2",
                    datetime(2026, 9, 5, tzinfo=UTC),
                )
        engine.dispose()

    def test_resolve_before_effective_date_fails(
        self,
        db_session: Session,
        provision_factory: sessionmaker[Session],
        prepared_test_db: str,
    ) -> None:
        """Resolution before the effective_from date fails."""
        with patch("app.db.session.get_session_factory", return_value=provision_factory):
            main(["--apply"])

        engine = _new_engine(prepared_test_db)
        verify_factory = sessionmaker(bind=engine, expire_on_commit=False)
        with verify_factory() as vsession:
            service = PricingService(vsession)
            with pytest.raises(PricingRuleNotFoundError):
                service.resolve(
                    LLMProvider.OPENAI,
                    ProviderSurface.OPENAI_RESPONSES_API,
                    "gpt-5.6-terra",
                    datetime(2026, 7, 29, tzinfo=UTC),
                )
        engine.dispose()


class TestReadyOverlapConsistencyIntegration:
    """Phase 13.5.5.1 - exact pinned rule + distinct overlapping rule.

    Proves the operator and PricingService.resolve() agree: when an
    overlapping rule exists alongside the exact pinned rule, the operator
    reports CONFLICT (not READY) and resolve() raises
    PricingConfigurationError.
    """

    def _seed_pinned_rule(self, prepared_test_db: str) -> None:
        """Apply the pinned rule via the operator."""
        engine = _new_engine(prepared_test_db)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        with patch("app.db.session.get_session_factory", return_value=factory):
            main(["--apply"])
        engine.dispose()

    def _seed_overlap_rule(self, prepared_test_db: str) -> None:
        """Insert a distinct overlapping rule via a separate session."""
        engine = _new_engine(prepared_test_db)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        with factory() as osession:
            overlap = ProviderPriceRule(
                pricing_key="openai:responses:gpt-5.6-terra:earlier",
                provider=LLMProvider.OPENAI,
                provider_surface=ProviderSurface.OPENAI_RESPONSES_API,
                model="gpt-5.6-terra",
                effective_from=datetime(2026, 1, 1, tzinfo=UTC),
                effective_to=None,
                input_per_million_usd=Decimal("1.00"),
                cached_input_per_million_usd=Decimal("0.10"),
                cache_write_per_million_usd=Decimal("1.25"),
                output_per_million_usd=Decimal("6.00"),
                reasoning_per_million_usd=Decimal("6.00"),
                citation_per_million_usd=None,
                search_per_1000_usd=Decimal("5.00"),
                request_fee_usd=None,
                input_tokens_include_cached=True,
                output_tokens_include_reasoning=True,
                verified_at=datetime(2026, 1, 1, tzinfo=UTC),
                source_url="https://example.test/earlier",
                notes="Earlier overlapping rule for testing",
            )
            osession.add(overlap)
            osession.commit()
        engine.dispose()

    def test_check_conflict_on_exact_match_with_overlap(
        self,
        db_session: Session,
        provision_factory: sessionmaker[Session],
        prepared_test_db: str,
    ) -> None:
        """Exact pinned rule + overlapping rule => --check CONFLICT."""
        self._seed_pinned_rule(prepared_test_db)
        self._seed_overlap_rule(prepared_test_db)

        with patch("app.db.session.get_session_factory", return_value=provision_factory):
            ret = main(["--check"])
        assert ret == EXIT_FAIL, "Must report CONFLICT, not READY"

    def test_apply_conflict_on_exact_match_with_overlap(
        self,
        db_session: Session,
        provision_factory: sessionmaker[Session],
        prepared_test_db: str,
    ) -> None:
        """Exact pinned rule + overlapping rule => --apply CONFLICT, zero writes."""
        self._seed_pinned_rule(prepared_test_db)
        self._seed_overlap_rule(prepared_test_db)

        # Count before
        engine = _new_engine(prepared_test_db)
        with engine.connect() as conn:
            count_before = conn.execute(
                text("SELECT COUNT(*) FROM provider_price_rules")
            ).scalar_one()

        with patch("app.db.session.get_session_factory", return_value=provision_factory):
            ret = main(["--apply"])
        assert ret == EXIT_FAIL

        # Count after must be unchanged (zero writes)
        with engine.connect() as conn:
            count_after = conn.execute(
                text("SELECT COUNT(*) FROM provider_price_rules")
            ).scalar_one()
        engine.dispose()
        assert count_after == count_before, "Zero writes on conflict"

    def test_existing_rows_untouched_on_conflict(
        self,
        db_session: Session,
        provision_factory: sessionmaker[Session],
        prepared_test_db: str,
    ) -> None:
        """Existing rows must remain untouched when --apply detects conflict."""
        self._seed_pinned_rule(prepared_test_db)
        self._seed_overlap_rule(prepared_test_db)

        # Capture the pinned rule's values before --apply
        engine = _new_engine(prepared_test_db)
        verify_factory = sessionmaker(bind=engine, expire_on_commit=False)
        with verify_factory() as vsession:
            before = vsession.execute(
                select(ProviderPriceRule).where(
                    ProviderPriceRule.pricing_key == "openai:responses:gpt-5.6-terra:2026-07-30"
                )
            ).scalar_one()
            pinned_id = before.id
            pinned_input_price = before.input_per_million_usd

        with patch("app.db.session.get_session_factory", return_value=provision_factory):
            main(["--apply"])

        # Verify the pinned rule is unchanged
        with verify_factory() as vsession:
            after = vsession.execute(
                select(ProviderPriceRule).where(ProviderPriceRule.id == pinned_id)
            ).scalar_one()
            assert after.input_per_million_usd == pinned_input_price
        engine.dispose()

    def test_pricing_service_raises_on_overlap(
        self,
        db_session: Session,
        provision_factory: sessionmaker[Session],
        prepared_test_db: str,
    ) -> None:
        """PricingService.resolve() raises PricingConfigurationError on overlap.

        This proves the runtime resolver and the operator agree: the
        overlapping state is ambiguous and must not be reported as READY.
        """
        self._seed_pinned_rule(prepared_test_db)
        self._seed_overlap_rule(prepared_test_db)

        engine = _new_engine(prepared_test_db)
        verify_factory = sessionmaker(bind=engine, expire_on_commit=False)
        with verify_factory() as vsession:
            service = PricingService(vsession)
            with pytest.raises(PricingConfigurationError):
                service.resolve(
                    LLMProvider.OPENAI,
                    ProviderSurface.OPENAI_RESPONSES_API,
                    "gpt-5.6-terra",
                    datetime(2026, 9, 5, tzinfo=UTC),
                )
        engine.dispose()


class TestOverlapBoundarySemantics:
    """Phase 13.5.5.1 - half-open interval [from, to) boundary tests.

    PricingService.resolve() uses:
        effective_from <= execution_time
        AND execution_time < effective_to

    The operator's overlap detection must use the same semantics.
    """

    def _seed_pinned(self, prepared_test_db: str) -> None:
        """Apply the pinned rule via the operator."""
        engine = _new_engine(prepared_test_db)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        with patch("app.db.session.get_session_factory", return_value=factory):
            main(["--apply"])
        engine.dispose()

    def _seed_rule(
        self,
        prepared_test_db: str,
        pricing_key: str,
        effective_from: datetime,
        effective_to: datetime | None,
    ) -> None:
        """Insert a custom rule via a separate session."""
        engine = _new_engine(prepared_test_db)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        with factory() as osession:
            rule = ProviderPriceRule(
                pricing_key=pricing_key,
                provider=LLMProvider.OPENAI,
                provider_surface=ProviderSurface.OPENAI_RESPONSES_API,
                model="gpt-5.6-terra",
                effective_from=effective_from,
                effective_to=effective_to,
                input_per_million_usd=Decimal("1.00"),
                cached_input_per_million_usd=Decimal("0.10"),
                cache_write_per_million_usd=Decimal("1.25"),
                output_per_million_usd=Decimal("6.00"),
                reasoning_per_million_usd=Decimal("6.00"),
                citation_per_million_usd=None,
                search_per_1000_usd=Decimal("5.00"),
                request_fee_usd=None,
                input_tokens_include_cached=True,
                output_tokens_include_reasoning=True,
                verified_at=datetime(2026, 1, 1, tzinfo=UTC),
                source_url="https://example.test/boundary",
                notes="Boundary test rule",
            )
            osession.add(rule)
            osession.commit()
        engine.dispose()

    def test_rule_ending_exactly_at_pinned_from_is_not_overlap(
        self,
        db_session: Session,
        provision_factory: sessionmaker[Session],
        prepared_test_db: str,
    ) -> None:
        """Historical rule ending exactly at pinned.effective_from is NOT overlap.

        existing.effective_to == pinned.effective_from means the existing
        interval is [from, pinned.effective_from) and the pinned interval
        starts at pinned.effective_from. No overlap.
        """
        self._seed_pinned(prepared_test_db)
        self._seed_rule(
            prepared_test_db,
            pricing_key="openai:responses:gpt-5.6-terra:historical",
            effective_from=datetime(2026, 1, 1, tzinfo=UTC),
            effective_to=datetime(2026, 7, 30, tzinfo=UTC),  # exactly at pinned.from
        )

        with patch("app.db.session.get_session_factory", return_value=provision_factory):
            ret = main(["--check"])
        assert ret == EXIT_OK, "Rule ending at pinned.effective_from is NOT an overlap"

    def test_rule_ending_after_pinned_from_is_overlap(
        self,
        db_session: Session,
        provision_factory: sessionmaker[Session],
        prepared_test_db: str,
    ) -> None:
        """Rule ending after pinned.effective_from IS an overlap."""
        self._seed_pinned(prepared_test_db)
        self._seed_rule(
            prepared_test_db,
            pricing_key="openai:responses:gpt-5.6-terra:overlapping-end",
            effective_from=datetime(2026, 1, 1, tzinfo=UTC),
            effective_to=datetime(2026, 8, 1, tzinfo=UTC),  # after pinned.from
        )

        with patch("app.db.session.get_session_factory", return_value=provision_factory):
            ret = main(["--check"])
        assert ret == EXIT_FAIL, "Rule ending after pinned.effective_from IS an overlap"

    def test_open_ended_rule_covering_pinned_from_is_overlap(
        self,
        db_session: Session,
        provision_factory: sessionmaker[Session],
        prepared_test_db: str,
    ) -> None:
        """Open-ended rule covering pinned.effective_from IS an overlap."""
        self._seed_pinned(prepared_test_db)
        self._seed_rule(
            prepared_test_db,
            pricing_key="openai:responses:gpt-5.6-terra:open-earlier",
            effective_from=datetime(2026, 1, 1, tzinfo=UTC),
            effective_to=None,  # open-ended
        )

        with patch("app.db.session.get_session_factory", return_value=provision_factory):
            ret = main(["--check"])
        assert ret == EXIT_FAIL, "Open-ended rule covering pinned.from IS an overlap"
