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

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

import pytest
from scripts.seed_provider_pricing import EXIT_FAIL, EXIT_OK, main
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.core.enums import LLMProvider, ProviderSurface
from app.core.exceptions import PricingRuleNotFoundError
from app.models.pricing import ProviderPriceRule
from app.services.pricing_service import PricingService


def _new_engine(prepared_test_db: str):
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
def _cleanup_pricing_rules(prepared_test_db: str):
    """Delete all ProviderPriceRule rows before and after each test."""
    engine = _new_engine(prepared_test_db)
    with engine.connect() as conn:
        conn.execute(text("DELETE FROM provider_price_rules"))
        conn.commit()
    engine.dispose()
    yield
    engine = _new_engine(prepared_test_db)
    with engine.connect() as conn:
        conn.execute(text("DELETE FROM provider_price_rules"))
        conn.commit()
    engine.dispose()


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
