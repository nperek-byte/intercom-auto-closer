#!/usr/bin/env python3
"""
Intercom Unassigned Queue Report -> Slack
------------------------------------------
Pulls all OPEN Intercom conversations sitting in the Unassigned inbox
(no admin AND no team assigned), analyzes them (priority, topic tags,
waiting time), builds a formatted report, and posts it to a Slack
channel via an Incoming Webhook.

Designed to be run on a schedule (cron / GitHub Actions) 4x/day with zero
manual steps.

REQUIRED ENVIRONMENT VARIABLES (never hardcode these):
    INTERCOM_TOKEN       - Intercom API access token
    SLACK_WEBHOOK_URL    - Slack webhook URL for the target channel

OPTIONAL ENVIRONMENT VARIABLES:
    ANTHROPIC_API_KEY        - Claude API key, used to classify tickets into
                               topics and write the summary. If not set, the
                               script falls back to counting Intercom tags
                               instead (less nuanced, but free/offline).
    INTERCOM_PRIORITY_ATTR   - API key of your custom "priority" attribute
                               (default: "Priority")
    INTERCOM_API_VERSION     - Intercom API version header (default: "2.11")
    NOTION_TOKEN             - Notion internal integration token. If not set,
                               the report is posted without the daily update
                               link (that section is just skipped).
    NOTION_DAILY_UPDATES_DATA_SOURCE_ID
                             - The "Daily Updates" data source ID (the
                               collection://... ID from the database). Find
                               it by opening the database in Notion and
                               fetching it, or ask whoever set this up.

USAGE:
    python intercom_slack_report.py --list-attributes   # discover attribute keys
    python intercom_slack_report.py                     # fetch, analyze, post to Slack
    python intercom_slack_report.py --dry-run            # print report, don't post
    python intercom_slack_report.py --save report.json   # also save raw JSON

CUSTOMIZATION:
    - THRESHOLDS dict below controls the red/orange/green rules.
    - TOPIC_LABELS dict lets you map raw Intercom tag names to nicer display
      names in the report (e.g. "billing" -> "Billing / seat management").
      Tags not in this map are shown using their raw tag name.

NOTE ON "UNASSIGNED":
    A conversation can have admin_assignee_id == 0 (no individual admin)
    while still being routed to a team (team_assignee_id != 0) - e.g. a
    "Support" team inbox that every new conversation lands in by default.
    Those are NOT the same as a ticket sitting in the actual Unassigned
    inbox. This script filters on BOTH admin_assignee_id == 0 AND
    team_assignee_id == 0 so it only counts conversations nobody -
    person or team - has claimed. If you ever want the looser definition
    (no individual admin, regardless of team), set REQUIRE_NO_TEAM = False
    below.

    Also note: the "open" boolean on a conversation is true for BOTH
    "open" and "snoozed" states - only the "state" field ("open",
    "closed", "snoozed") tells them apart. Snoozed tickets are paused
    (waiting on the customer or a deadline) and are excluded from
    Intercom's own Unassigned inbox count, so this script filters on
    state == "open" rather than the open boolean, to match what you see
    in the Intercom sidebar.
    Reference: https://developers.intercom.com/docs/references/2.11/rest-api/api.intercom.io/conversations

NOTE ON "WAITING SINCE":
    Uses Intercom's own top-level `waiting_since` field on the conversation
    object (returned directly in list/search results - no extra fetch
    needed): "the time a customer started waiting for a response," null if
    the last reply was from an admin. Age/thresholds are based on this, not
    on when the ticket was originally created.
    Reference: https://developers.intercom.com/docs/references/2.8/rest-api/api.intercom.io/conversations
"""

