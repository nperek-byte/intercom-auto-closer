#!/usr/bin/env python3
"""
Intercom Auto-Closer -> Slack DM
----------------------------------
Finds each configured teammate's assigned, open Intercom conversations where
the LAST message was THEIR previous "just following up" nudge and the
customer still hasn't replied. For each match, POSTS that teammate's closing
reply to the conversation and CLOSES it in Intercom - then posts a summary
to that teammate's Slack inbox.

This sends real messages to real customers and closes real tickets.
Always run with --debug or --dry-run first when changing anything here.

TEAM SETUP
    Who runs, which conversations they own, and what message they send is
    driven by config/team.yaml (checked into the repo, not secret). To add a
    teammate: add an entry there, then add their Slack webhook URL to the
    SLACK_WEBHOOKS_JSON secret (see README.md). No code changes needed.

REQUIRED ENVIRONMENT VARIABLES (never hardcode these):
    INTERCOM_TOKEN       - Intercom API access token (shared workspace token)
    SLACK_WEBHOOKS_JSON  - JSON object mapping each teammate's config "key"
                           to their personal Slack Workflow Builder webhook
                           URL, e.g. {"nitzan": "https://hooks.slack.com/..."}

OPTIONAL ENVIRONMENT VARIABLES:
    INTERCOM_API_VERSION - Intercom API version header (default: "2.11")

USAGE:
    python intercom_auto_closer.py --list-admins        (one-time, per person,
                                                           to find their admin ID)
    python intercom_auto_closer.py --debug               (see what would match,
                                                           no sending, all teammates)
    python intercom_auto_closer.py --dry-run              (log matches, post dry-run
                                                           Slack summaries, send/close
                                                           nothing)
    python intercom_auto_closer.py --only nitzan          (run just one teammate)
    python intercom_auto_closer.py                        (sends + closes matching
                                                           tickets for everyone in
                                                           config/team.yaml)

OUTPUT:
    Sends the closing reply and closes each matching ticket in Intercom.
    Posts a per-teammate summary to their Slack inbox.
    Saves a single markdown log (intercom_closing_log_YYYY-MM-DD.md) with a
    section per teammate, recording what was sent, to which ticket, and
    whether it succeeded.
"""

import os
import sys
import re
import json
import logging
import argparse
from datetime import datetime, timezone

import requests
import yaml
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------- CONFIG ----------------
INTERCOM_TOKEN = os.environ.get("INTERCOM_TOKEN")
INTERCOM_API_VERSION = os.environ.get("INTERCOM_API_VERSION", "2.11")
BASE_URL = "https://api.intercom.io"
DEFAULT_TEAM_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config", "team.yaml")
# -----------------------------------------

log = logging.getLogger("intercom_auto_closer")


def build_headers():
    return {
        "Authorization": f"Bearer {INTERCOM_TOKEN}",
        "Content-Type": "application/json",
        "Intercom-Version": INTERCOM_API_VERSION,
    }


