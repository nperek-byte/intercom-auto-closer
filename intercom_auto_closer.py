#!/usr/bin/env python3
"""
Intercom Auto-Closer -> Slack DM
----------------------------------
Finds YOUR assigned, open Intercom conversations where the LAST message
was YOUR previous "just following up" nudge and the customer still hasn't
replied. For each match, POSTS the closing reply to the conversation and
CLOSES it in Intercom - then posts a summary of what happened to your
personal Slack inbox.

This sends real messages to real customers and closes real tickets.
Always run with --debug or --dry-run first when changing anything here.

REQUIRED ENVIRONMENT VARIABLES (never hardcode these):
    INTERCOM_TOKEN            - Intercom API access token
    INTERCOM_MY_ADMIN_ID      - Your Intercom admin ID (see --list-admins)
    SLACK_CLOSING_WEBHOOK_URL - Slack Workflow Builder webhook URL that DMs you

OPTIONAL ENVIRONMENT VARIABLES:
    INTERCOM_API_VERSION  - Intercom API version header (default: "2.11")

USAGE:
    python intercom_auto_closer.py --list-admins   (one-time, to find your ID)
    python intercom_auto_closer.py --debug          (see what would match, no sending)
    python intercom_auto_closer.py --dry-run        (log matches, post dry-run Slack
                                                       summary, but send/close nothing)
    python intercom_auto_closer.py                  (sends + closes matching tickets,
                                                       posts real Slack summary)

OUTPUT:
    Sends the closing reply and closes each matching ticket in Intercom.
    Posts a summary to your Slack inbox.
    Also saves a markdown log (intercom_closing_log_YYYY-MM-DD.md) recording
    what was sent, to which ticket, and whether it succeeded.
"""

import os
import sys
import re
import json
import argparse
from datetime import datetime, timezone
import requests

# ---------------- CONFIG ----------------
INTERCOM_TOKEN = os.environ.get("INTERCOM_TOKEN")
MY_ADMIN_ID = os.environ.get("INTERCOM_MY_ADMIN_ID")
SLACK_CLOSING_WEBHOOK_URL = os.environ.get("SLACK_CLOSING_WEBHOOK_URL")

# Same variable-name convention as the queue report script - must match
# whatever variable name you set up in Slack Workflow Builder's
# "From a webhook" trigger for THIS workflow.
SLACK_WORKFLOW_VARIABLE = os.environ.get("SLACK_WORKFLOW_VARIABLE", "report_text")

# The message that gets sent when closing.
MESSAGE_TEMPLATE = (
    "I'll go ahead and close this conversation for now since we haven't "
    "heard back, but if anything else comes up or you need further help, "
    "you can reply anytime and we'll be happy to assist."
)

# A conversation only qualifies if the LAST message in it was your own
# previous follow-up nudge (matched by this distinctive phrase, since exact
# formatting/whitespace can vary slightly).
PREVIOUS_FOLLOWUP_MARKER = "I just wanted to follow up in case you missed my previous message"

INTERCOM_API_VERSION = os.environ.get("INTERCOM_API_VERSION", "2.11")
BASE_URL = "https://api.intercom.io"
# -----------------------------------------

HEADERS = {
    "Authorization": f"Bearer {INTERCOM_TOKEN}",
    "Content-Type": "application/json",
    "Intercom-Version": INTERCOM_API_VERSION,
}


def list_admins():
    """Print all admins/teammates so you can find your own ID."""
    resp = requests.get(f"{BASE_URL}/admins", headers=HEADERS)
    resp.raise_for_status()
    data = resp.json()
    print("\nTeammates in this workspace:\n")
    for admin in data.get("admins", []):
        print(f"  id: {admin.get('id'):<15} name: {admin.get('name', '')}  email: {admin.get('email', '')}")
    print("\nUse the 'id' value for MY_ADMIN_ID / INTERCOM_MY_ADMIN_ID.\n")


