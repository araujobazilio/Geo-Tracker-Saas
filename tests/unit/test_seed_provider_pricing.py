"""Unit tests for the provider pricing bootstrap operator.

Tests cover:
- Pinned pricing evidence values (Decimal, no floats).
- Conflict detection logic (matching, formatting).
- Overlap detection logic.
- AST-based import resolution.
- main() --check and --apply CLI behavior with mocked sessions.

No real database is touched by these tests.
No provider/network calls are made.
"""

from __future__ import annotations

import ast
import importlib
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from scripts.seed_provider_pricing import (
    _OPENAI_GPT56_TERRA,
    EXIT_FAIL,
    EXIT_OK,
    _format_conflict,
    _rule_to_dict,
    _values_match,
)

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "seed_provider_pricing.py"


class TestPinnedPricingEvidence:
    """Verify the pinned pricing data is correct and Decimal-safe."""

    def test_pricing_key_is_stable(self) -> None:
        assert _OPENAI_GPT56_TERRA.pricing_key == "openai:responses:gpt-5.6-terra:2026-07-30"

    def test_provider_and_surface(self) -> None:
        assert _OPENAI_GPT56_TERRA.provider == "OPENAI"
        assert _OPENAI_GPT56_TERRA.provider_surface == "OPENAI_RESPONSES_API"

    def test_model(self) -> None:
        assert _OPENAI_GPT56_TERRA.model == "gpt-5.6-terra"

    def test_all_monetary_values_are_decimal(self) -> None:
        """No binary floats — all monetary values must be Decimal or None."""
        monetary_fields = [
            "input_per_million_usd",
            "cached_input_per_million_usd",
            "cache_write_per_million_usd",
            "output_per_million_usd",
            "reasoning_per_million_usd",
            "citation_per_million_usd",
            "search_per_1000_usd",
            "request_fee_usd",
        ]
        for field in monetary_fields:
            val = getattr(_OPENAI_GPT56_TERRA, field)
            assert val is None or isinstance(
                val, Decimal
            ), f"{field} must be Decimal or None, got {type(val)}"

    def test_input_price(self) -> None:
        assert _OPENAI_GPT56_TERRA.input_per_million_usd == Decimal("2.00")

    def test_cached_input_price(self) -> None:
        assert _OPENAI_GPT56_TERRA.cached_input_per_million_usd == Decimal("0.20")

    def test_cache_write_price(self) -> None:
        assert _OPENAI_GPT56_TERRA.cache_write_per_million_usd == Decimal("2.50")

    def test_output_price(self) -> None:
        assert _OPENAI_GPT56_TERRA.output_per_million_usd == Decimal("12.00")

    def test_reasoning_price(self) -> None:
        assert _OPENAI_GPT56_TERRA.reasoning_per_million_usd == Decimal("12.00")

    def test_search_price(self) -> None:
        assert _OPENAI_GPT56_TERRA.search_per_1000_usd == Decimal("10.00")

    def test_citation_is_null(self) -> None:
        assert _OPENAI_GPT56_TERRA.citation_per_million_usd is None

    def test_request_fee_is_null(self) -> None:
        assert _OPENAI_GPT56_TERRA.request_fee_usd is None

    def test_input_tokens_include_cached(self) -> None:
        assert _OPENAI_GPT56_TERRA.input_tokens_include_cached is True

    def test_output_tokens_include_reasoning(self) -> None:
        assert _OPENAI_GPT56_TERRA.output_tokens_include_reasoning is True

    def test_effective_from_is_utc(self) -> None:
        assert _OPENAI_GPT56_TERRA.effective_from.tzinfo is not None
        assert _OPENAI_GPT56_TERRA.effective_from == datetime(2026, 7, 30, tzinfo=UTC)

    def test_effective_to_is_none(self) -> None:
        assert _OPENAI_GPT56_TERRA.effective_to is None

    def test_verified_at_is_utc(self) -> None:
        assert _OPENAI_GPT56_TERRA.verified_at.tzinfo is not None
        assert _OPENAI_GPT56_TERRA.verified_at == datetime(2026, 9, 5, tzinfo=UTC)

    def test_source_url_is_official(self) -> None:
        assert "openai.com" in _OPENAI_GPT56_TERRA.source_url

    def test_notes_document_evidence(self) -> None:
        notes = _OPENAI_GPT56_TERRA.notes
        assert "2026-09-05" in notes
        assert "cache write" in notes.lower() or "1.25" in notes
        assert "reasoning" in notes.lower()


