import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("INTERCOM_TOKEN", "fake-token-for-tests")

import intercom_slack_report as report  # noqa: E402


# ---------------- format_age ----------------

def test_format_age_under_an_hour():
    assert report.format_age(90) == "1m"


def test_format_age_hours_and_minutes():
    assert report.format_age(3661) == "1h 1m"


def test_format_age_days_and_hours():
    assert report.format_age(90000) == "1d 1h"


def test_format_age_none_is_unknown():
    assert report.format_age(None) == "unknown"


# ---------------- determine_queue_status ----------------

def _item(priority="normal", waiting_seconds=0):
    return {"priority": priority, "waiting_seconds": waiting_seconds}


def test_status_green_when_under_all_thresholds():
    items = [_item() for _ in range(5)]
    status, reason = report.determine_queue_status(items)
    assert status == "green"


def test_status_red_when_too_many_urgent():
    items = [_item(priority="urgent") for _ in range(report.THRESHOLDS["red_urgent_high_count"] + 1)]
    status, reason = report.determine_queue_status(items)
    assert status == "red"
    assert "urgent" in reason


def test_status_red_when_total_unassigned_too_high():
    items = [_item() for _ in range(report.THRESHOLDS["red_total_unassigned"] + 1)]
    status, reason = report.determine_queue_status(items)
    assert status == "red"


def test_status_orange_when_many_old_tickets():
    two_day_seconds = report.THRESHOLDS["orange_min_age_days"] * 86400
    items = [_item(waiting_seconds=two_day_seconds + 1)
             for _ in range(report.THRESHOLDS["orange_old_ticket_count"] + 1)]
    status, reason = report.determine_queue_status(items)
    assert status == "orange"


# ---------------- build_topic_breakdown_from_tags ----------------

def test_topic_breakdown_counts_tags_and_untagged():
    # TOPIC_LABELS now has real defaults out of the box (billing -> "Billing
    # / seat management", etc.) so raw tags map to friendly names here.
    items = [
        {"tags": ["billing"]},
        {"tags": ["billing", "bug"]},
        {"tags": []},
    ]
    counts, untagged = report.build_topic_breakdown_from_tags(items)
    assert counts["Billing / seat management"] == 2
    assert counts["Technical/product bugs"] == 1
    assert untagged == 1


def test_topic_breakdown_applies_friendly_labels():
    original = dict(report.TOPIC_LABELS)
    report.TOPIC_LABELS["billing"] = "Billing / seat management"
    try:
        items = [{"tags": ["billing"]}]
        counts, _ = report.build_topic_breakdown_from_tags(items)
        assert counts["Billing / seat management"] == 1
    finally:
        report.TOPIC_LABELS.clear()
        report.TOPIC_LABELS.update(original)


# ---------------- build_summary_text ----------------

def test_summary_text_empty_queue():
    text = report.build_summary_text([], {}, 0, 0, "green")
    assert "No unassigned tickets" in text


def test_summary_text_flags_old_tickets():
    two_day_seconds = report.THRESHOLDS["orange_min_age_days"] * 86400
    items = [_item(waiting_seconds=two_day_seconds + 1)]
    text = report.build_summary_text(items, {}, 0, two_day_seconds + 1, "orange")
    assert "waiting" in text and "reviewed" in text


# ---------------- extract_summary ----------------

def test_extract_summary_pulls_expected_fields():
    conv = {
        "id": "abc123",
        "created_at": 1_700_000_000,
        "waiting_since": 1_700_000_500,
        "custom_attributes": {"Priority": "urgent"},
        "source": {"subject": "Help!", "body": "<p>I need help</p>", "author": {"type": "user"}},
        "state": "open",
        "admin_assignee_id": 0,
        "team_assignee_id": 0,
        "tags": {"tags": [{"name": "billing"}]},
    }
    item = report.extract_summary(conv)
    assert item["id"] == "abc123"
    assert item["priority"] == "urgent"
    assert item["subject"] == "Help!"
    assert item["tags"] == ["billing"]
    assert item["waiting_seconds"] is not None and item["waiting_seconds"] > 0


# ---------------- format_report_text (smoke test) ----------------

def test_format_report_text_includes_key_sections():
    fake_report = {
        "queue_status": "green",
        "queue_status_reason": "within normal thresholds",
        "total_unassigned": 3,
        "oldest_waiting_display": "1d 2h",
        "topic_breakdown": {"billing": 2},
        "untagged_count": 1,
        "urgent_high_count": 0,
        "urgent_high_tickets": [],
        "summary": "All quiet.",
        "daily_update_url": None,
        "daily_update_label": None,
        "median_first_response_display": "2h 15m",
        "median_first_response_window_hours": 24,
    }
    text = report.format_report_text(fake_report)
    assert "Queue Status: GREEN" in text
    assert "Unassigned chats: 3" in text
    assert "billing: 2" in text
    assert "All quiet." in text
    assert "Median first response time (last 24h): 2h 15m" in text


# ---------------- median_of ----------------

def test_median_of_empty_is_none():
    assert report.median_of([]) is None


def test_median_of_odd_count():
    assert report.median_of([300, 100, 200]) == 200


def test_median_of_even_count_averages_middle_two():
    assert report.median_of([100, 200, 300, 400]) == 250


# ---------------- classify_with_claude taxonomy sanitization ----------------

def test_classify_with_claude_uses_fixed_taxonomy_prompt(monkeypatch):
    """When TOPIC_TAXONOMY is set, the prompt sent to Claude must list the
    exact allowed labels, and any label the model returns that ISN'T in the
    taxonomy must be bucketed as 'Other' rather than kept as a new one-off
    label."""
    monkeypatch.setattr(report, "ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.setattr(report, "TOPIC_TAXONOMY", ["Billing / seat management", "Technical/product bugs"])

    captured_prompt = {}

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            '{"tickets": ['
                            '{"id": "1", "topics": ["Billing / seat management"]}, '
                            '{"id": "2", "topics": ["Something Claude made up"]}'
                            '], "summary": "Mostly billing."}'
                        ),
                    }
                ]
            }

    def fake_post(url, headers=None, json=None, timeout=None):
        captured_prompt["text"] = json["messages"][0]["content"]
        return FakeResponse()

    monkeypatch.setattr(report.SESSION, "post", fake_post)

    items = [
        {"id": "1", "subject": "Can't see my invoice", "snippet": "billing issue",
         "priority": "normal", "waiting_seconds": 100},
        {"id": "2", "subject": "App crashes", "snippet": "bug report",
         "priority": "normal", "waiting_seconds": 200},
    ]

    result = report.classify_with_claude(items)
    assert result is not None
    topic_counts, untagged, summary = result

    # Prompt must have included the exact taxonomy labels
    assert "Billing / seat management" in captured_prompt["text"]
    assert "Technical/product bugs" in captured_prompt["text"]

    # A label Claude invented outside the taxonomy gets sanitized to "Other"
    assert topic_counts.get("Other") == 1
    assert topic_counts.get("Billing / seat management") == 1
    assert "Something Claude made up" not in topic_counts
