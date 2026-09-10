"""Tests for the shared OpenAI web-action classifier."""

from __future__ import annotations

from app.providers.web_actions import classify_openai_web_actions


def test_classifier_preserves_order_and_returns_all_counters() -> None:
    result = classify_openai_web_actions(
        [
            {"type": "message"},
            {"type": "web_search_call", "action": {"type": "open_page"}},
            {"type": "web_search_call", "action": {"type": "search"}},
            {"type": "web_search_call", "action": {"type": "find_in_page"}},
            {"type": "web_search_call", "action": {"type": "future_action"}},
            {"type": "web_search_call"},
        ]
    )

    assert [row.action_type for row in result.ordered_actions] == [
        "open_page",
        "search",
        "find_in_page",
        "unknown",
        "unknown",
    ]
    assert [row.output_index for row in result.ordered_actions] == [1, 2, 3, 4, 5]
    assert [row.web_call_ordinal for row in result.ordered_actions] == [1, 2, 3, 4, 5]
    assert result.web_tool_call_count == 5
    assert result.search_action_count == 1
    assert result.open_page_action_count == 1
    assert result.find_in_page_action_count == 1
    assert result.unknown_web_action_count == 2
    assert result.action_counts == {
        "search": 1,
        "open_page": 1,
        "find_in_page": 1,
        "unknown": 2,
    }


def test_classifier_keeps_missing_output_distinct_from_zero_actions() -> None:
    result = classify_openai_web_actions(None)

    assert result.ordered_actions == ()
    assert result.web_tool_call_count is None
    assert result.search_action_count is None
    assert result.action_counts == {"search": 0, "open_page": 0, "find_in_page": 0, "unknown": 0}