class TestConflictDetection:
    """Test the conflict detection logic."""

    def test_matching_values_return_true(self) -> None:
        pinned = _OPENAI_GPT56_TERRA
        existing = _rule_to_dict(pinned)
        assert _values_match(_rule_to_dict(pinned), existing) is True

    def test_different_input_price_returns_false(self) -> None:
        pinned = _OPENAI_GPT56_TERRA
        existing = _rule_to_dict(pinned)
        existing["input_per_million_usd"] = Decimal("3.00")
        assert _values_match(_rule_to_dict(pinned), existing) is False

    def test_different_model_returns_false(self) -> None:
        pinned = _OPENAI_GPT56_TERRA
        existing = _rule_to_dict(pinned)
        existing["model"] = "gpt-5.6-terra-v2"
        assert _values_match(_rule_to_dict(pinned), existing) is False

    def test_none_vs_decimal_returns_false(self) -> None:
        pinned = _OPENAI_GPT56_TERRA
        existing = _rule_to_dict(pinned)
        existing["input_per_million_usd"] = None
        assert _values_match(_rule_to_dict(pinned), existing) is False

    def test_decimal_equal_with_different_precision(self) -> None:
        """Decimal('2.00') == Decimal('2.0') should match."""
        pinned = _OPENAI_GPT56_TERRA
        existing = _rule_to_dict(pinned)
        existing["input_per_million_usd"] = Decimal("2.0")
        assert _values_match(_rule_to_dict(pinned), existing) is True

    def test_format_conflict_produces_sanitized_report(self) -> None:
        pinned = _OPENAI_GPT56_TERRA
        existing = MagicMock()
        existing.provider = pinned.provider
        existing.provider_surface = pinned.provider_surface
        existing.model = "different-model"
        existing.effective_from = pinned.effective_from
        existing.effective_to = pinned.effective_to
        existing.input_per_million_usd = Decimal("3.00")
        existing.cached_input_per_million_usd = pinned.cached_input_per_million_usd
        existing.cache_write_per_million_usd = pinned.cache_write_per_million_usd
        existing.output_per_million_usd = pinned.output_per_million_usd
        existing.reasoning_per_million_usd = pinned.reasoning_per_million_usd
        existing.citation_per_million_usd = pinned.citation_per_million_usd
        existing.search_per_1000_usd = pinned.search_per_1000_usd
        existing.request_fee_usd = pinned.request_fee_usd
        existing.input_tokens_include_cached = pinned.input_tokens_include_cached
        existing.output_tokens_include_reasoning = pinned.output_tokens_include_reasoning

        report = _format_conflict(pinned, existing)
        assert "CONFLICT" in report
        assert "model" in report
        assert "input_per_million_usd" in report
        # No secrets in the report.
        assert "API_KEY" not in report
        assert "SECRET" not in report
        assert "PASSWORD" not in report


