"""Offline tests for live-validation provider evidence capture."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.core.enums import ProviderExecutionMode
from app.providers.base import ProviderRequest
from app.providers.errors import ProviderResponseError
from app.providers.evidence import (
    EVIDENCE_BUNDLE_NAMES,
    EvidenceSecurityError,
    FilesystemProviderEvidenceSink,
    prepare_validation_directory,
    validate_evidence_bundle,
)
from app.providers.openai_adapter import OpenAIProviderAdapter


def _settings() -> Settings:
    return Settings(
        app_env="test",
        openai_api_key=SecretStr("test-only-key"),
        openai_scan_model="gpt-test",
        openai_base_url="https://api.openai.com/v1",
        openai_web_search_max_tool_calls=3,
    )


def _response_payload(*, include_secrets: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": "resp_evidence",
        "model": "gpt-test",
        "output_text": "The answer.",
        "output": [
            {
                "type": "web_search_call",
                "action": {"type": "search", "query": "must not be copied to actions"},
            },
            {"type": "message", "content": [{"type": "output_text", "text": "The answer."}]},
            {
                "type": "web_search_call",
                "action": {"type": "open_page", "url": "https://example.test"},
            },
            {"type": "web_search_call", "action": {"type": "future_action"}},
        ],
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        "status": "completed",
        "incomplete_details": None,
    }
    if include_secrets:
        payload["response_metadata"] = {
            "Authorization": "Bearer TEST_SECRET",
            "openai_api_key": "sk-proj-TEST_SECRET_VALUE",
            "nested": [{"secret": "TEST_SECRET"}],
        }
    return payload


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, indent=2, sort_keys=True, separators=(",", ": ")
    ).encode("utf-8")


def _capture_bundle(
    tmp_path: Path, *, include_secrets: bool = False
) -> tuple[Path, dict[str, Any], bytes]:
    run = prepare_validation_directory(tmp_path / "evidence-root", "bundle-1")
    sink = FilesystemProviderEvidenceSink(
        run,
        metadata={
            "validation_id": "bundle-1",
            "workspace_id": "workspace-1",
            "prompt_sha256": "prompt-hash",
            "project_id": "project-1",
            "prompt_set_id": "prompt-set-1",
            "prompt_id": "prompt-1",
            "scan_id": "scan-1",
            "prompt_run_id": "run-1",
            "provider": "OPENAI",
            "surface": "OPENAI_RESPONSES_API",
            "requested_model": "gpt-test",
        },
    )
    sink.ensure_ready()
    payload = _response_payload(include_secrets=include_secrets)
    raw_response = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    sink.capture_transport(
        response_status=200,
        response_headers={"x-request-id": "req-1"},
        response_bytes=raw_response,
        provider_request_id="req-1",
        latency_ms=12,
    )
    sink.capture_response(
        request=ProviderRequest(
            prompt="exact prompt",
            mode=ProviderExecutionMode.WEB_GROUNDED,
            model="gpt-test",
            correlation_id="corr-1",
        ),
        request_body={
            "model": "gpt-test",
            "input": "exact prompt",
            "store": False,
            "metadata": {"Authorization": "Bearer TEST_SECRET"},
        },
        response_status=200,
        response_headers={"x-request-id": "req-1", "authorization": "must-not-copy"},
        response_bytes=raw_response,
        response_json=payload,
        provider_request_id="req-1",
        provider_response_id="resp_evidence",
        latency_ms=12,
    )
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    expected = {
        key: manifest[key]
        for key in (
            "validation_id",
            "workspace_id",
            "prompt_sha256",
            "project_id",
            "prompt_set_id",
            "prompt_id",
            "scan_id",
            "prompt_run_id",
            "provider",
            "surface",
            "requested_model",
        )
        if key in manifest
    }
    return run, expected, raw_response


def _rewrite_manifest(run: Path, manifest: dict[str, Any], *, update_detached: bool) -> None:
    manifest_bytes = _canonical_json(manifest)
    (run / "manifest.json").write_bytes(manifest_bytes)
    if update_detached:
        (run / "manifest.sha256").write_text(
            hashlib.sha256(manifest_bytes).hexdigest() + "\n", encoding="ascii"
        )


def test_filesystem_sink_sanitizes_response_and_indexes_complete_bundle(
    tmp_path: Path,
) -> None:
    run, expected, raw_response = _capture_bundle(tmp_path, include_secrets=True)

    response = json.loads((run / "response.json").read_text(encoding="utf-8"))
    assert (run / "response.json").read_bytes() != raw_response
    assert [item["type"] for item in response["output"]] == [
        "web_search_call",
        "message",
        "web_search_call",
        "web_search_call",
    ]
    assert response["output"][0]["action"]["type"] == "search"
    assert response["output"][2]["action"]["type"] == "open_page"
    assert response["response_metadata"]["Authorization"] == "[REDACTED]"
    assert response["response_metadata"]["openai_api_key"] == "[REDACTED]"

    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    assert {row["filename"] for row in manifest["artifacts"]} == {
        "request.json",
        "response_transport.json",
        "response.json",
        "web_actions.json",
    }
    assert (run / "manifest.sha256").is_file()
    assert manifest["response_transport_sha256"] == hashlib.sha256(raw_response).hexdigest()
    assert validate_evidence_bundle(run, expected_fields=expected) is None
    assert all(
        b"TEST_SECRET" not in path.read_bytes()
        and b"sk-proj-TEST_SECRET_VALUE" not in path.read_bytes()
        for path in run.iterdir()
    )


def test_openai_adapter_captures_transport_before_functional_validation(tmp_path: Path) -> None:
    payload = _response_payload()
    payload["output_text"] = ""
    payload["output"] = [payload["output"][0]]
    raw_response = json.dumps(payload).encode("utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://api.openai.com/v1/responses")
        return httpx.Response(200, content=raw_response, headers={"x-request-id": "req-fail"})

    run = prepare_validation_directory(tmp_path / "evidence", "offline-fail")
    sink = FilesystemProviderEvidenceSink(
        run,
        metadata={"validation_id": "offline-fail", "prompt_sha256": "abc"},
    )
    sink.ensure_ready()
    adapter = OpenAIProviderAdapter(
        settings=_settings(),
        transport=httpx.MockTransport(handler),
        evidence_sink=sink,
    )
    with pytest.raises(ProviderResponseError):
        asyncio.run(
            adapter.execute(
                ProviderRequest(prompt="exact", mode=ProviderExecutionMode.WEB_GROUNDED)
            )
        )

    transport = json.loads((run / "response_transport.json").read_text(encoding="utf-8"))
    assert transport["response_bytes"] == len(raw_response)
    assert json.loads((run / "response.json").read_text(encoding="utf-8"))["output_text"] == ""
    assert json.loads((run / "manifest.json").read_text(encoding="utf-8"))[
        "provider_request_id"
    ] == ("req-fail")


def test_sink_recursively_redacts_sensitive_request_and_response_metadata(
    tmp_path: Path,
) -> None:
    run, _, _ = _capture_bundle(tmp_path, include_secrets=True)

    for name in EVIDENCE_BUNDLE_NAMES:
        assert b"TEST_SECRET" not in (run / name).read_bytes()
        assert b"sk-proj-TEST_SECRET_VALUE" not in (run / name).read_bytes()
    request = json.loads((run / "request.json").read_text(encoding="utf-8"))
    assert request["metadata"]["Authorization"] == "[REDACTED]"


def test_validation_directory_is_hashed_and_rejects_collision(tmp_path: Path) -> None:
    root = tmp_path / "evidence-root"
    run_path = prepare_validation_directory(root, "../path/escape")
    assert run_path.parent == root
    assert run_path.name.startswith("validation-")
    assert "/" not in run_path.name and "\\" not in run_path.name

    FilesystemProviderEvidenceSink(run_path, metadata={"validation_id": "escape"}).ensure_ready()
    with pytest.raises(ValueError, match="already exists"):
        prepare_validation_directory(root, "../path/escape")


def test_sink_rejects_existing_run_directory(tmp_path: Path) -> None:
    run_path = tmp_path / "run"
    run_path.mkdir()
    with pytest.raises(ValueError, match="already exists"):
        FilesystemProviderEvidenceSink(
            run_path, metadata={"validation_id": "collision"}
        ).ensure_ready()


def test_sink_rejects_artifact_path_escape(tmp_path: Path) -> None:
    run, _, _ = _capture_bundle(tmp_path)
    sink = FilesystemProviderEvidenceSink(run, metadata={"validation_id": "path"})
    with pytest.raises(ValueError, match="escaped"):
        sink._atomic_write_bytes(run.parent / "escape.json", b"must-not-write")
    with pytest.raises(ValueError, match="escaped"):
        sink._atomic_write_bytes(run / ".." / "escape.json", b"must-not-write")
    assert not (run.parent / "escape.json").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink and mode guard")
def test_sink_rejects_symlink_roots_intermediates_and_artifacts(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root_link = tmp_path / "root-link"
    root_link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(EvidenceSecurityError, match="symlink|unsafe|missing"):
        prepare_validation_directory(root_link, "symlink-root")

    parent = tmp_path / "parent"
    parent.mkdir()
    intermediate_link = parent / "link"
    intermediate_link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(EvidenceSecurityError, match="symlink|unsafe|missing"):
        prepare_validation_directory(intermediate_link / "child", "symlink-intermediate")

    run = prepare_validation_directory(tmp_path / "secure-root", "artifact-link")
    sink = FilesystemProviderEvidenceSink(run, metadata={"validation_id": "artifact-link"})
    sink.ensure_ready()
    destination = run / "request.json"
    destination.symlink_to(outside / "outside-file")
    with pytest.raises(EvidenceSecurityError):
        sink._atomic_write_bytes(destination, b"must not follow")

    assert not (outside / "outside-file").exists()
    assert run.stat().st_mode & 0o777 == 0o700
    assert (run / "manifest.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission guard")
def test_prepare_rejects_insecure_evidence_root(tmp_path: Path) -> None:
    root = tmp_path / "insecure-root"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    with pytest.raises(ValueError, match="permissions"):
        prepare_validation_directory(root, "insecure")


def test_sink_never_overwrites_published_artifact(tmp_path: Path) -> None:
    run, _, _ = _capture_bundle(tmp_path)
    original = (run / "request.json").read_bytes()
    with pytest.raises((FileExistsError, ValueError, OSError)):
        FilesystemProviderEvidenceSink._atomic_write_bytes(
            FilesystemProviderEvidenceSink(run, metadata={"validation_id": "x"}),
            run / "request.json",
            b"tampered",
        )
    assert (run / "request.json").read_bytes() == original


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_manifest",
        "missing_manifest_sha256",
        "missing_request",
        "missing_transport",
        "missing_response",
        "missing_web_actions",
        "request_hash",
        "transport_hash",
        "response_hash",
        "web_actions_hash",
        "manifest_hash",
        "size_mismatch",
        "request_top_level_hash_mismatch",
        "transport_top_level_hash_mismatch",
        "provider_response_id_mismatch",
        "web_tool_count_mismatch",
        "web_action_counts_mismatch",
        "scan_id_mismatch",
        "prompt_run_id_mismatch",
        "validation_id_mismatch",
    ],
)
def test_validator_rejects_direct_corruption_matrix(tmp_path: Path, mutation: str) -> None:
    run, expected, _ = _capture_bundle(tmp_path)
    if mutation.startswith("missing_"):
        filename = {
            "missing_manifest": "manifest.json",
            "missing_manifest_sha256": "manifest.sha256",
            "missing_request": "request.json",
            "missing_transport": "response_transport.json",
            "missing_response": "response.json",
            "missing_web_actions": "web_actions.json",
        }[mutation]
        (run / filename).unlink()
    elif mutation.endswith("_hash") and mutation != "manifest_hash":
        filename = {
            "request_hash": "request.json",
            "transport_hash": "response_transport.json",
            "response_hash": "response.json",
            "web_actions_hash": "web_actions.json",
        }[mutation]
        (run / filename).write_bytes((run / filename).read_bytes() + b"tamper")
    else:
        manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
        if mutation == "manifest_hash":
            manifest["tampered"] = True
            _rewrite_manifest(run, manifest, update_detached=False)
        elif mutation == "size_mismatch":
            manifest["artifacts"][0]["size_bytes"] += 1
            _rewrite_manifest(run, manifest, update_detached=True)
        elif mutation == "request_top_level_hash_mismatch":
            manifest["request_sha256"] = "0" * 64
            _rewrite_manifest(run, manifest, update_detached=True)
        elif mutation == "transport_top_level_hash_mismatch":
            manifest["response_transport_sha256"] = "0" * 64
            _rewrite_manifest(run, manifest, update_detached=True)
        elif mutation == "provider_response_id_mismatch":
            manifest["provider_response_id"] = "wrong-response-id"
            _rewrite_manifest(run, manifest, update_detached=True)
        elif mutation == "web_tool_count_mismatch":
            manifest["web_tool_call_count"] += 1
            _rewrite_manifest(run, manifest, update_detached=True)
        elif mutation == "web_action_counts_mismatch":
            manifest["web_action_counts"]["search"] += 1
            _rewrite_manifest(run, manifest, update_detached=True)
        else:
            manifest[mutation.removesuffix("_mismatch")] = "wrong-id"
            _rewrite_manifest(run, manifest, update_detached=True)

    result = validate_evidence_bundle(run, expected_fields=expected)
    assert result is not None
    assert result.startswith("EVIDENCE_BUNDLE_PARTIAL_OR_CORRUPT")


def test_validator_rejects_manifest_internal_hash_with_valid_detached_hash(
    tmp_path: Path,
) -> None:
    run, expected, _ = _capture_bundle(tmp_path)
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    manifest["artifacts"][0]["sha256"] = "0" * 64
    _rewrite_manifest(run, manifest, update_detached=True)
    assert validate_evidence_bundle(run, expected_fields=expected) is not None


def test_validator_rejects_orphan_temp_artifact(tmp_path: Path) -> None:
    run, expected, _ = _capture_bundle(tmp_path)
    (run / ".request.json.orphan.tmp").write_bytes(b"partial")
    result = validate_evidence_bundle(run, expected_fields=expected)
    assert result is not None
    assert result.startswith("EVIDENCE_BUNDLE_PARTIAL_OR_CORRUPT")
