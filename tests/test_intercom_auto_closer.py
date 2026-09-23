import json
import sys
import os
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import intercom_auto_closer as ac  # noqa: E402


# ---------------- strip_html ----------------

def test_strip_html_removes_tags_and_collapses_whitespace():
    html = "<p>Hi   there,</p>\n<p>thanks!</p>"
    assert ac.strip_html(html) == "Hi there, thanks!"


def test_strip_html_handles_empty_and_none():
    assert ac.strip_html("") == ""
    assert ac.strip_html(None) == ""


# ---------------- conversation_qualifies (the core matching logic) ----------------

MARKER = "I just wanted to follow up in case you missed my previous message"


def test_qualifies_when_admin_sent_marker_last():
    body = f"Hey! {MARKER}. Let me know!"
    assert ac.conversation_qualifies(body, "admin", MARKER) is True


def test_does_not_qualify_when_customer_replied():
    body = f"{MARKER}"
    assert ac.conversation_qualifies(body, "user", MARKER) is False


def test_does_not_qualify_without_marker():
    body = "Just checking in, any update?"
    assert ac.conversation_qualifies(body, "admin", MARKER) is False


# ---------------- resolve_teammate_settings ----------------

def test_resolve_teammate_settings_uses_shared_default_template():
    config = {
        "default_follow_up_marker": MARKER,
        "default_closing_message_template": "Bye {name} from {team}",
    }
    teammate = {"key": "nitzan", "name": "Nitzan", "team": "Figma Weave Support", "admin_id": "1"}
    marker, message = ac.resolve_teammate_settings(teammate, config)
    assert marker == MARKER
    assert message == "Bye Nitzan from Figma Weave Support"


def test_resolve_teammate_settings_allows_per_teammate_override():
    config = {
        "default_follow_up_marker": MARKER,
        "default_closing_message_template": "Bye {name} from {team}",
    }
    teammate = {
        "key": "dana",
        "name": "Dana",
        "team": "Support",
        "admin_id": "2",
        "follow_up_marker": "custom marker",
        "closing_message": "A fully custom closing message",
    }
    marker, message = ac.resolve_teammate_settings(teammate, config)
    assert marker == "custom marker"
    assert message == "A fully custom closing message"


# ---------------- format_slack_summary ----------------

def test_format_slack_summary_counts_and_dry_run_note():
    results = [
        {"id": "1", "name": "Alice", "hours_waiting": 10.0, "link": "http://x/1", "status": "sent + closed"},
        {"id": "2", "name": "Bob", "hours_waiting": 5.0, "link": "http://x/2", "status": "FAILED: boom"},
    ]
    text = ac.format_slack_summary("Nitzan", results, dry_run=False)
    assert "Closed: 1" in text
    assert "Failed: 1" in text
    assert "DRY RUN" not in text

    dry_text = ac.format_slack_summary("Nitzan", [], dry_run=True)
    assert "DRY RUN" in dry_text
    assert "No tickets matched" in dry_text


# ---------------- load_webhooks ----------------

def test_load_webhooks_parses_json_env(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOKS_JSON", json.dumps({"nitzan": "https://hooks.slack.com/abc"}))
    assert ac.load_webhooks() == {"nitzan": "https://hooks.slack.com/abc"}


def test_load_webhooks_missing_env_returns_empty(monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOKS_JSON", raising=False)
    assert ac.load_webhooks() == {}


# ---------------- find_stale_conversations (mocked session, no real network) ----------------

def _mock_response(json_data, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status.return_value = None
    return resp


def test_find_stale_conversations_matches_last_admin_nudge():
    session = MagicMock()

    search_response = _mock_response({
        "conversations": [{"id": "111", "contacts": {"contacts": []}}],
        "pages": {},
    })

    conv_detail_response = _mock_response({
        "conversation_parts": {
            "conversation_parts": [
                {
                    "part_type": "comment",
                    "author": {"type": "admin"},
                    "body": f"<p>{MARKER}</p>",
                    "created_at": 1_700_000_000,
                }
            ]
        }
    })

    # session.post is used for the search call, session.get for the detail call
    session.post.return_value = search_response
    session.get.return_value = conv_detail_response

    matches = ac.find_stale_conversations(session, admin_id="42", marker=MARKER, debug=False)

    assert len(matches) == 1
    assert matches[0]["id"] == "111"


def test_find_stale_conversations_skips_when_customer_replied():
    session = MagicMock()

    search_response = _mock_response({
        "conversations": [{"id": "222", "contacts": {"contacts": []}}],
        "pages": {},
    })
    conv_detail_response = _mock_response({
        "conversation_parts": {
            "conversation_parts": [
                {
                    "part_type": "comment",
                    "author": {"type": "admin"},
                    "body": MARKER,
                    "created_at": 1_700_000_000,
                },
                {
                    "part_type": "comment",
                    "author": {"type": "user"},
                    "body": "actually I still need help",
                    "created_at": 1_700_000_500,
                },
            ]
        }
    })
    session.post.return_value = search_response
    session.get.return_value = conv_detail_response

    matches = ac.find_stale_conversations(session, admin_id="42", marker=MARKER, debug=False)

    assert matches == []