def build_session():
    """
    A requests Session that automatically retries transient failures
    (rate limits and server errors) with exponential backoff, instead of
    letting one 429/500 kill the whole run.
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


# ---------------- Config loading ----------------

def load_team_config(path):
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    if not data.get("teammates"):
        sys.exit(f"No 'teammates' found in {path} - nothing to do.")
    return data


def resolve_teammate_settings(teammate, config):
    """Fill in a teammate's marker + closing message from their own override,
    or the shared team default."""
    marker = teammate.get("follow_up_marker") or config["default_follow_up_marker"]
    message = teammate.get("closing_message")
    if not message:
        template = config["default_closing_message_template"]
        message = template.format(name=teammate["name"], team=teammate.get("team", ""))
    return marker, message


def load_webhooks():
    raw = os.environ.get("SLACK_WEBHOOKS_JSON")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        sys.exit(f"SLACK_WEBHOOKS_JSON is not valid JSON: {e}")


# ---------------- Intercom helpers ----------------

def list_admins(session):
    """Print all admins/teammates so a person can find their own ID."""
    resp = session.get(f"{BASE_URL}/admins", headers=build_headers())
    resp.raise_for_status()
    data = resp.json()
    print("\nTeammates in this workspace:\n")
    for admin in data.get("admins", []):
        print(f"  id: {admin.get('id'):<15} name: {admin.get('name', '')}  email: {admin.get('email', '')}")
    print("\nUse the 'id' value as 'admin_id' in config/team.yaml.\n")


def search_open_conversations(session, admin_id):
    """Fetch open conversations assigned to admin_id."""
    url = f"{BASE_URL}/conversations/search"
    query = {
        "query": {
            "operator": "AND",
            "value": [
                {"field": "open", "operator": "=", "value": "true"},
                {"field": "admin_assignee_id", "operator": "=", "value": admin_id},
            ],
        },
        "pagination": {"per_page": 150},
    }

    conversations = []
    while True:
        resp = session.post(url, headers=build_headers(), json=query)
        resp.raise_for_status()
        data = resp.json()
        conversations.extend(data.get("conversations", []))

        next_page = data.get("pages", {}).get("next")
        if not next_page:
            break
        query["pagination"]["starting_after"] = next_page.get("starting_after")

    return conversations


def get_contact_first_name(session, conv):
    """Best-effort extraction of the customer's first name."""
    contacts = conv.get("contacts", {}).get("contacts", [])
    if contacts:
        contact_id = contacts[0].get("id")
        if contact_id:
            try:
                resp = session.get(f"{BASE_URL}/contacts/{contact_id}", headers=build_headers())
                if resp.status_code == 200:
                    contact = resp.json()
                    name = contact.get("name") or ""
                    if name:
                        return name.split()[0]
            except requests.RequestException:
                pass
    return "there"


def hours_since(timestamp):
    if not timestamp:
        return None
    delta = datetime.now(timezone.utc) - datetime.fromtimestamp(timestamp, tz=timezone.utc)
    return round(delta.total_seconds() / 3600, 1)


def strip_html(html_text):
    """Very small HTML-tag stripper, good enough for matching message text."""
    text = re.sub(r"<[^<]+?>", "", html_text or "")
    return " ".join(text.split())


def get_last_message_info(session, conv_id):
    """
    Fetch the full conversation and return (body_text, author_type, created_at,
    part_type) for the last ACTUAL MESSAGE in the thread - skipping over notes,
    assignments, snoozes, and other non-message events, which can otherwise get
    picked up as the "last part" and mask the real last message.
    """
    resp = session.get(f"{BASE_URL}/conversations/{conv_id}", headers=build_headers())
    resp.raise_for_status()
    conv = resp.json()

    parts = conv.get("conversation_parts", {}).get("conversation_parts", [])
    message_parts = [p for p in parts if p.get("part_type") == "comment"]

    if message_parts:
        last_part = message_parts[-1]
        body = last_part.get("body", "")
        author_type = last_part.get("author", {}).get("type")
        created_at = last_part.get("created_at")
        part_type = last_part.get("part_type")
    else:
        source = conv.get("source", {}) or {}
        body = source.get("body", "")
        author_type = (source.get("author", {}) or {}).get("type")
        created_at = conv.get("created_at")
        part_type = "source"

    return strip_html(body), author_type, created_at, part_type


def conversation_qualifies(body, author_type, marker):
    """
    Pure decision function, kept separate from any network calls so it's
    trivially unit-testable: a conversation qualifies for closing if the
    last message was from an admin AND it contains the follow-up marker.
    """
    return author_type == "admin" and marker in body


