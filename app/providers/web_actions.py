"""Shared OpenAI web-action classification.

The provider adapter and the optional evidence sink must use the same result
when interpreting ``web_search_call`` output items.  Keeping the parser here
prevents the persisted evidence from drifting away from accounting counters.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_KNOWN_ACTION_TYPES = frozenset({"search", "open_page", "find_in_page"})


@dataclass(frozen=True)
class WebAction:
    """One ordered, sanitized web-tool action."""

    output_index: int
    web_call_ordinal: int
    action_type: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "output_index": self.output_index,
            "web_call_ordinal": self.web_call_ordinal,
            "action_type": self.action_type,
        }


@dataclass(frozen=True)
class WebActionClassification:
    """Ordered actions and their accounting counters."""

    ordered_actions: tuple[WebAction, ...]
    web_tool_call_count: int | None
    search_action_count: int | None
    open_page_action_count: int | None
    find_in_page_action_count: int | None
    unknown_web_action_count: int | None

    @property
    def action_counts(self) -> dict[str, int]:
        """Return concrete counts for the known action labels."""

        return {
            "search": sum(row.action_type == "search" for row in self.ordered_actions),
            "open_page": sum(row.action_type == "open_page" for row in self.ordered_actions),
            "find_in_page": sum(row.action_type == "find_in_page" for row in self.ordered_actions),
            "unknown": sum(row.action_type == "unknown" for row in self.ordered_actions),
        }


def classify_openai_web_actions(output: Any) -> WebActionClassification:
    """Classify web-search output items while preserving their original order.

    Missing/non-list output means that the provider exposed no per-item web
    breakdown, so counters remain ``None`` to preserve existing adapter
    semantics.  Once a ``web_search_call`` item exists, every such item is
    counted and exactly one of the four action buckets is assigned.
    """

    if not isinstance(output, list):
        return WebActionClassification((), None, None, None, None, None)

    rows: list[WebAction] = []
    for output_index, item in enumerate(output):
        if not isinstance(item, dict) or item.get("type") != "web_search_call":
            continue
        action = item.get("action")
        action_type = action.get("type") if isinstance(action, dict) else None
        if not isinstance(action_type, str) or action_type not in _KNOWN_ACTION_TYPES:
            action_type = "unknown"
        rows.append(
            WebAction(
                output_index=output_index,
                web_call_ordinal=len(rows) + 1,
                action_type=action_type,
            )
        )

    if not rows:
        return WebActionClassification((), None, None, None, None, None)

    counts = {
        "search": sum(row.action_type == "search" for row in rows),
        "open_page": sum(row.action_type == "open_page" for row in rows),
        "find_in_page": sum(row.action_type == "find_in_page" for row in rows),
        "unknown": sum(row.action_type == "unknown" for row in rows),
    }
    return WebActionClassification(
        ordered_actions=tuple(rows),
        web_tool_call_count=len(rows),
        search_action_count=counts["search"],
        open_page_action_count=counts["open_page"],
        find_in_page_action_count=counts["find_in_page"],
        unknown_web_action_count=counts["unknown"],
    )