def search_my_open_conversations():
    """Fetch open conversations assigned to MY_ADMIN_ID."""
    url = f"{BASE_URL}/conversations/search"
    query = {
        "query": {
            "operator": "AND",
            "value": [
                {"field": "open", "operator": "=", "value": "true"},
                {"field": "admin_assignee_id", "operator": "=", "value": MY_ADMIN_ID},
            ],
        },
        "pagination": {"per_page": 150},
    }

    conversations = []
    while True:
        resp = requests.post(url, headers=HEADERS, json=query)
        resp.raise_for_status()
        data = resp.json()
        conversations.extend(data.get("conversations", []))

        next_page = data.get("pages", {}).get("next")
        if not next_page:
            break
        query["pagination"]["starting_after"] = next_page.get("starting_after")

    return conversations


def get_contact_first_name(conv):
    """Best-effort extraction of the customer's first name."""
    contacts = conv.get("contacts", {}).get("contacts", [])
    if contacts:
        contact_id = contacts[0].get("id")
        if contact_id:
            try:
                resp = requests.get(f"{BASE_URL}/contacts/{contact_id}", headers=HEADERS)
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


def get_last_message_info(conv_id):
    """
    Fetch the full conversation and return (body_text, author_type, created_at,
    part_type) for the last ACTUAL MESSAGE in the thread - skipping over notes,
    assignments, snoozes, and other non-message events, which can otherwise get
    picked up as the "last part" and mask the real last message.
    """
    resp = requests.get(f"{BASE_URL}/conversations/{conv_id}", headers=HEADERS)
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


def find_stale_conversations(debug=False):
    """
    A conversation qualifies if:
    - the LAST actual message in the thread was sent by an admin (you)
    - that last message is your previous "just following up" nudge
      (matched via PREVIOUS_FOLLOWUP_MARKER)
    There is no minimum-wait requirement - a conversation qualifies as soon
    as your nudge is the last message and the customer hasn't replied.
    """
    conversations = search_my_open_conversations()
    if debug:
        print(f"[debug] {len(conversations)} open conversation(s) assigned to you\n")

    matches = []

    for conv in conversations:
        conv_id = conv.get("id")
        body, author_type, created_at, part_type = get_last_message_info(conv_id)

        marker_found = PREVIOUS_FOLLOWUP_MARKER in body
        qualifies = (author_type == "admin") and marker_found
        hrs = hours_since(created_at)

        if debug:
            preview = body[:80] + ("..." if len(body) > 80 else "")
            print(f"[debug] conversation {conv_id}")
            print(f"        last part_type={part_type}  author_type={author_type}  marker_found={marker_found}")
            print(f"        hours_since_nudge={hrs}")
            print(f"        body preview: {preview!r}")
            print(f"        => {'MATCH' if qualifies else 'skipped'}\n")

        if not qualifies:
            continue

        matches.append(
            {
                "id": conv_id,
                "hours_waiting": hrs,
                "link": f"https://app.intercom.com/a/inbox/_/inbox/conversation/{conv_id}",
                "name": get_contact_first_name(conv),
            }
        )

    matches.sort(key=lambda x: x["hours_waiting"] or 0, reverse=True)
    return matches


def post_reply(conv_id, message_body):
    """Post an admin reply (visible to the customer) on a conversation."""
    url = f"{BASE_URL}/conversations/{conv_id}/reply"
    payload = {
        "message_type": "comment",
        "type": "admin",
        "admin_id": MY_ADMIN_ID,
        "body": message_body,
    }
    resp = requests.post(url, headers=HEADERS, json=payload)
    resp.raise_for_status()
    return resp.json()


def close_conversation(conv_id):
    """Close a conversation as the admin."""
    url = f"{BASE_URL}/conversations/{conv_id}/reply"
    payload = {
        "message_type": "close",
        "type": "admin",
        "admin_id": MY_ADMIN_ID,
    }
    resp = requests.post(url, headers=HEADERS, json=payload)
    resp.raise_for_status()
    return resp.json()