import os
import sys
import json
import argparse
from datetime import datetime, timezone
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def build_session():
    """
    A requests Session that automatically retries transient failures (rate
    limits and server errors) with exponential backoff, instead of letting
    one 429/500 kill the whole run. Same pattern used by the auto-closer.
    """
    session = requests.Session()
    retry = Retry(
        total=5,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


SESSION = build_session()

# ---------------- CONFIG ----------------
INTERCOM_TOKEN = os.environ.get("INTERCOM_TOKEN")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

PRIORITY_ATTRIBUTE = os.environ.get("INTERCOM_PRIORITY_ATTR", "Priority")
INTERCOM_API_VERSION = os.environ.get("INTERCOM_API_VERSION", "2.11")
BASE_URL = "https://api.intercom.io"

# If True (default), a conversation only counts as "unassigned" when NEITHER
# an admin NOR a team is assigned to it - i.e. it's actually sitting in the
# Unassigned inbox. Set to False if you instead want "no individual admin,
# even if it's parked in a team's queue".
REQUIRE_NO_TEAM = os.environ.get("INTERCOM_REQUIRE_NO_TEAM", "true").lower() != "false"

# Notion "Daily Updates" database - used to link the latest product update
# in the Slack report. Both optional; if either is missing, that section of
# the report is just skipped.
NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
NOTION_DAILY_UPDATES_DATA_SOURCE_ID = os.environ.get(
    "NOTION_DAILY_UPDATES_DATA_SOURCE_ID", "d14997b6-76e4-4722-a219-d1e4403ac496"
)
NOTION_API_VERSION = "2025-09-03"

# Values in your Priority attribute that count as "urgent/high" for the
# red-alert rule. Edit to match your actual attribute values.
URGENT_PRIORITY_VALUES = {"urgent", "high"}

# Queue status thresholds - edit these to tune the rules.
THRESHOLDS = {
    "red_urgent_high_count": 10,     # more than this many urgent/high -> RED
    "red_total_unassigned": 50,      # more than this many total unassigned -> RED
    "orange_min_age_days": 2,        # "oldest ticket waiting time" threshold for orange
    "orange_old_ticket_count": 20,   # more than this many tickets that old -> ORANGE
}

# Optional: map raw Intercom tag names -> friendly display labels.
# Anything not listed here just shows up using its raw tag name.
# Only used by the tag-based fallback (when TOPIC_TAXONOMY below is empty,
# or ANTHROPIC_API_KEY isn't set).
TOPIC_LABELS = {
    "billing": "Billing / seat management",
    "access": "Account & access provisioning",
    "pricing": "Pricing/plan inquiries (leads)",
    "bug": "Technical/product bugs",
    "misrouted-lead": "Leads misrouted to support",
}

# Fixed set of topics for Claude to classify tickets into (content-based,
# not Intercom's built-in topic AI). When this list is non-empty, Claude
# must pick from EXACTLY these labels (or "Other") instead of inventing new
# wording each run - this is what keeps topic names consistent across
# reports so trends are actually comparable over time.
#
# Pulled directly from Intercom -> Reports -> Conversational Insights ->
# Topics (app.intercom.com/a/apps/siifiun4/reports/conversational-insights/topics)
# as of 2026-09-23. TOPIC_HINTS below carries a couple of the example
# phrases Intercom itself associates with each topic, passed to Claude as
# classification hints - this is what should make classification MORE
# reliable than Intercom's own topic AI, since Claude sees the actual
# ticket content plus these anchor phrases, rather than a black-box model.
# Edit either list any time your real topics change; they don't need to
# stay in sync with what Intercom shows.
TOPIC_HINTS = {
    "Plan Cancellation/ Refund Request": ["cancel subscription", "cancel plan", "canceling it in your account settings"],
    "[Feature Request] Change Email": ["changed email", "want changed email", "no option to change a user's email"],
    "Model stuck / hanging": ["model stuck", "stuck generating", "stuck at 95%", "loading forever", "not progressing"],
    "Credits Missing": ["missing credits", "credits disappeared", "credits gone", "credits showing 0"],
    "Unable to open workflow": ["can't open file", "can't open workflow", "workflow won't open", "file broken"],
    "Enterprise Plan Request": [],
    "Model Content Moderation": ["moderation system", "flag content", "block content", "restricted content", "safety system"],
    "[Feature Request] External API": ["endpoint", "programmatic", "webhook", "mcp", "api"],
    "Unable to run model": ["can't run model", "model won't start", "run failed", "failed running"],
    "Figma handoff": ["to add additional comments, reply to this email or visit"],
    "Fonts": ["fonts", "font"],
    "Error when generating": ["generation error", "failed to generate", "error message when running", "output failed"],
    "Card Declined": ["card declines", "reach out to your card issuer", "reach out to your card provider"],
    "Model Verification System": ["verified models", "unverified models", "verification system", "verified and unverified"],
    "Educational Plan": ["special plan for individual students", "supporting educational institutions"],
    "Credits Rollover": ["credits roll over"],
    "Model Output Dissatisfaction": ["each model has its own strengths and weaknesses", "quality of the generation depends"],
    "Data Privacy & Security": ["privacy and data security", "data protection", "gdpr compliance"],
    "[Feature Request] Custom Fonts": ["add custom fonts", "you are the owner of the fonts"],
    "Login Issues": ["login issue", "can't login", "login not working", "login error", "can't access"],
    "Imported Model - Free Users": ["imported models are available", "model import is available on starter"],
    "[Feature Request] Admin-Only Seat": ["admin only", "only paying", "active user as an admin", "paid seat on the team"],
    "Video Models - Free Users": ["video models are only available on starter"],
    "[Feature Request] Multiplayer Editing": ["edit the same file at the same time", "same time in the same file"],
    "Yearly to Monthly Plan Change": ["yearly to monthly", "change annual monthly"],
    "Service outage / System down": ["unable to load", "outage", "everything is broken"],
    "[Feature Request] JSON Import/Export": ["export json", "import json", "download json", "json prompt"],
    "How to log out": ["how to logout", "how to log out", "how to sign out"],
}
TOPIC_TAXONOMY = list(TOPIC_HINTS.keys())

# Rolling window (hours) used to compute median first response time. This
# looks at ALL conversations created in this window, regardless of current
# assignment - it measures team responsiveness, not the current backlog.
MEDIAN_FRT_WINDOW_HOURS = int(os.environ.get("INTERCOM_MEDIAN_FRT_WINDOW_HOURS", "24"))
# -----------------------------------------

HEADERS = {
    "Authorization": f"Bearer {INTERCOM_TOKEN}",
    "Content-Type": "application/json",
    "Intercom-Version": INTERCOM_API_VERSION,
}


def list_conversation_attributes():
    """Print all custom conversation attributes so you can find the right API key."""
    resp = SESSION.get(f"{BASE_URL}/data_attributes", headers=HEADERS,
                         params={"model": "conversation"})
    resp.raise_for_status()
    data = resp.json()
    print("\nCustom conversation attributes available:\n")
    for attr in data.get("data", []):
        if attr.get("custom"):
            print(f"  api_key: {attr['name']:<30} label: {attr.get('label', '')}")
    print("\nUse the 'api_key' value for PRIORITY_ATTRIBUTE / INTERCOM_PRIORITY_ATTR.\n")


def search_unassigned_open_conversations(require_no_team=REQUIRE_NO_TEAM):
    """Fetch all open conversations sitting in the Unassigned inbox.

    By default this means BOTH:
      - admin_assignee_id == 0  (no individual admin claimed it), AND
      - team_assignee_id == 0   (it isn't parked in a team's queue either)

    A conversation with admin_assignee_id == 0 but a non-zero
    team_assignee_id is routed to a team's inbox, not the Unassigned
    inbox - Intercom's own conversation object confirms both fields are
    independent (e.g. "admin_assignee_id": 0, "team_assignee_id": 5017691
    is a valid, team-owned-but-not-individually-claimed state).

    This also filters on state == "open" rather than the open boolean,
    since "open" stays true for snoozed conversations too - those are
    excluded from Intercom's own Unassigned inbox count and were
    inflating the total.

    Set require_no_team=False (or INTERCOM_REQUIRE_NO_TEAM=false) if you
    instead want the looser definition: no individual admin, regardless
    of team.
    """
    url = f"{BASE_URL}/conversations/search"
    filters = [
        # NOTE: the "open" boolean stays true for BOTH open and snoozed
        # conversations - only "state" distinguishes them ("open",
        # "closed", "snoozed"). Snoozed tickets are paused (waiting on the
        # customer/a deadline) and Intercom's own Unassigned inbox count
        # excludes them, so filtering on "open" alone over-counts. Filter
        # on state == "open" instead to match the real Unassigned inbox.
        {"field": "state", "operator": "=", "value": "open"},
        {"field": "admin_assignee_id", "operator": "=", "value": "0"},
    ]
    if require_no_team:
        filters.append({"field": "team_assignee_id", "operator": "=", "value": "0"})

    query = {
        "query": {
            "operator": "AND",
            "value": filters,
        },
        "pagination": {"per_page": 150},
    }

    conversations = []
    while True:
        resp = SESSION.post(url, headers=HEADERS, json=query)
        resp.raise_for_status()
        data = resp.json()
        conversations.extend(data.get("conversations", []))

        next_page = data.get("pages", {}).get("next")
        if not next_page:
            break
        query["pagination"]["starting_after"] = next_page.get("starting_after")

    return conversations


def extract_summary(conv):
    """Pull the fields we actually care about out of a raw conversation object."""
    custom_attrs = conv.get("custom_attributes", {}) or {}
    source = conv.get("source", {}) or {}

    created_at = conv.get("created_at")
    created_iso = (
        datetime.fromtimestamp(created_at, tz=timezone.utc).isoformat()
        if created_at else None
    )

    # Intercom computes this for us directly on the conversation object:
    # "the time a customer started waiting for a response." It's null if
    # the last reply was from an admin (i.e. nobody's currently waiting).
    # Available on list/search results, no extra per-conversation fetch needed.
    waiting_since_ts = conv.get("waiting_since")
    waiting_since_iso = (
        datetime.fromtimestamp(waiting_since_ts, tz=timezone.utc).isoformat()
        if waiting_since_ts else None
    )
    waiting_seconds = None
    if waiting_since_ts:
        waiting_seconds = datetime.now(timezone.utc).timestamp() - waiting_since_ts

    return {
        "id": conv.get("id"),
        "created_at": created_iso,
        "waiting_since": waiting_since_iso,
        "waiting_seconds": waiting_seconds,
        "priority": str(custom_attrs.get(PRIORITY_ATTRIBUTE, "unset")),
        "subject": source.get("subject") or None,
        "snippet": (source.get("body") or "")[:500],
        "author_type": (source.get("author") or {}).get("type"),
        "state": conv.get("state"),
        "admin_assignee_id": conv.get("admin_assignee_id"),
        "team_assignee_id": conv.get("team_assignee_id"),
        "tags": [t.get("name") for t in (conv.get("tags", {}).get("tags", []) or [])],
    }


def format_age(seconds):
    """Turn a duration in seconds into a compact 'Xd Yh' style string."""
    if seconds is None:
        return "unknown"
    total_hours = int(seconds // 3600)
    days, hours = divmod(total_hours, 24)
    if days > 0:
        return f"{days}d {hours}h"
    minutes = int((seconds % 3600) // 60)
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def median_of(values):
    """Pure function: median of a list of numbers. Returns None for an
    empty list. Kept separate from any network calls so it's trivially
    unit-testable."""
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 0:
        return (ordered[mid - 1] + ordered[mid]) / 2
    return ordered[mid]


def compute_median_first_response_time(window_hours=MEDIAN_FRT_WINDOW_HOURS):
    """
    Median time-to-first-admin-reply across ALL conversations created in
    the last `window_hours`, regardless of current assignment - this
    measures team responsiveness, not the current unassigned backlog.

    Uses Intercom's own precomputed statistics.time_to_admin_reply (seconds,
    business-hours-adjusted) rather than recalculating it from raw
    timestamps, since Intercom already does that math for us. Only
    conversations that have actually received a first admin reply are
    included; conversations still fully unanswered are skipped (they'd
    otherwise pull the median toward "no reply yet", which isn't the same
    metric).

    Returns seconds (float) or None if there's nothing to measure.
    Costs one extra GET per matching conversation - fine for a 24h window
    at typical support volumes, but slow for very high-volume inboxes.
    """
    cutoff_ts = int(datetime.now(timezone.utc).timestamp()) - (window_hours * 3600)
    url = f"{BASE_URL}/conversations/search"
    query = {
        "query": {
            "operator": "AND",
            "value": [
                {"field": "created_at", "operator": ">", "value": cutoff_ts},
            ],
        },
        "pagination": {"per_page": 150},
    }

    conversations = []
    while True:
        resp = SESSION.post(url, headers=HEADERS, json=query)
        resp.raise_for_status()
        data = resp.json()
        conversations.extend(data.get("conversations", []))
        next_page = data.get("pages", {}).get("next")
        if not next_page:
            break
        query["pagination"]["starting_after"] = next_page.get("starting_after")

    response_times = []
    for conv in conversations:
        conv_id = conv.get("id")
        try:
            resp = SESSION.get(f"{BASE_URL}/conversations/{conv_id}", headers=HEADERS)
            resp.raise_for_status()
            detail = resp.json()
        except requests.RequestException:
            continue
        stats = detail.get("statistics") or {}
        ttar = stats.get("time_to_admin_reply")
        if ttar is not None:
            response_times.append(ttar)

    return median_of(response_times)


def determine_queue_status(items):
    """Apply the red/orange/green rules. Returns (status, reason)."""
    total = len(items)
    urgent_high_count = sum(
        1 for i in items if i["priority"].lower() in URGENT_PRIORITY_VALUES
    )
    two_day_seconds = THRESHOLDS["orange_min_age_days"] * 86400
    old_ticket_count = sum(
        1 for i in items if (i["waiting_seconds"] or 0) >= two_day_seconds
    )

    if urgent_high_count > THRESHOLDS["red_urgent_high_count"]:
        return "red", f"{urgent_high_count} urgent/high priority tickets unassigned"
    if total > THRESHOLDS["red_total_unassigned"]:
        return "red", f"{total} total unassigned tickets"
    if old_ticket_count > THRESHOLDS["orange_old_ticket_count"]:
        return "orange", (
            f"{old_ticket_count} tickets waiting {THRESHOLDS['orange_min_age_days']}+ days"
        )
    return "green", "within normal thresholds"


def build_topic_breakdown_from_tags(items):
    """Fallback: count tag occurrences across all tickets. A ticket with
    multiple tags is counted once per tag (so totals can exceed ticket
    count). Used only if Claude classification isn't available."""
    counts = {}
    untagged = 0
    for item in items:
        tags = item.get("tags") or []
        if not tags:
            untagged += 1
            continue
        for tag in tags:
            label = TOPIC_LABELS.get(tag, tag)
            counts[label] = counts.get(label, 0) + 1
    sorted_counts = dict(sorted(counts.items(), key=lambda x: x[1], reverse=True))
    return sorted_counts, untagged


def classify_with_claude(items):
    """Use the Claude API to bucket each ticket into 1+ short topic labels
    and write a short human-style summary, similar to how a person skimming
    the queue would categorize things. Returns (topic_counts, summary_text)
    or None if the API isn't configured / call fails, so callers can fall
    back to tag-based counting.

    Ticket text is sent to the Claude API for this purpose only.
    """
    if not ANTHROPIC_API_KEY or not items:
        return None

    tickets_payload = [
        {
            "id": i["id"],
            "subject": i.get("subject") or "",
            "snippet": (i.get("snippet") or "")[:300],
            "priority": i.get("priority"),
            "waiting_display": format_age(i.get("waiting_seconds")),
        }
        for i in items
    ]

    if TOPIC_TAXONOMY:
        # Fixed taxonomy: force Claude to pick from an exact, pre-agreed list
        # instead of inventing new label wording each run. This is what
        # keeps topic names stable across reports so counts/trends are
        # actually comparable over time, unlike Intercom's own topic AI
        # (which can label similar tickets differently from one run to the
        # next). Example phrases (from TOPIC_HINTS, if any) are included as
        # anchors so classification is based on concrete signal, not just
        # the label name alone.
        def _format_topic_line(t):
            hints = TOPIC_HINTS.get(t)
            if hints:
                examples = ", ".join(f'"{h}"' for h in hints[:4])
                return f'  - "{t}" (example phrases: {examples})'
            return f'  - "{t}"'

        taxonomy_list = "\n".join(_format_topic_line(t) for t in TOPIC_TAXONOMY)
        topic_instructions = (
            "1. Assign each ticket ONE topic label, chosen EXACTLY from this fixed list "
            "(copy the text exactly, do not rephrase or invent new labels). Example "
            "phrases are just anchors, not exact-match requirements - classify based on "
            "the actual meaning of the ticket:\n"
            f"{taxonomy_list}\n"
            "  - \"Other\" (only if truly none of the above fit)\n"
            "A ticket may have a second label from the list only if it genuinely spans "
            "two categories - otherwise just one.\n"
        )
    else:
        topic_instructions = (
            "1. Assign each ticket one or more short topic labels (2-4 words each). "
            "A ticket can have more than one label if it genuinely spans categories. "
            "Use consistent, reusable label names across tickets so counts make sense.\n"
        )

    prompt = (
        "You are triaging a support inbox queue. Below is a JSON list of "
        "unassigned support tickets (id, subject, snippet, priority, waiting time).\n\n"
        f"{topic_instructions}"
        "2. Write a short 1-3 sentence plain-English summary of what's going on in "
        "the queue overall, in the voice of a support lead reporting on shift "
        "status (like: \"Mostly account/billing self-service friction, plus a few "
        "product bugs worth an eng look.\").\n\n"
        "Respond with ONLY valid JSON, no other text, in this exact shape:\n"
        '{"tickets": [{"id": "...", "topics": ["..."]}], "summary": "..."}\n\n'
        f"Tickets:\n{json.dumps(tickets_payload, indent=2)}"
    )

    try:
        resp = SESSION.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 1500,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        text = "".join(
            block.get("text", "") for block in data.get("content", [])
            if block.get("type") == "text"
        ).strip()
        # Strip accidental markdown code fences if the model adds them
        text = text.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(text)
    except Exception as e:
        print(f"Claude classification failed, falling back to tags: {e}", file=sys.stderr)
        return None

    allowed = set(TOPIC_TAXONOMY) | {"Other"} if TOPIC_TAXONOMY else None

    topic_counts = {}
    untagged = 0
    for t in parsed.get("tickets", []):
        topics = t.get("topics") or []
        if not topics:
            untagged += 1
            continue
        for topic in topics:
            # Defensive: if a fixed taxonomy is configured, any label that
            # doesn't exactly match it gets bucketed as "Other" rather than
            # silently creating a new one-off label in the report.
            if allowed is not None and topic not in allowed:
                topic = "Other"
            topic_counts[topic] = topic_counts.get(topic, 0) + 1

    sorted_counts = dict(sorted(topic_counts.items(), key=lambda x: x[1], reverse=True))
    summary = parsed.get("summary", "").strip()
    return sorted_counts, untagged, summary


def build_summary_text(items, topic_counts, untagged, oldest_waiting_seconds, status):
    """Rule-based short summary (no external API calls)."""
    total = len(items)
    if total == 0:
        return "No unassigned tickets right now. Queue is clear."

    parts = []
    top_topics = list(topic_counts.items())[:2]
    if top_topics:
        topic_str = " and ".join(f"{name} ({count})" for name, count in top_topics)
        parts.append(f"Most common topics right now: {topic_str}.")

    if untagged:
        parts.append(f"{untagged} ticket(s) have no topic tag.")

    two_day_seconds = THRESHOLDS["orange_min_age_days"] * 86400
    old_count = sum(1 for i in items if (i["waiting_seconds"] or 0) >= two_day_seconds)
    if old_count:
        parts.append(
            f"{old_count} ticket(s) have been waiting {THRESHOLDS['orange_min_age_days']}+ days "
            f"and should be reviewed."
        )

    if status == "green" and not old_count:
        parts.append("No major concerns at this time.")

    return " ".join(parts)


def get_latest_daily_update():
    """
    Fetch the most recent entry from the Notion "Daily Updates" database
    (sorted by Date Added, descending). Returns (page_url, label) or None
    if Notion isn't configured or the lookup fails - callers should just
    skip that section of the report in that case, not fail the whole run.
    """
    if not NOTION_TOKEN or not NOTION_DAILY_UPDATES_DATA_SOURCE_ID:
        return None

    url = f"https://api.notion.com/v1/data_sources/{NOTION_DAILY_UPDATES_DATA_SOURCE_ID}/query"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_API_VERSION,
        "Content-Type": "application/json",
    }
    payload = {
        "sorts": [{"property": "Date Added", "direction": "descending"}],
        "page_size": 1,
    }

    try:
        resp = SESSION.post(url, headers=headers, json=payload, timeout=15)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if not results:
            return None

        page = results[0]
        page_url = page.get("url")

        date_prop = page.get("properties", {}).get("Date Added", {}).get("date") or {}
        date_start = date_prop.get("start")  # e.g. "2026-08-21"
        if date_start:
            dt = datetime.fromisoformat(date_start)
            date_label = dt.strftime("%d/%m/%y")
        else:
            date_label = "latest"

        return page_url, f"Daily Updates - {date_label}"
    except requests.RequestException as e:
        print(f"Notion daily update lookup failed, skipping: {e}", file=sys.stderr)
        return None


def build_report():
    raw = search_unassigned_open_conversations()
    items = [extract_summary(c) for c in raw]

    total = len(items)
    urgent_high = [i for i in items if i["priority"].lower() in URGENT_PRIORITY_VALUES]

    oldest_waiting_seconds = max((i["waiting_seconds"] or 0 for i in items), default=0)
    oldest_item = max(items, key=lambda i: i["waiting_seconds"] or 0, default=None)

    status, status_reason = determine_queue_status(items)

    claude_result = classify_with_claude(items)
    if claude_result:
        topic_counts, untagged, ai_summary = claude_result
        summary = ai_summary or build_summary_text(items, topic_counts, untagged, oldest_waiting_seconds, status)
    else:
        topic_counts, untagged = build_topic_breakdown_from_tags(items)
        summary = build_summary_text(items, topic_counts, untagged, oldest_waiting_seconds, status)

    daily_update = get_latest_daily_update()

    median_frt_seconds = compute_median_first_response_time()

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "require_no_team": REQUIRE_NO_TEAM,
        "total_unassigned": total,
        "urgent_high_count": len(urgent_high),
        "urgent_high_tickets": urgent_high,
        "oldest_waiting_seconds": oldest_waiting_seconds,
        "oldest_waiting_display": format_age(oldest_waiting_seconds),
        "oldest_ticket_subject": (oldest_item or {}).get("subject"),
        "queue_status": status,
        "queue_status_reason": status_reason,
        "topic_breakdown": topic_counts,
        "untagged_count": untagged,
        "summary": summary,
        "median_first_response_seconds": median_frt_seconds,
        "median_first_response_display": (
            format_age(median_frt_seconds) if median_frt_seconds is not None else "no data"
        ),
        "median_first_response_window_hours": MEDIAN_FRT_WINDOW_HOURS,
        "daily_update_url": daily_update[0] if daily_update else None,
        "daily_update_label": daily_update[1] if daily_update else None,
        "conversations": items,
    }
    return report


STATUS_EMOJI = {"red": "🔴", "orange": "🟠", "green": "🟢"}

# The variable name you set up in Slack Workflow Builder's "From a webhook"
# trigger. Must match exactly, or the workflow won't receive the message.
SLACK_WORKFLOW_VARIABLE = os.environ.get("SLACK_WORKFLOW_VARIABLE", "report_text")


def format_report_text(report):
    """Build a single plain-text report for Slack (Workflow Builder inserts
    variables as literal text, so markdown like *bold* won't render there —
    rely on emoji + line structure for visual hierarchy instead)."""
    status = report["queue_status"]
    emoji = STATUS_EMOJI.get(status, "⚪")
    now_str = datetime.now(timezone.utc).strftime("%b %d, %Y · %H:%M UTC")

    lines = []
    lines.append(f"{emoji} Queue Status: {status.upper()}")
    lines.append(f"({report['queue_status_reason']})")
    lines.append("")

    lines.append(f"📊 Unassigned chats: {report['total_unassigned']}")
    lines.append(f"⏳ Oldest ticket waiting since: {report['oldest_waiting_display']}")
    window_h = report.get("median_first_response_window_hours")
    lines.append(
        f"⚡ Median first response time (last {window_h}h): "
        f"{report['median_first_response_display']}"
    )
    lines.append("")

    lines.append("🔍 Any Trends Observed?")
    if report["topic_breakdown"]:
        for name, count in report["topic_breakdown"].items():
            lines.append(f"• {name}: {count}")
        if report["untagged_count"]:
            lines.append(f"• Uncategorized: {report['untagged_count']}")
    else:
        lines.append("• No trends detected.")
    lines.append("")

    lines.append("🚨 Any Critical Chats?")
    if report["urgent_high_count"] > 0:
        lines.append(f"• {report['urgent_high_count']} urgent/high priority — needs attention:")
        for t in report["urgent_high_tickets"][:10]:
            subject = t.get("subject") or "(no subject)"
            lines.append(f"   ◦ {subject} — waiting {format_age(t['waiting_seconds'])}")
    else:
        lines.append("• None — all normal priority.")
    lines.append("")

    lines.append("📝 Any Notes?")
    lines.append(f"• {report['summary']}")
    if report.get("daily_update_url"):
        lines.append(f"• Check out: <{report['daily_update_url']}|{report['daily_update_label']}>")
    lines.append("")
    lines.append(f"Generated {now_str}")

    return "\n".join(lines)


def post_to_slack(report):
    if not SLACK_WEBHOOK_URL:
        sys.exit("SLACK_WEBHOOK_URL is not set. Set it as an environment variable / secret.")

    text = format_report_text(report)
    payload = {SLACK_WORKFLOW_VARIABLE: text}
    resp = SESSION.post(SLACK_WEBHOOK_URL, json=payload)
    resp.raise_for_status()
    print("Posted report to Slack.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--list-attributes", action="store_true",
                         help="List custom conversation attributes and exit")
    parser.add_argument("--save", default=None,
                         help="Also save the raw report JSON to this path")
    parser.add_argument("--dry-run", action="store_true",
                         help="Print the report instead of posting to Slack")
    args = parser.parse_args()

    if not INTERCOM_TOKEN:
        sys.exit("INTERCOM_TOKEN environment variable is not set.")

    if args.list_attributes:
        list_conversation_attributes()
        return

    report = build_report()

    if args.save:
        with open(args.save, "w") as f:
            json.dump(report, f, indent=2)
        print(f"Saved raw report to {args.save}")

    print(f"Unassigned (require_no_team={REQUIRE_NO_TEAM}): {report['total_unassigned']} | "
          f"Status: {report['queue_status']} | "
          f"Oldest waiting: {report['oldest_waiting_display']}")

    if args.dry_run:
        print("\n--- Message that would be posted to Slack ---\n")
        print(format_report_text(report))
    else:
        post_to_slack(report)


if __name__ == "__main__":
    main()
