"""Unit tests for domain enums."""

from __future__ import annotations

from app.core.enums import (
    BillingSource,
    LLMProvider,
    PromptType,
    ScanStatus,
    ScanType,
    WorkspaceRole,
    WorkspaceType,
)


def test_workspace_types() -> None:
    assert WorkspaceType.PERSONAL.value == "PERSONAL"
    assert WorkspaceType.AGENCY.value == "AGENCY"


def test_workspace_roles() -> None:
    assert {r.value for r in WorkspaceRole} == {"OWNER", "ADMIN", "MEMBER"}


def test_prompt_types_distinct() -> None:
    assert {p.value for p in PromptType} == {"NON_BRANDED", "BRANDED", "COMPETITOR"}


def test_llm_providers() -> None:
    assert {p.value for p in LLMProvider} == {
        "OPENAI",
        "ANTHROPIC",
        "GOOGLE",
        "PERPLEXITY",
    }


def test_scan_types_and_statuses() -> None:
    assert ScanType.STANDARD.value == "STANDARD"
    assert ScanStatus.PENDING.value == "PENDING"


def test_billing_sources() -> None:
    assert {b.value for b in BillingSource} == {"APPSUMO", "STRIPE", "ADMIN"}