def format_slack_summary(results, dry_run):
    """Plain-text summary for a Slack Workflow Builder DM (variables render
    as literal text, so no markdown syntax here)."""
    now_str = datetime.now(timezone.utc).strftime("%b %d, %Y · %H:%M UTC")
    sent_count = sum(1 for r in results if r["status"] == "sent + closed")
    failed = [r for r in results if r["status"].startswith("FAILED")]

    lines = []
    mode_note = " (DRY RUN — nothing was actually sent)" if dry_run else ""
    lines.append(f"🧹 Auto-Closer Summary{mode_note}")
    lines.append("")
    lines.append(f"✅ Closed: {sent_count}")
    if failed:
        lines.append(f"❌ Failed: {len(failed)}")
    lines.append("")

    if results:
        lines.append("Tickets:")
        for r in results:
            icon = "✅" if r["status"] == "sent + closed" else ("⏭️" if "dry-run" in r["status"] else "❌")
            lines.append(f"{icon} {r['name']} — waited {r['hours_waiting']}h — {r['link']}")
    else:
        lines.append("No tickets matched the closing condition this run.")

    lines.append("")
    lines.append(f"Generated {now_str}")
    return "\n".join(lines)


def post_to_slack(text):
    if not SLACK_CLOSING_WEBHOOK_URL:
        print("SLACK_CLOSING_WEBHOOK_URL not set - skipping Slack post.", file=sys.stderr)
        return
    payload = {SLACK_WORKFLOW_VARIABLE: text}
    resp = requests.post(SLACK_CLOSING_WEBHOOK_URL, json=payload)
    resp.raise_for_status()
    print("Posted closing summary to Slack.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--list-admins", action="store_true", help="List teammates and exit")
    parser.add_argument("--out", default=None, help="Output markdown file path")
    parser.add_argument("--debug", action="store_true",
                         help="Print why each open conversation was matched or skipped")
    parser.add_argument("--dry-run", action="store_true",
                         help="Find matches, write the log, post a dry-run Slack summary, "
                              "but do NOT send or close anything")
    args = parser.parse_args()

    if not INTERCOM_TOKEN:
        sys.exit("INTERCOM_TOKEN environment variable is not set.")

    if args.list_admins:
        list_admins()
        return

    if not MY_ADMIN_ID:
        sys.exit("INTERCOM_MY_ADMIN_ID is not set - run with --list-admins to find it.")

    matches = find_stale_conversations(debug=args.debug)

    date_str = datetime.now().strftime("%Y-%m-%d")
    out_path = args.out or f"intercom_closing_log_{date_str}.md"

    results = []
    for m in matches:
        if args.dry_run:
            results.append({**m, "status": "dry-run (not sent)"})
            continue
        try:
            post_reply(m["id"], MESSAGE_TEMPLATE)
            close_conversation(m["id"])
            results.append({**m, "status": "sent + closed"})
            print(f"Ticket {m['id']}: sent + closed")
        except requests.RequestException as e:
            results.append({**m, "status": f"FAILED: {e}"})
            print(f"Ticket {m['id']}: FAILED - {e}")

    with open(out_path, "w") as f:
        f.write(f"# Closing log - {date_str}\n\n")
        f.write(f"{len(matches)} ticket(s) matched the closing condition.\n\n")
        f.write("---\n\n")

        for r in results:
            f.write(f"## Ticket {r['id']}  (waiting {r['hours_waiting']}h)\n\n")
            f.write(f"Status: **{r['status']}**\n\n")
            f.write(f"[Open in Intercom]({r['link']})\n\n")
            f.write("**Message:**\n\n")
            f.write("```\n")
            f.write(MESSAGE_TEMPLATE)
            f.write("\n```\n\n")
            f.write("---\n\n")

    sent_count = sum(1 for r in results if r["status"] == "sent + closed")
    print(f"\n{sent_count}/{len(matches)} ticket(s) sent + closed.")
    print(f"Log saved to: {out_path}")

    slack_text = format_slack_summary(results, dry_run=args.dry_run)
    post_to_slack(slack_text)


if __name__ == "__main__":
    main()
