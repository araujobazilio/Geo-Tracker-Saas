"""Safe, durable provider-response evidence sinks.

The sink deliberately stores a sanitized JSON response rather than the raw
provider body. A separate transport artifact records only response metadata
and a SHA-256 fingerprint of the bytes received before JSON parsing.

Live execution uses POSIX directory descriptors and no-follow operations so a
validated evidence root cannot be swapped for a symlink between the security
check and the write. Windows remains supported for plan mode and offline
tests, but it is not treated as equivalent to the POSIX live-execution guard.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat as stat_module
import tempfile
import uuid
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from app.providers.base import ProviderRequest
from app.providers.web_actions import classify_openai_web_actions

REDACTED = "[REDACTED]"
EVIDENCE_ARTIFACT_NAMES = (
    "request.json",
    "response_transport.json",
    "response.json",
    "web_actions.json",
)
EVIDENCE_BUNDLE_NAMES = (*EVIDENCE_ARTIFACT_NAMES, "manifest.json", "manifest.sha256")
_ARTIFACT_ROLES = {
    "request.json": "request",
    "response_transport.json": "response_transport",
    "response.json": "response_json",
    "web_actions.json": "web_actions",
}
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_OPENAI_KEY_RE = re.compile(r"(?i)\bsk-(?:proj-)?[A-Za-z0-9_-]{8,}\b")
_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "proxy_authorization",
        "api_key",
        "apikey",
        "x_api_key",
        "openai_api_key",
        "access_token",
        "refresh_token",
        "id_token",
        "client_secret",
        "password",
        "secret",
        "bearer",
        "auth",
        "credential",
        "credentials",
        "cookie",
        "set_cookie",
        "token",
    }
)
_POSIX_REQUIRED_FLAGS = ("O_DIRECTORY", "O_NOFOLLOW")
_POSIX_SECURITY_ERRNOS = frozenset(
    {
        errno.EACCES,
        errno.ELOOP,
        errno.ENOTDIR,
        errno.ENOENT,
        errno.EPERM,
    }
)


class EvidenceError(ValueError):
    """Base class for safe, predictable evidence-boundary failures."""


class EvidenceSecurityError(EvidenceError):
    """The evidence path or permissions violate the security contract."""


class EvidenceCollisionError(EvidenceError):
    """A published evidence path already belongs to an execution."""


class EvidenceIntegrityError(EvidenceError):
    """A required evidence artifact or publication invariant is invalid."""


class ProviderEvidenceSink(Protocol):
    """Capture one provider response without changing provider semantics."""

    def ensure_ready(self) -> None:
        """Validate/create the destination before a provider call."""

    def capture_transport(
        self,
        *,
        response_status: int,
        response_headers: Mapping[str, str],
        response_bytes: bytes,
        provider_request_id: str | None,
        latency_ms: int,
    ) -> None:
        """Persist safe transport metadata before JSON parsing."""

    def capture_response(
        self,
        *,
        request: ProviderRequest,
        request_body: Mapping[str, Any],
        response_status: int,
        response_headers: Mapping[str, str],
        response_bytes: bytes,
        response_json: Mapping[str, Any] | None,
        provider_request_id: str | None,
        provider_response_id: str | None,
        latency_ms: int,
    ) -> None:
        """Persist one sanitized response envelope exactly once."""


def validation_evidence_directory(root: Path, validation_id: str) -> Path:
    """Return the deterministic run path without creating it."""

    root_path = _absolute_path(root)
    run_name = f"validation-{hashlib.sha256(validation_id.encode('utf-8')).hexdigest()}"
    return root_path / run_name


def prepare_validation_directory(root: Path, validation_id: str) -> Path:
    """Validate an evidence root and return a collision-safe run path.

    The root may be created once, but the run directory is created exclusively
    by ``FilesystemProviderEvidenceSink.ensure_ready``. On POSIX, both root
    validation and the later run creation use no-follow directory descriptors.
    """

    root_path = _absolute_path(root)
    if os.name == "posix":
        root_fd = _open_or_create_secure_directory(root_path, 0o700)
        os.close(root_fd)
    else:
        try:
            resolved_root = root_path.resolve(strict=False)
        except OSError as exc:
            raise EvidenceSecurityError("Evidence root could not be resolved safely.") from exc
        if resolved_root != root_path:
            raise EvidenceSecurityError("Evidence root must not resolve through a symlink.")
        if os.path.lexists(root_path):
            if os.path.islink(root_path) or not root_path.is_dir():
                raise EvidenceSecurityError("Evidence root must be a real directory.")
        else:
            try:
                root_path.mkdir(mode=0o700, exist_ok=False)
            except OSError as exc:
                raise EvidenceSecurityError("Evidence root could not be created safely.") from exc
        _verify_posix_mode(root_path, 0o700)

    run_path = validation_evidence_directory(root_path, validation_id)
    if os.path.lexists(run_path):
        raise EvidenceCollisionError("Evidence run directory already exists.")
    return run_path


class FilesystemProviderEvidenceSink:
    """Atomic, collision-safe filesystem sink for one isolated validation.

    Content artifacts are fixed and private:

    * ``request.json`` — sanitized provider request body, without headers;
    * ``response_transport.json`` — status, request ID, byte length, latency,
      and a fingerprint of the pre-parse HTTP body;
    * ``response.json`` — the complete parsed response envelope after recursive
      sanitization, or a parse-failure marker;
    * ``web_actions.json`` — ordered sanitized action metadata.

    ``manifest.json`` indexes every content artifact with SHA-256, byte size,
    and semantic role. ``manifest.sha256`` is a detached checksum of the exact
    persisted manifest bytes. A capture failure is recorded in ``last_error``
    and is intentionally not raised past the provider adapter; the canonical
    accounting path still processes the response once and the operator reports
    the result as inconclusive without retrying.
    """

    _ARTIFACT_NAMES = frozenset(EVIDENCE_ARTIFACT_NAMES)

    def __init__(self, directory: Path, *, metadata: Mapping[str, Any]) -> None:
        self.directory = _absolute_path(directory)
        self._metadata = sanitize_evidence_structure(dict(metadata))
        self._prepared = False
        self._captured = False
        self._last_error: str | None = None
        self._prepared_at: str | None = None
        self._transport_captured = False
        self._transport_artifact_bytes: bytes | None = None
        self._dir_fd: int | None = None

    @property
    def last_error(self) -> str | None:
        """Return a sanitized local persistence error, if one occurred."""

        return self._last_error

    @property
    def artifact_paths(self) -> dict[str, Path]:
        return {name: self.directory / name for name in EVIDENCE_BUNDLE_NAMES}

    def ensure_ready(self) -> None:
        """Create and validate the evidence directory before network I/O."""

        if self._prepared:
            raise RuntimeError("Evidence sink is already prepared.")
        try:
            if os.name == "posix":
                parent_fd = _open_secure_directory(self.directory.parent)
                try:
                    os.mkdir(self.directory.name, 0o700, dir_fd=parent_fd)
                    run_fd = os.open(
                        self.directory.name,
                        _secure_directory_flags(),
                        dir_fd=parent_fd,
                    )
                except FileExistsError as exc:
                    raise EvidenceCollisionError("Evidence run directory already exists.") from exc
                finally:
                    os.close(parent_fd)
                _verify_fd_mode(run_fd, 0o700)
                self._dir_fd = run_fd
            else:
                if os.path.lexists(self.directory):
                    raise EvidenceCollisionError("Evidence run directory already exists.")
                parent = self.directory.parent
                try:
                    resolved_parent = parent.resolve(strict=False)
                except OSError as exc:
                    raise EvidenceSecurityError(
                        "Evidence run directory parent could not be resolved."
                    ) from exc
                if resolved_parent != parent or not os.path.isdir(parent) or os.path.islink(parent):
                    raise EvidenceSecurityError(
                        "Evidence run directory parent is not a safe directory."
                    )
                try:
                    self.directory.mkdir(mode=0o700, exist_ok=False)
                except FileExistsError as exc:
                    raise EvidenceCollisionError("Evidence run directory already exists.") from exc
                self._verify_mode(self.directory, 0o700)

            self._prepared_at = datetime.now(UTC).isoformat()
            self._atomic_write_json(self.directory / "manifest.json", self._prepared_manifest())
            self._prepared = True
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        """Close the retained trusted directory descriptor, if any."""

        if self._dir_fd is not None:
            os.close(self._dir_fd)
            self._dir_fd = None

    def set_execution_context(
        self,
        *,
        scan_id: str,
        prompt_run_id: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        """Persist durable identifiers before the provider call begins."""

        if not self._prepared or self._captured or self._prepared_at is None:
            raise RuntimeError("Evidence sink is not available for execution context.")
        context: dict[str, Any] = {"scan_id": scan_id, "prompt_run_id": prompt_run_id}
        if metadata is not None:
            context.update(metadata)
        self._metadata.update(sanitize_evidence_structure(context))
        self._atomic_write_json(
            self.directory / "manifest.json", self._prepared_manifest(), replace=True
        )

    def capture_transport(
        self,
        *,
        response_status: int,
        response_headers: Mapping[str, str],
        response_bytes: bytes,
        provider_request_id: str | None,
        latency_ms: int,
    ) -> None:
        """Persist safe transport facts before attempting to parse JSON."""

        if not self._prepared:
            raise RuntimeError("Evidence sink must be prepared before capture.")
        if self._transport_captured:
            raise RuntimeError("Evidence sink already captured transport evidence.")
        try:
            safe_headers = sanitize_evidence_structure(
                {
                    key.lower(): value
                    for key, value in response_headers.items()
                    if key.lower() == "x-request-id"
                }
            )
            payload = {
                "evidence_version": 1,
                "status": "captured",
                "captured_at": datetime.now(UTC).isoformat(),
                "http_status": response_status,
                "response_headers": safe_headers,
                "provider_request_id": sanitize_evidence_structure(provider_request_id),
                "response_bytes": len(response_bytes),
                "response_transport_sha256": hashlib.sha256(response_bytes).hexdigest(),
                "sha256_scope": "pre-parse HTTP response bytes; not response.json bytes",
                "latency_ms": latency_ms,
            }
            payload_bytes = self._json_bytes(payload)
            self._atomic_write_bytes(self.directory / "response_transport.json", payload_bytes)
            self._transport_artifact_bytes = payload_bytes
            self._transport_captured = True
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: evidence artifact write failed"
            raise

    def capture_response(
        self,
        *,
        request: ProviderRequest,
        request_body: Mapping[str, Any],
        response_status: int,
        response_headers: Mapping[str, str],
        response_bytes: bytes,
        response_json: Mapping[str, Any] | None,
        provider_request_id: str | None,
        provider_response_id: str | None,
        latency_ms: int,
    ) -> None:
        if not self._prepared:
            raise RuntimeError("Evidence sink must be prepared before capture.")
        if not self._transport_captured or self._transport_artifact_bytes is None:
            raise RuntimeError("Transport evidence must be captured before response evidence.")
        if self._captured:
            raise RuntimeError("Evidence sink already captured a response.")

        try:
            classification = classify_openai_web_actions(
                response_json.get("output") if response_json is not None else None
            )
            safe_headers = sanitize_evidence_structure(
                {
                    key.lower(): value
                    for key, value in response_headers.items()
                    if key.lower() == "x-request-id"
                }
            )
            action_rows = [row.as_dict() for row in classification.ordered_actions]
            request_payload = sanitize_evidence_structure(dict(request_body))
            response_payload: Any = (
                sanitize_evidence_structure(dict(response_json))
                if response_json is not None
                else {"parseable": False}
            )
            request_bytes = self._json_bytes(request_payload)
            response_json_bytes = self._json_bytes(response_payload)
            actions_bytes = self._json_bytes(action_rows)
            transport_bytes = self._transport_artifact_bytes
            artifact_bytes = {
                "request.json": request_bytes,
                "response_transport.json": transport_bytes,
                "response.json": response_json_bytes,
                "web_actions.json": actions_bytes,
            }
            transport_payload = json.loads(transport_bytes.decode("utf-8"))
            manifest = {
                "evidence_version": 1,
                "status": "captured",
                "captured_at": datetime.now(UTC).isoformat(),
                **self._metadata,
                "provider": "OPENAI",
                "surface": "OPENAI_RESPONSES_API",
                "http_status": response_status,
                "provider_request_id": sanitize_evidence_structure(provider_request_id),
                "provider_response_id": sanitize_evidence_structure(provider_response_id),
                "latency_ms": latency_ms,
                "response_json_parseable": response_json is not None,
                "response_headers": safe_headers,
                "response_transport_sha256": transport_payload["response_transport_sha256"],
                "response_json_sha256": hashlib.sha256(response_json_bytes).hexdigest(),
                "request_sha256": hashlib.sha256(request_bytes).hexdigest(),
                "request_correlation_id": sanitize_evidence_structure(request.correlation_id),
                "web_tool_call_count": classification.web_tool_call_count,
                "web_action_counts": classification.action_counts,
                "artifacts": [
                    {
                        "filename": name,
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "size_bytes": len(payload),
                        "semantic_role": _ARTIFACT_ROLES[name],
                    }
                    for name, payload in artifact_bytes.items()
                ],
            }

            self._atomic_write_bytes(self.directory / "request.json", request_bytes)
            self._atomic_write_bytes(self.directory / "response.json", response_json_bytes)
            self._atomic_write_bytes(self.directory / "web_actions.json", actions_bytes)
            manifest_bytes = self._json_bytes(manifest)
            self._atomic_write_bytes(self.directory / "manifest.json", manifest_bytes, replace=True)
            manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest().encode("ascii") + b"\n"
            self._atomic_write_bytes(self.directory / "manifest.sha256", manifest_sha256)
            self._captured = True
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: evidence artifact write failed"
            raise

    def _prepared_manifest(self) -> dict[str, Any]:
        return {
            "evidence_version": 1,
            "status": "prepared",
            "prepared_at": self._prepared_at,
            **self._metadata,
        }

    @staticmethod
    def _verify_mode(path: Path, mode: int) -> None:
        _verify_posix_mode(path, mode)

    @staticmethod
    def _json_bytes(value: Any) -> bytes:
        return json.dumps(
            value, ensure_ascii=False, indent=2, sort_keys=True, separators=(",", ": ")
        ).encode("utf-8")

    def _atomic_write_json(self, path: Path, value: Any, *, replace: bool = False) -> None:
        self._atomic_write_bytes(path, self._json_bytes(value), replace=replace)

    def _atomic_write_bytes(self, path: Path, payload: bytes, *, replace: bool = False) -> None:
        name = self._validate_artifact_path(path)
        if self._dir_fd is not None and os.name == "posix":
            self._atomic_write_posix(name, payload, replace=replace)
            return

        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=self.directory, prefix=f".{name}.", delete=False
            ) as handle:
                temporary = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            self._verify_mode(temporary, 0o600)
            destination = self.directory / name
            if replace:
                if os.path.islink(destination):
                    raise EvidenceSecurityError("Evidence artifact is a symlink.")
                os.replace(temporary, destination)
            else:
                if os.path.lexists(destination):
                    raise EvidenceCollisionError("Evidence artifact already exists.")
                os.rename(temporary, destination)
                temporary = None
            self._verify_mode(destination, 0o600)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    def _atomic_write_posix(self, name: str, payload: bytes, *, replace: bool) -> None:
        if self._dir_fd is None:
            raise RuntimeError("Trusted evidence directory descriptor is unavailable.")
        temp_name = f".{name}.{uuid.uuid4().hex}.tmp"
        temp_fd: int | None = None
        published = False
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | int(getattr(os, "O_NOFOLLOW", 0))
        flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            temp_fd = os.open(temp_name, flags, 0o600, dir_fd=self._dir_fd)
            view = memoryview(payload)
            while view:
                written = os.write(temp_fd, view)
                if written <= 0:
                    raise OSError("Evidence artifact write made no progress.")
                view = view[written:]
            os.fsync(temp_fd)
            _verify_fd_mode(temp_fd, 0o600)
            os.close(temp_fd)
            temp_fd = None

            if replace:
                try:
                    destination_stat = os.stat(name, dir_fd=self._dir_fd, follow_symlinks=False)
                except FileNotFoundError as exc:
                    raise EvidenceIntegrityError("Replace target does not exist.") from exc
                if stat_module.S_ISLNK(destination_stat.st_mode) or not stat_module.S_ISREG(
                    destination_stat.st_mode
                ):
                    raise EvidenceSecurityError("Evidence artifact is not a regular file.")
                os.replace(
                    temp_name,
                    name,
                    src_dir_fd=self._dir_fd,
                    dst_dir_fd=self._dir_fd,
                )
            else:
                try:
                    destination_stat = os.stat(name, dir_fd=self._dir_fd, follow_symlinks=False)
                except FileNotFoundError:
                    destination_stat = None
                if destination_stat is not None:
                    if stat_module.S_ISLNK(destination_stat.st_mode):
                        raise EvidenceSecurityError("Evidence artifact is a symlink.")
                    if not stat_module.S_ISREG(destination_stat.st_mode):
                        raise EvidenceSecurityError("Evidence artifact is not a regular file.")
                try:
                    os.link(
                        temp_name,
                        name,
                        src_dir_fd=self._dir_fd,
                        dst_dir_fd=self._dir_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError as exc:
                    raise EvidenceCollisionError("Evidence artifact already exists.") from exc
                os.unlink(temp_name, dir_fd=self._dir_fd)
            published = True
            final_stat = os.stat(name, dir_fd=self._dir_fd, follow_symlinks=False)
            if not stat_module.S_ISREG(final_stat.st_mode):
                raise EvidenceSecurityError("Published evidence artifact is not a regular file.")
            if stat_module.S_IMODE(final_stat.st_mode) != 0o600:
                raise EvidenceSecurityError(
                    "Evidence artifact permissions are not restrictive enough."
                )
            os.fsync(self._dir_fd)
        finally:
            if temp_fd is not None:
                os.close(temp_fd)
            if not published:
                with suppress(FileNotFoundError, OSError):
                    os.unlink(temp_name, dir_fd=self._dir_fd)

    def _validate_artifact_path(self, path: Path) -> str:
        absolute = _absolute_path(path)
        if absolute.parent != self.directory:
            raise EvidenceSecurityError("Evidence artifact escaped its run directory.")
        name = absolute.name
        if name not in EVIDENCE_BUNDLE_NAMES:
            raise EvidenceSecurityError("Unknown evidence artifact name.")
        return name


def validate_evidence_bundle(
    directory: Path,
    *,
    expected_fields: Mapping[str, Any],
) -> str | None:
    """Validate a complete bundle without following evidence symlinks.

    The returned message is intentionally operational and contains no artifact
    values. Any missing, extra, malformed, mismatched, or unsafe state is
    treated as ``EVIDENCE_BUNDLE_PARTIAL_OR_CORRUPT``.
    """

    try:
        files = _read_bundle_files(directory)
        if set(files) != set(EVIDENCE_BUNDLE_NAMES):
            return (
                "EVIDENCE_BUNDLE_PARTIAL_OR_CORRUPT: bundle file set is incomplete or unexpected."
            )

        manifest_bytes = files["manifest.json"]
        manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
        detached_hash = files["manifest.sha256"].decode("ascii").strip()
        if detached_hash != manifest_hash:
            return "EVIDENCE_BUNDLE_PARTIAL_OR_CORRUPT: manifest checksum mismatch."

        manifest = json.loads(manifest_bytes.decode("utf-8"))
        if not isinstance(manifest, dict) or manifest.get("status") != "captured":
            return "EVIDENCE_BUNDLE_PARTIAL_OR_CORRUPT: manifest is invalid or not captured."
        if any(manifest.get(key) != value for key, value in expected_fields.items()):
            return "EVIDENCE_BUNDLE_PARTIAL_OR_CORRUPT: critical correlation mismatch."

        artifact_rows = manifest.get("artifacts")
        if not isinstance(artifact_rows, list) or len(artifact_rows) != len(
            EVIDENCE_ARTIFACT_NAMES
        ):
            return "EVIDENCE_BUNDLE_PARTIAL_OR_CORRUPT: manifest artifact index is incomplete."
        indexed: dict[str, Mapping[str, Any]] = {}
        for row in artifact_rows:
            if not isinstance(row, Mapping):
                return "EVIDENCE_BUNDLE_PARTIAL_OR_CORRUPT: manifest artifact entry is invalid."
            filename = row.get("filename")
            if not isinstance(filename, str) or filename in indexed:
                return "EVIDENCE_BUNDLE_PARTIAL_OR_CORRUPT: manifest artifact filename is invalid."
            indexed[filename] = row
        if set(indexed) != set(EVIDENCE_ARTIFACT_NAMES):
            return (
                "EVIDENCE_BUNDLE_PARTIAL_OR_CORRUPT: manifest does not index the complete bundle."
            )

        for filename in EVIDENCE_ARTIFACT_NAMES:
            row = indexed[filename]
            if row.get("semantic_role") != _ARTIFACT_ROLES[filename]:
                return "EVIDENCE_BUNDLE_PARTIAL_OR_CORRUPT: artifact role mismatch."
            content = files[filename]
            if row.get("size_bytes") != len(content):
                return "EVIDENCE_BUNDLE_PARTIAL_OR_CORRUPT: artifact size mismatch."
            if row.get("sha256") != hashlib.sha256(content).hexdigest():
                return "EVIDENCE_BUNDLE_PARTIAL_OR_CORRUPT: artifact hash mismatch."

        try:
            request_json = json.loads(files["request.json"].decode("utf-8"))
            transport_json = json.loads(files["response_transport.json"].decode("utf-8"))
            response_json = json.loads(files["response.json"].decode("utf-8"))
            actions_json = json.loads(files["web_actions.json"].decode("utf-8"))
        except (UnicodeError, ValueError, TypeError):
            return "EVIDENCE_BUNDLE_PARTIAL_OR_CORRUPT: JSON artifact is malformed."
        if not isinstance(request_json, Mapping):
            return "EVIDENCE_BUNDLE_PARTIAL_OR_CORRUPT: request artifact is invalid."
        if not isinstance(transport_json, Mapping) or not isinstance(response_json, Mapping):
            return "EVIDENCE_BUNDLE_PARTIAL_OR_CORRUPT: response artifact is invalid."
        if not isinstance(actions_json, list):
            return "EVIDENCE_BUNDLE_PARTIAL_OR_CORRUPT: web-action artifact is invalid."
        if transport_json.get("provider_request_id") != manifest.get("provider_request_id"):
            return "EVIDENCE_BUNDLE_PARTIAL_OR_CORRUPT: provider request correlation mismatch."
        if manifest.get("request_sha256") != indexed["request.json"].get("sha256"):
            return "EVIDENCE_BUNDLE_PARTIAL_OR_CORRUPT: request hash linkage mismatch."
        if manifest.get("response_transport_sha256") != transport_json.get(
            "response_transport_sha256"
        ):
            return "EVIDENCE_BUNDLE_PARTIAL_OR_CORRUPT: transport hash linkage mismatch."
        if response_json.get("id") != manifest.get("provider_response_id"):
            return "EVIDENCE_BUNDLE_PARTIAL_OR_CORRUPT: provider response correlation mismatch."
        action_counts = {
            "search": 0,
            "open_page": 0,
            "find_in_page": 0,
            "unknown": 0,
        }
        for row in actions_json:
            if not isinstance(row, Mapping) or row.get("action_type") not in action_counts:
                return "EVIDENCE_BUNDLE_PARTIAL_OR_CORRUPT: web-action entry is invalid."
            action_counts[row["action_type"]] += 1
        if manifest.get("web_tool_call_count") != len(actions_json):
            return "EVIDENCE_BUNDLE_PARTIAL_OR_CORRUPT: web-tool count linkage mismatch."
        if manifest.get("web_action_counts") != action_counts:
            return "EVIDENCE_BUNDLE_PARTIAL_OR_CORRUPT: web-action count linkage mismatch."
        if (
            manifest.get("response_json_sha256")
            != hashlib.sha256(files["response.json"]).hexdigest()
        ):
            return "EVIDENCE_BUNDLE_PARTIAL_OR_CORRUPT: response JSON hash mismatch."
        return None
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return "EVIDENCE_BUNDLE_PARTIAL_OR_CORRUPT: evidence could not be verified."


def sanitize_evidence_structure(value: Any, *, _key: str | None = None) -> Any:
    """Recursively redact credential-like keys and values before persistence."""

    if _key is not None and _is_sensitive_key(_key):
        return REDACTED
    if isinstance(value, Mapping):
        return {
            str(key): sanitize_evidence_structure(item, _key=str(key))
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [sanitize_evidence_structure(item) for item in value]
    if isinstance(value, str):
        return _BEARER_RE.sub(f"Bearer {REDACTED}", _OPENAI_KEY_RE.sub(REDACTED, value))
    if value is None or isinstance(value, bool | int | float):
        return value
    return _BEARER_RE.sub(f"Bearer {REDACTED}", str(value))


def _is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    return normalized in _SENSITIVE_KEYS or normalized.endswith("_token")


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _secure_directory_flags() -> int:
    missing = [name for name in _POSIX_REQUIRED_FLAGS if not hasattr(os, name)]
    if missing:
        raise EvidenceSecurityError("POSIX no-follow directory operations are unavailable.")
    flags = os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0)) | int(getattr(os, "O_NOFOLLOW", 0))
    flags |= int(getattr(os, "O_CLOEXEC", 0))
    return flags


def _open_secure_directory(path: Path) -> int:
    """Open every absolute path component with O_DIRECTORY|O_NOFOLLOW."""

    if os.name != "posix":
        raise ValueError("Secure directory descriptors require POSIX.")
    absolute = _absolute_path(path)
    fd = os.open(os.sep, _secure_directory_flags())
    try:
        for component in absolute.parts:
            if component in (absolute.anchor, "", "."):
                continue
            next_fd = os.open(component, _secure_directory_flags(), dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except OSError as exc:
        os.close(fd)
        if exc.errno in _POSIX_SECURITY_ERRNOS:
            raise EvidenceSecurityError(
                "Evidence path contains a missing or unsafe directory."
            ) from exc
        raise


def _open_or_create_secure_directory(path: Path, mode: int) -> int:
    if os.name != "posix":
        raise EvidenceSecurityError("Secure directory descriptors require POSIX.")
    absolute = _absolute_path(path)
    if not absolute.name:
        raise EvidenceSecurityError("Evidence root must be a named directory.")
    parent_fd = _open_secure_directory(absolute.parent)
    try:
        try:
            fd = os.open(absolute.name, _secure_directory_flags(), dir_fd=parent_fd)
        except FileNotFoundError:
            with suppress(FileExistsError):
                os.mkdir(absolute.name, mode, dir_fd=parent_fd)
            try:
                fd = os.open(absolute.name, _secure_directory_flags(), dir_fd=parent_fd)
            except OSError as exc:
                if exc.errno in _POSIX_SECURITY_ERRNOS:
                    raise EvidenceSecurityError("Evidence root is missing or unsafe.") from exc
                raise
        except OSError as exc:
            if exc.errno in _POSIX_SECURITY_ERRNOS:
                raise EvidenceSecurityError("Evidence root is missing or unsafe.") from exc
            raise
        _verify_fd_mode(fd, mode)
        return fd
    finally:
        os.close(parent_fd)


def _read_bundle_files(directory: Path) -> dict[str, bytes]:
    absolute = _absolute_path(directory)
    if os.name == "posix":
        dir_fd = _open_secure_directory(absolute)
        try:
            _verify_fd_mode(dir_fd, 0o700)
            names = os.listdir(dir_fd)
            if any(name not in EVIDENCE_BUNDLE_NAMES for name in names):
                raise ValueError("Unexpected evidence bundle artifact.")
            return {name: _read_secure_file_at(dir_fd, name) for name in EVIDENCE_BUNDLE_NAMES}
        finally:
            os.close(dir_fd)

    if absolute.is_symlink() or not absolute.is_dir():
        raise ValueError("Evidence bundle directory is missing or unsafe.")
    names = [entry.name for entry in absolute.iterdir()]
    if any(name not in EVIDENCE_BUNDLE_NAMES for name in names):
        raise ValueError("Unexpected evidence bundle artifact.")
    files: dict[str, bytes] = {}
    for name in EVIDENCE_BUNDLE_NAMES:
        path = absolute / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("Evidence artifact is missing or unsafe.")
        files[name] = path.read_bytes()
    return files


def _read_secure_file_at(directory_fd: int, name: str) -> bytes:
    flags = os.O_RDONLY | int(getattr(os, "O_NOFOLLOW", 0))
    flags |= int(getattr(os, "O_CLOEXEC", 0))
    fd = os.open(name, flags, dir_fd=directory_fd)
    try:
        info = os.fstat(fd)
        if not stat_module.S_ISREG(info.st_mode) or stat_module.S_IMODE(info.st_mode) != 0o600:
            raise ValueError("Evidence artifact permissions or type are unsafe.")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _verify_fd_mode(fd: int, mode: int) -> None:
    actual = stat_module.S_IMODE(os.fstat(fd).st_mode)
    if actual != mode:
        raise EvidenceSecurityError("Evidence permissions are not restrictive enough.")


def _verify_posix_mode(path: Path, mode: int) -> None:
    """Enforce restrictive modes on POSIX; Windows remains test-compatible."""

    if os.name != "posix":
        return
    try:
        actual = stat_module.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        raise EvidenceSecurityError("Could not establish secure evidence permissions.") from exc
    if actual != mode:
        raise EvidenceSecurityError("Evidence permissions are not restrictive enough.")
