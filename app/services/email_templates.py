"""Email content templates for Phase 11 notifications.

Small Python functions that render plain-text + simple HTML multipart
email bodies. No frontend template framework is introduced.
"""

from __future__ import annotations

from app.config import get_settings
from app.core.enums import NotificationType, ScanStatus
from app.models.notification import Notification
from app.models.scan import Scan


def _deep_link(notification: Notification) -> str:
    """Build a deep link to the application for a notification."""
    base = get_settings().app_public_base_url.rstrip("/")
    if notification.deep_link_path:
        return f"{base}{notification.deep_link_path}"
    return base


def _project_link(notification: Notification) -> str:
    base = get_settings().app_public_base_url.rstrip("/")
    if notification.project_id:
        return f"{base}/projects/{notification.project_id}"
    return base


def render_email(notification: Notification) -> tuple[str, str, str]:
    """Render email subject, text body, and HTML body for a notification.

    Returns ``(subject, text_body, html_body)``.
    """
    nt = NotificationType(notification.notification_type)
    if nt == NotificationType.SCHEDULED_SCAN_COMPLETED:
        subject = "Scheduled scan completed"
        text = _scheduled_scan_text(notification, "completed successfully")
    elif nt == NotificationType.SCHEDULED_SCAN_PARTIAL:
        subject = "Scheduled scan partially completed"
        text = _scheduled_scan_text(notification, "partially completed")
    elif nt == NotificationType.SCHEDULED_SCAN_FAILED:
        subject = "Scheduled scan failed"
        text = _scheduled_scan_text(notification, "failed")
    elif nt == NotificationType.NEW_HIGH_PRIORITY_OPPORTUNITY:
        subject = "New high-priority opportunity detected"
        text = _high_priority_text(notification)
    elif nt == NotificationType.VERIFICATION_RESOLVED:
        subject = "Verification resolved"
        text = _verification_text(
            notification,
            "The verification measurement no longer met the threshold "
            "that created this opportunity.",
        )
    elif nt == NotificationType.VERIFICATION_IMPROVED:
        subject = "Verification improved"
        text = _verification_text(
            notification,
            "The measured gap improved in the verification measurement.",
        )
    elif nt == NotificationType.VERIFICATION_REGRESSED:
        subject = "Verification regressed"
        text = _verification_text(
            notification,
            "The measured gap increased in the verification measurement.",
        )
    elif nt == NotificationType.VERIFICATION_INCONCLUSIVE:
        subject = "Verification inconclusive"
        text = _verification_text(
            notification,
            "The verification did not have sufficient evidence for a reliable comparison.",
        )
    else:
        subject = notification.title
        text = notification.message

    html = _wrap_html(text, _deep_link(notification))
    return subject, text, html


def _scheduled_scan_text(notification: Notification, status_phrase: str) -> str:
    lines = [
        f"GEO Tracker — Scheduled scan {status_phrase}.",
        "",
        notification.message,
        "",
        f"View project: {_project_link(notification)}",
    ]
    return "\n".join(lines)


def _high_priority_text(notification: Notification) -> str:
    lines = [
        "GEO Tracker — New high-priority opportunity detected.",
        "",
        notification.message,
        "",
        f"View opportunity: {_deep_link(notification)}",
    ]
    return "\n".join(lines)


def _verification_text(notification: Notification, description: str) -> str:
    lines = [
        "GEO Tracker — Verification outcome.",
        "",
        description,
        "",
        notification.message,
        "",
        f"View opportunity: {_deep_link(notification)}",
    ]
    return "\n".join(lines)


def _wrap_html(text_body: str, link: str) -> str:
    """Wrap text body in minimal HTML."""
    escaped = text_body.replace("<", "&lt;").replace(">", "&gt;")
    escaped = escaped.replace("\n", "<br>\n")
    return f"""\
<html>
<body>
<p>{escaped}</p>
<p><a href="{link}">Open GEO Tracker</a></p>
</body>
</html>"""


def build_scheduled_scan_message(
    scan: Scan,
    *,
    measurement_coverage: float | None = None,
    brand_visibility: float | None = None,
    open_opportunities: int | None = None,
) -> str:
    """Build the notification message body for a scheduled scan summary."""
    parts: list[str] = []
    status_label = {
        ScanStatus.COMPLETED: "completed",
        ScanStatus.PARTIAL: "partially completed",
        ScanStatus.FAILED: "failed",
    }.get(scan.status, str(scan.status))

    parts.append(f"Scan {status_label}.")

    if measurement_coverage is not None:
        parts.append(f"Measurement coverage: {measurement_coverage:.1f}%.")
    if brand_visibility is not None:
        parts.append(f"Brand visibility: {brand_visibility:.1f}%.")
    if open_opportunities is not None:
        parts.append(f"Open opportunities: {open_opportunities}.")

    if scan.status == ScanStatus.PARTIAL:
        parts.append("Some measurement requests did not complete.")
    elif scan.status == ScanStatus.FAILED:
        parts.append("The scan did not complete successfully.")

    return " ".join(parts)