def find_stale_conversations(session, admin_id, marker, debug=False):
    """
    A conversation qualifies if:
    - the LAST actual message in the thread was sent by an admin
    - that last message is their previous "just following up" nudge
      (matched via `marker`)
    There is no minimum-wait requirement - a conversation qualifies as soon
    as the nudge is the last message and the customer hasn't replied.
    """
    conversations = search_open_conversations(session, admin_id)
    if debug:
        log.debug("%d open conversation(s) assigned to admin_id=%s", len(conversations), admin_id)

    matches = []

    for conv in conversations:
        conv_id = conv.get("id")
        body, author_type, created_at, part_type = get_last_message_info(session, conv_id)

        qualifies = conversation_qualifies(body, author_type, marker)
        hrs = hours_since(created_at)

        if debug:
            preview = body[:80] + ("..." if len(body) > 80 else "")
            log.debug(
                "conversation %s | last part_type=%s author_type=%s marker_found=%s | "
                "hours_since_nudge=%s | body preview: %r | => %s",
                conv_id, part_type, author_type, marker in body, hrs, preview,
                "MATCH" if qualifies else "skipped",
            )

        if not qualifies:
            continue

        matches.append(
            {
                "id": conv_id,
                "hours_waiting": hrs,
                "link": f"https://app.intercom.com/a/inbox/_/inbox/conversation/{conv_id}",
                "name": get_contact_first_name(session, conv),
            }
        )

    matches.sort(key=lambda x: x["hours_waiting"] or 0, reverse=True)
    return matches


def post_reply(session, conv_id, admin_id, message_body):
    """Post an admin reply (visible to the customer) on a conversation."""
    url = f"{BASE_URL}/conversations/{conv_id}/reply"
    payload = {
        "message_type": "comment",
        "type": "admin",
        "admin_id": admin_id,
        "body": message_body,
    }
    resp = session.post(url, headers=build_headers(), json=payload)
    resp.raise_for_status()
    return resp.json()


def close_conversation(session, conv_id, admin_id):
    """Close a conversation as the admin."""
    url = f"{BASE_URL}/conversations/{conv_id}/reply"
    payload = {
        "message_type": "close",
        "type": "admin",
        "admin_id": admin_id,
    }
    resp = session.post(url, headers=build_headers(), json=payload)
    resp.raise_for_status()
    return resp.json()


# ---------------- Slack + logging output ----------------

def format_slack_summary(teammate_name, results, dry_run):
    """Plain-text summary for a Slack Workflow Builder DM (variables render
    as literal text, so no markdown syntax here)."""
    now_str = datetime.now(timezone.utc).strftime("%b %d, %Y - %H:%M UTC")
    sent_count = sum(1 for r in results if r["status"] == "sent + closed")
    failed = [r for r in results if r["status"].startswith("FAILED")]

    lines = []
    mode_note = " (DRY RUN - nothing was actually sent)" if dry_run else ""
    lines.append(f"Auto-Closer Summary for {teammate_name}{mode_note}")
    lines.append("")
    lines.append(f"Closed: {sent_count}")
    if failed:
        lines.append(f"Failed: {len(failed)}")
    lines.append("")

    if results:
        lines.append("Tickets:")
        for r in results:
            icon = "OK" if r["status"] == "sent + closed" else ("SKIPPED" if "dry-run" in r["status"] else "FAILED")
            lines.append(f"[{icon}] {r['name']} - waited {r['hours_waiting']}h - {r['link']}")
    else:
        lines.append("No tickets matched the closing condition this run.")

    lines.append("")
    lines.append(f"Generated {now_str}")
    return "\n".join(lines)


def post_to_slack(session, webhook_url, text):
    if not webhook_url:
        log.warning("No Slack webhook configured - skipping Slack post.")
        return
    resp = session.post(webhook_url, json={"report_text": text})
    resp.raise_for_status()
    log.info("Posted closing summary to Slack.")


