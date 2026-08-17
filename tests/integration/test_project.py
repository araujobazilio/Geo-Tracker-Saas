"""Integration tests for Project, keyword, competitor, provider, prompt."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.enums import (
    CompetitorSource,
    FunnelStage,
    LLMProvider,
    ProjectStatus,
    PromptType,
    WorkspaceRole,
    WorkspaceType,
)
from app.models import (
    Competitor,
    Project,
    ProjectKeyword,
    ProjectProvider,
    Prompt,
    User,
    Workspace,
    WorkspaceMember,
)


def _make_project(db_session, name: str = "Acme", domain: str = "acme.example") -> Project:  # type: ignore[no-untyped-def]
    user = User(email=f"{name.lower()}@example.com", password_hash="h")
    ws = Workspace(name=f"{name} WS", workspace_type=WorkspaceType.PERSONAL)
    db_session.add_all([user, ws])
    db_session.flush()
    db_session.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role=WorkspaceRole.OWNER))
    db_session.flush()
    project = Project(
        workspace_id=ws.id,
        name=name,
        domain=domain,
        brand_name=name,
        brand_aliases=[f"{name} Inc"],
        industry="Email Marketing",
        target_country="US",
        target_language="en",
        target_audience="Small Businesses",
        status=ProjectStatus.ACTIVE,
    )
    db_session.add(project)
    db_session.flush()
    return project


@pytest.mark.integration
def test_create_project(db_session) -> None:  # type: ignore[no-untyped-def]
    p = _make_project(db_session)
    assert p.id is not None
    assert p.brand_aliases == ["Acme Inc"]
    assert p.status == ProjectStatus.ACTIVE


@pytest.mark.integration
def test_project_workspace_relationship(db_session) -> None:  # type: ignore[no-untyped-def]
    p = _make_project(db_session)
    assert p.workspace_id is not None


@pytest.mark.integration
def test_create_keyword(db_session) -> None:  # type: ignore[no-untyped-def]
    p = _make_project(db_session)
    kw = ProjectKeyword(
        project_id=p.id, text="best email marketing software", funnel_stage=FunnelStage.PURCHASE
    )
    db_session.add(kw)
    db_session.flush()
    assert kw.id is not None
    assert kw.active is True


@pytest.mark.integration
def test_keyword_unique_per_project(db_session) -> None:  # type: ignore[no-untyped-def]
    p = _make_project(db_session)
    db_session.add(ProjectKeyword(project_id=p.id, text="email automation"))
    db_session.flush()
    db_session.add(ProjectKeyword(project_id=p.id, text="email automation"))
    with pytest.raises(IntegrityError):
        db_session.flush()


@pytest.mark.integration
def test_create_competitor(db_session) -> None:  # type: ignore[no-untyped-def]
    p = _make_project(db_session)
    c = Competitor(
        project_id=p.id,
        name="Mailchimp",
        domain="mailchimp.com",
        source=CompetitorSource.USER_DEFINED,
    )
    db_session.add(c)
    db_session.flush()
    assert c.id is not None
    assert c.source == CompetitorSource.USER_DEFINED


@pytest.mark.integration
def test_competitor_unique_per_project_domain(db_session) -> None:  # type: ignore[no-untyped-def]
    p = _make_project(db_session)
    db_session.add(Competitor(project_id=p.id, name="Mailchimp", domain="mailchimp.com"))
    db_session.flush()
    db_session.add(Competitor(project_id=p.id, name="Mailchimp2", domain="mailchimp.com"))
    with pytest.raises(IntegrityError):
        db_session.flush()


@pytest.mark.integration
def test_project_provider_config(db_session) -> None:  # type: ignore[no-untyped-def]
    p = _make_project(db_session)
    pp = ProjectProvider(project_id=p.id, provider=LLMProvider.OPENAI, enabled=True)
    db_session.add(pp)
    db_session.flush()
    assert pp.id is not None
    assert pp.provider == LLMProvider.OPENAI


@pytest.mark.integration
def test_project_provider_unique(db_session) -> None:  # type: ignore[no-untyped-def]
    p = _make_project(db_session)
    db_session.add(ProjectProvider(project_id=p.id, provider=LLMProvider.OPENAI))
    db_session.flush()
    db_session.add(ProjectProvider(project_id=p.id, provider=LLMProvider.OPENAI))
    with pytest.raises(IntegrityError):
        db_session.flush()


@pytest.mark.integration
def test_prompt_creation_and_type_persistence(db_session) -> None:  # type: ignore[no-untyped-def]
    p = _make_project(db_session)
    kw = ProjectKeyword(project_id=p.id, text="best crm")
    db_session.add(kw)
    db_session.flush()
    prompt = Prompt(
        project_keyword_id=kw.id,
        text="What is the best CRM for a small business?",
        prompt_set_version=1,
        prompt_type=PromptType.NON_BRANDED,
        funnel_stage=FunnelStage.AWARENESS,
        commercial_intent=True,
    )
    db_session.add(prompt)
    db_session.flush()
    assert prompt.id is not None
    assert prompt.prompt_type == PromptType.NON_BRANDED


@pytest.mark.integration
def test_prompt_version_persistence(db_session) -> None:  # type: ignore[no-untyped-def]
    """Regeneration must NOT overwrite; a new version coexists with the old."""
    p = _make_project(db_session)
    kw = ProjectKeyword(project_id=p.id, text="best crm")
    db_session.add(kw)
    db_session.flush()
    v1 = Prompt(
        project_keyword_id=kw.id,
        text="What is the best CRM?",
        prompt_set_version=1,
        prompt_type=PromptType.NON_BRANDED,
    )
    db_session.add(v1)
    db_session.flush()
    v2 = Prompt(
        project_keyword_id=kw.id,
        text="What is the best CRM for startups?",
        prompt_set_version=2,
        prompt_type=PromptType.NON_BRANDED,
    )
    db_session.add(v2)
    db_session.flush()
    assert v1.prompt_set_version == 1
    assert v2.prompt_set_version == 2
    assert v1.id != v2.id