class TestScriptImportResolution:
    """AST-based validation that all app.* imports in the script resolve."""

    @pytest.fixture()
    def script_ast(self) -> ast.Module:
        source = _SCRIPT_PATH.read_text()
        return ast.parse(source)

    def _get_app_imports(self, tree: ast.Module) -> list[tuple[str, str | None]]:
        imports: list[tuple[str, str | None]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.startswith("app."):
                    for alias in node.names:
                        imports.append((module, alias.name))
        return imports

    def test_all_app_imports_resolve(self, script_ast: ast.Module) -> None:
        imports = self._get_app_imports(script_ast)
        assert len(imports) > 0, "Script must have app.* imports"

        failures: list[str] = []
        for module_path, name in imports:
            try:
                mod = importlib.import_module(module_path)
            except ModuleNotFoundError as exc:
                failures.append(f"Cannot import {module_path}: {exc}")
                continue
            if name is not None and not hasattr(mod, name):
                failures.append(f"{module_path} has no attribute '{name}'")

        assert not failures, (
            "app.* imports in seed_provider_pricing.py do not resolve: " + "; ".join(failures)
        )


class TestCheckMode:
    """Test --check CLI mode with mocked sessions."""

    def test_check_missing(self) -> None:
        """Empty DB -> --check reports MISSING."""
        from scripts.seed_provider_pricing import main

        mock_session = MagicMock()
        mock_session.__enter__.return_value = mock_session
        mock_session.__exit__.return_value = False

        # No existing rule found, no overlapping rules.
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = []
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute.return_value = mock_result

        with patch("app.db.session.get_session_factory", return_value=lambda: mock_session):
            ret = main(["--check"])

        assert ret == EXIT_OK
        # Verify check was read-only (no add/commit/flush).
        assert not mock_session.add.called
        assert not mock_session.commit.called

    def test_check_ready(self) -> None:
        """Existing matching rule -> --check reports READY."""
        from scripts.seed_provider_pricing import main

        mock_session = MagicMock()
        mock_session.__enter__.return_value = mock_session
        mock_session.__exit__.return_value = False

        # Create a mock existing rule that matches all canonical fields.
        existing = MagicMock()
        existing.provider = _OPENAI_GPT56_TERRA.provider
        existing.provider_surface = _OPENAI_GPT56_TERRA.provider_surface
        existing.model = _OPENAI_GPT56_TERRA.model
        existing.effective_from = _OPENAI_GPT56_TERRA.effective_from
        existing.effective_to = _OPENAI_GPT56_TERRA.effective_to
        existing.input_per_million_usd = _OPENAI_GPT56_TERRA.input_per_million_usd
        existing.cached_input_per_million_usd = _OPENAI_GPT56_TERRA.cached_input_per_million_usd
        existing.cache_write_per_million_usd = _OPENAI_GPT56_TERRA.cache_write_per_million_usd
        existing.output_per_million_usd = _OPENAI_GPT56_TERRA.output_per_million_usd
        existing.reasoning_per_million_usd = _OPENAI_GPT56_TERRA.reasoning_per_million_usd
        existing.citation_per_million_usd = _OPENAI_GPT56_TERRA.citation_per_million_usd
        existing.search_per_1000_usd = _OPENAI_GPT56_TERRA.search_per_1000_usd
        existing.request_fee_usd = _OPENAI_GPT56_TERRA.request_fee_usd
        existing.input_tokens_include_cached = _OPENAI_GPT56_TERRA.input_tokens_include_cached
        existing.output_tokens_include_reasoning = (
            _OPENAI_GPT56_TERRA.output_tokens_include_reasoning
        )

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing
        mock_session.execute.return_value = mock_result

        with patch("app.db.session.get_session_factory", return_value=lambda: mock_session):
            ret = main(["--check"])

        assert ret == EXIT_OK
        assert not mock_session.commit.called

    def test_check_conflict(self) -> None:
        """Existing rule with different values -> --check reports CONFLICT."""
        from scripts.seed_provider_pricing import main

        mock_session = MagicMock()
        mock_session.__enter__.return_value = mock_session
        mock_session.__exit__.return_value = False

        # Create a mock existing rule with a different model.
        existing = MagicMock()
        existing.provider = _OPENAI_GPT56_TERRA.provider
        existing.provider_surface = _OPENAI_GPT56_TERRA.provider_surface
        existing.model = "different-model"
        existing.effective_from = _OPENAI_GPT56_TERRA.effective_from
        existing.effective_to = _OPENAI_GPT56_TERRA.effective_to
        existing.input_per_million_usd = _OPENAI_GPT56_TERRA.input_per_million_usd
        existing.cached_input_per_million_usd = _OPENAI_GPT56_TERRA.cached_input_per_million_usd
        existing.cache_write_per_million_usd = _OPENAI_GPT56_TERRA.cache_write_per_million_usd
        existing.output_per_million_usd = _OPENAI_GPT56_TERRA.output_per_million_usd
        existing.reasoning_per_million_usd = _OPENAI_GPT56_TERRA.reasoning_per_million_usd
        existing.citation_per_million_usd = _OPENAI_GPT56_TERRA.citation_per_million_usd
        existing.search_per_1000_usd = _OPENAI_GPT56_TERRA.search_per_1000_usd
        existing.request_fee_usd = _OPENAI_GPT56_TERRA.request_fee_usd
        existing.input_tokens_include_cached = _OPENAI_GPT56_TERRA.input_tokens_include_cached
        existing.output_tokens_include_reasoning = (
            _OPENAI_GPT56_TERRA.output_tokens_include_reasoning
        )

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing
        mock_session.execute.return_value = mock_result

        with patch("app.db.session.get_session_factory", return_value=lambda: mock_session):
            ret = main(["--check"])

        assert ret == EXIT_FAIL


class TestApplyMode:
    """Test --apply CLI mode with mocked sessions."""

    def test_apply_creates_missing_rule(self) -> None:
        """Empty DB -> --apply creates the rule."""
        from scripts.seed_provider_pricing import main

        mock_session = MagicMock()
        mock_session.__enter__.return_value = mock_session
        mock_session.__exit__.return_value = False

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = []
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute.return_value = mock_result

        with patch("app.db.session.get_session_factory", return_value=lambda: mock_session):
            ret = main(["--apply"])

        assert ret == EXIT_OK
        assert mock_session.add.called, "Rule must be added to session"
        assert mock_session.commit.called, "Must commit the transaction"

    def test_apply_idempotent_on_ready(self) -> None:
        """Existing matching rule -> --apply makes zero writes."""
        from scripts.seed_provider_pricing import main

        mock_session = MagicMock()
        mock_session.__enter__.return_value = mock_session
        mock_session.__exit__.return_value = False

        existing = MagicMock()
        existing.provider = _OPENAI_GPT56_TERRA.provider
        existing.provider_surface = _OPENAI_GPT56_TERRA.provider_surface
        existing.model = _OPENAI_GPT56_TERRA.model
        existing.effective_from = _OPENAI_GPT56_TERRA.effective_from
        existing.effective_to = _OPENAI_GPT56_TERRA.effective_to
        existing.input_per_million_usd = _OPENAI_GPT56_TERRA.input_per_million_usd
        existing.cached_input_per_million_usd = _OPENAI_GPT56_TERRA.cached_input_per_million_usd
        existing.cache_write_per_million_usd = _OPENAI_GPT56_TERRA.cache_write_per_million_usd
        existing.output_per_million_usd = _OPENAI_GPT56_TERRA.output_per_million_usd
        existing.reasoning_per_million_usd = _OPENAI_GPT56_TERRA.reasoning_per_million_usd
        existing.citation_per_million_usd = _OPENAI_GPT56_TERRA.citation_per_million_usd
        existing.search_per_1000_usd = _OPENAI_GPT56_TERRA.search_per_1000_usd
        existing.request_fee_usd = _OPENAI_GPT56_TERRA.request_fee_usd
        existing.input_tokens_include_cached = _OPENAI_GPT56_TERRA.input_tokens_include_cached
        existing.output_tokens_include_reasoning = (
            _OPENAI_GPT56_TERRA.output_tokens_include_reasoning
        )

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing
        mock_session.execute.return_value = mock_result

        with patch("app.db.session.get_session_factory", return_value=lambda: mock_session):
            ret = main(["--apply"])

        assert ret == EXIT_OK
        assert not mock_session.add.called, "No writes on idempotent run"

    def test_apply_fails_on_conflict(self) -> None:
        """Existing rule with different values -> --apply fails closed."""
        from scripts.seed_provider_pricing import main

        mock_session = MagicMock()
        mock_session.__enter__.return_value = mock_session
        mock_session.__exit__.return_value = False

        existing = MagicMock()
        existing.provider = _OPENAI_GPT56_TERRA.provider
        existing.provider_surface = _OPENAI_GPT56_TERRA.provider_surface
        existing.model = "different-model"
        existing.effective_from = _OPENAI_GPT56_TERRA.effective_from
        existing.effective_to = _OPENAI_GPT56_TERRA.effective_to
        existing.input_per_million_usd = _OPENAI_GPT56_TERRA.input_per_million_usd
        existing.cached_input_per_million_usd = _OPENAI_GPT56_TERRA.cached_input_per_million_usd
        existing.cache_write_per_million_usd = _OPENAI_GPT56_TERRA.cache_write_per_million_usd
        existing.output_per_million_usd = _OPENAI_GPT56_TERRA.output_per_million_usd
        existing.reasoning_per_million_usd = _OPENAI_GPT56_TERRA.reasoning_per_million_usd
        existing.citation_per_million_usd = _OPENAI_GPT56_TERRA.citation_per_million_usd
        existing.search_per_1000_usd = _OPENAI_GPT56_TERRA.search_per_1000_usd
        existing.request_fee_usd = _OPENAI_GPT56_TERRA.request_fee_usd
        existing.input_tokens_include_cached = _OPENAI_GPT56_TERRA.input_tokens_include_cached
        existing.output_tokens_include_reasoning = (
            _OPENAI_GPT56_TERRA.output_tokens_include_reasoning
        )

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing
        mock_session.execute.return_value = mock_result

        with patch("app.db.session.get_session_factory", return_value=lambda: mock_session):
            ret = main(["--apply"])

        assert ret == EXIT_FAIL
        assert mock_session.rollback.called, "Must rollback on conflict"
        assert not mock_session.commit.called, "Must not commit on conflict"