def write_markdown_log(sections, out_path, dry_run):
    date_str = datetime.now().strftime("%Y-%m-%d")
    with open(out_path, "w") as f:
        f.write(f"# Closing log - {date_str}\n\n")
        if dry_run:
            f.write("_DRY RUN - nothing was actually sent or closed._\n\n")

        for teammate_name, results, message in sections:
            f.write(f"## {teammate_name}\n\n")
            f.write(f"{len(results)} ticket(s) matched the closing condition.\n\n")
            f.write("---\n\n")
            for r in results:
                f.write(f"### Ticket {r['id']}  (waiting {r['hours_waiting']}h)\n\n")
                f.write(f"Status: **{r['status']}**\n\n")
                f.write(f"[Open in Intercom]({r['link']})\n\n")
                f.write("**Message:**\n\n")
                f.write("```\n")
                f.write(message)
                f.write("\n```\n\n")
                f.write("---\n\n")


# ---------------- Main ----------------

def process_teammate(session, teammate, config, debug, dry_run):
    marker, message = resolve_teammate_settings(teammate, config)
    admin_id = teammate["admin_id"]

    if not admin_id or admin_id.startswith("REPLACE_WITH"):
        log.warning("Skipping '%s' - admin_id not configured in team.yaml yet.", teammate["key"])
        return []

    matches = find_stale_conversations(session, admin_id, marker, debug=debug)

    results = []
    for m in matches:
        if dry_run:
            results.append({**m, "status": "dry-run (not sent)"})
            continue
        try:
            post_reply(session, m["id"], admin_id, message)
            close_conversation(session, m["id"], admin_id)
            results.append({**m, "status": "sent + closed"})
            log.info("Ticket %s: sent + closed", m["id"])
        except requests.RequestException as e:
            results.append({**m, "status": f"FAILED: {e}"})
            log.error("Ticket %s: FAILED - %s", m["id"], e)

    return results, message


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--list-admins", action="store_true", help="List teammates and exit")
    parser.add_argument("--config", default=DEFAULT_TEAM_CONFIG_PATH, help="Path to team.yaml")
    parser.add_argument("--only", default=None, help="Only run for this teammate's config 'key'")
    parser.add_argument("--out", default=None, help="Output markdown file path")
    parser.add_argument("--debug", action="store_true",
                         help="Print why each open conversation was matched or skipped")
    parser.add_argument("--dry-run", action="store_true",
                         help="Find matches, write the log, post dry-run Slack summaries, "
                              "but do NOT send or close anything")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    if not INTERCOM_TOKEN:
        sys.exit("INTERCOM_TOKEN environment variable is not set.")

    session = build_session()

    if args.list_admins:
        list_admins(session)
        return

    config = load_team_config(args.config)
    teammates = config["teammates"]
    if args.only:
        teammates = [t for t in teammates if t.get("key") == args.only]
        if not teammates:
            sys.exit(f"No teammate with key '{args.only}' found in {args.config}")

    webhooks = load_webhooks()

    date_str = datetime.now().strftime("%Y-%m-%d")
    out_path = args.out or f"intercom_closing_log_{date_str}.md"

    log_sections = []
    for teammate in teammates:
        log.info("--- Processing %s (%s) ---", teammate.get("name", teammate["key"]), teammate["key"])
        outcome = process_teammate(session, teammate, config, args.debug, args.dry_run)
        if not outcome:
            continue
        results, message = outcome
        log_sections.append((teammate.get("name", teammate["key"]), results, message))

        sent_count = sum(1 for r in results if r["status"] == "sent + closed")
        log.info("%d/%d ticket(s) sent + closed for %s.", sent_count, len(results), teammate["key"])

        slack_text = format_slack_summary(teammate.get("name", teammate["key"]), results, args.dry_run)
        webhook_url = webhooks.get(teammate["key"])
        post_to_slack(session, webhook_url, slack_text)

    write_markdown_log(log_sections, out_path, args.dry_run)
    log.info("Log saved to: %s", out_path)


if __name__ == "__main__":
    main()
