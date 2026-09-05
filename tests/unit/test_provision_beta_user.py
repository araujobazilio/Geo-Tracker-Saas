"""Unit tests for the beta user provisioning operator script.

Regression coverage for the Phase 13.5.4 defect where
scripts/provision_beta_user.py imported WorkspaceMember from a
nonexistent app.models.workspace_member module. WorkspaceMember is
actually defined in app.models.workspace.

These tests verify:
1. The actual main() function can be called and reaches application
   logic (does not raise ModuleNotFoundError from bad imports).
2. AST-based import resolution: every app.* ImportFrom target in the
   script resolves to a real importable module.
3. The script does not reference the nonexistent workspace_member module.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "provision_beta_user.py"


class TestProvisionBetaUserRuntimeImports:
    """Test that the provisioning script's runtime imports resolve."""

    def test_main_does_not_raise_module_not_found(self) -> None:
        """Calling main() must NOT raise ModuleNotFoundError.

        Before the fix, main() contained:
            from app.models.workspace_member import WorkspaceMember
        which raised ModuleNotFoundError because that module does not exist.

        After the fix, main() imports WorkspaceMember from
        app.models.workspace, which is the canonical location.

        We call main() with a nonexistent plan code and a mocked session
        factory so that main() resolves all imports and reaches the
        "PlanDefinition not found" error path, returning EXIT_FAIL (1)
        instead of raising.
        """
        from scripts.provision_beta_user import EXIT_FAIL, main

        # Mock get_session_factory to return a fake factory that yields
        # a mock session. This prevents any real database connection.
        mock_session = MagicMock()
        # The script uses `with factory() as session:`, so __enter__
        # must return the same mock session.
        mock_session.__enter__.return_value = mock_session
        mock_session.__exit__.return_value = False
        mock_factory = MagicMock(return_value=mock_session)
        # session.execute returns a mock that has scalar_one_or_none → None
        # (no plan found), so main() should return EXIT_FAIL.
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        with patch("app.db.session.get_session_factory", return_value=mock_factory):
            # Use a nonexistent plan code so main() reaches the
            # "PlanDefinition not found" error path after all imports resolve.
            ret = main(["--email", "test@example.com", "--plan-code", "NONEXISTENT_PLAN"])

        assert ret == EXIT_FAIL, (
            "main() should return EXIT_FAIL for a nonexistent plan, "
            "not raise ModuleNotFoundError from a bad import"
        )

    def test_main_reaches_application_logic(self) -> None:
        """main() must get past all imports and reach the plan lookup.

        If the imports are broken, main() raises before any session
        interaction. If imports are correct, main() reaches the
        session.execute() call for PlanDefinition lookup.
        """
        from scripts.provision_beta_user import main

        mock_session = MagicMock()
        # The script uses `with factory() as session:`, so __enter__
        # must return the same mock session.
        mock_session.__enter__.return_value = mock_session
        mock_session.__exit__.return_value = False
        mock_factory = MagicMock(return_value=mock_session)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        with patch("app.db.session.get_session_factory", return_value=mock_factory):
            main(["--email", "test@example.com", "--plan-code", "NONEXISTENT"])

        # Verify main() actually reached the session.execute() call,
        # proving it got past all runtime imports.
        assert mock_session.execute.called, (
            "main() must reach session.execute() for plan lookup, "
            "proving all runtime imports resolved successfully"
        )


class TestScriptImportResolution:
    """AST-based validation that all app.* imports in the script resolve."""

    @pytest.fixture()
    def script_ast(self) -> ast.Module:
        """Parse the provisioning script into an AST."""
        source = _SCRIPT_PATH.read_text()
        return ast.parse(source)

    def _get_app_imports(self, tree: ast.Module) -> list[tuple[str, str | None]]:
        """Extract all (module, name) pairs from ImportFrom nodes targeting app.*.

        Returns a list of (module_path, imported_name) tuples.
        """
        imports: list[tuple[str, str | None]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.startswith("app."):
                    for alias in node.names:
                        imports.append((module, alias.name))
        return imports

    def test_all_app_imports_resolve(self, script_ast: ast.Module) -> None:
        """Every app.* ImportFrom target in the script must be importable.

        This is a semantic import-resolution test: it parses the script's
        AST, finds every `from app.X import Y` statement, and verifies
        that app.X can actually be imported and Y exists in it.
        """
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
            "These app.* imports in provision_beta_user.py do not resolve: " + "; ".join(failures)
        )

    def test_no_workspace_member_module_reference(self, script_ast: ast.Module) -> None:
        """The script must NOT reference app.models.workspace_member.

        This module does not exist. WorkspaceMember is defined in
        app.models.workspace.
        """
        imports = self._get_app_imports(script_ast)
        for module_path, _name in imports:
            assert module_path != "app.models.workspace_member", (
                "Script must not import from app.models.workspace_member "
                "(this module does not exist; WorkspaceMember is in "
                "app.models.workspace)"
            )

    def test_workspace_member_imported_from_correct_module(self, script_ast: ast.Module) -> None:
        """WorkspaceMember must be imported from app.models.workspace."""
        imports = self._get_app_imports(script_ast)
        wm_imports = [(mod, name) for mod, name in imports if name == "WorkspaceMember"]
        assert len(wm_imports) == 1, "Script must import WorkspaceMember exactly once"
        mod, _name = wm_imports[0]
        assert mod == "app.models.workspace", (
            f"WorkspaceMember must be imported from app.models.workspace, " f"not from {mod}"
        )

    def test_workspace_also_imported_from_correct_module(self, script_ast: ast.Module) -> None:
        """Workspace must be imported from app.models.workspace."""
        imports = self._get_app_imports(script_ast)
        ws_imports = [(mod, name) for mod, name in imports if name == "Workspace"]
        assert len(ws_imports) >= 1, "Script must import Workspace"
        for mod, _name in ws_imports:
            assert mod == "app.models.workspace", (
                f"Workspace must be imported from app.models.workspace, " f"not from {mod}"
            )
