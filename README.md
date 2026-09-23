# Intercom Auto-Closer

Finds open Intercom conversations where **you** sent a "just following up"
nudge and the customer never replied, then posts your closing message and
closes the ticket — for you and, optionally, your whole team. Posts a
per-person summary to Slack and writes a markdown log of everything it did.

> ⚠️ This sends real messages to real customers and closes real tickets.
> Always run with `--debug` or `--dry-run` first after any change.

## How it decides a ticket is done

A conversation qualifies for closing when:
1. It's currently **open** and assigned to a configured teammate, **and**
2. The **last message in the thread** was sent by that admin, **and**
3. That last message contains their configured follow-up phrase
   (default: *"I just wanted to follow up in case you missed my previous
   message"*).

There's no minimum wait time — it qualifies as soon as the nudge is the last
word and the customer hasn't replied since.

## Setup

### 1. Secrets (GitHub → Settings → Secrets and variables → Actions)

| Secret | What it is |
|---|---|
| `INTERCOM_TOKEN` | Your workspace's Intercom API access token (shared — one token covers everyone on the team). |
| `SLACK_WEBHOOKS_JSON` | A JSON object mapping each teammate's config `key` to *their* personal Slack Workflow Builder webhook URL. Example: `{"nitzan": "https://hooks.slack.com/workflows/...", "dana": "https://hooks.slack.com/workflows/..."}` |

### 2. Config (`config/team.yaml` — not secret, committed to the repo)

This is what makes the tool shared instead of personal. Each teammate gets
an entry:

```yaml
teammates:
  - key: nitzan            # must match a key in SLACK_WEBHOOKS_JSON
    name: Nitzan
    team: Figma Weave Support
    admin_id: "6789012"    # find with: python intercom_auto_closer.py --list-admins
```

The closing message and follow-up phrase come from the shared
`default_closing_message_template` / `default_follow_up_marker` at the top
of the file, with `{name}` / `{team}` filled in automatically. Any teammate
can override either one individually if they need different wording —
see the commented-out example in the file.

### 3. Onboard a new teammate

1. They run `python intercom_auto_closer.py --list-admins` (with
   `INTERCOM_TOKEN` set locally) to find their Intercom admin ID.
2. Add a new entry under `teammates:` in `config/team.yaml`.
3. Add their Slack webhook URL to the `SLACK_WEBHOOKS_JSON` secret.
4. Open a PR — the test suite runs automatically and doesn't touch Intercom.

No code changes required.

## Usage

```bash
pip install -r requirements.txt

export INTERCOM_TOKEN=...
export SLACK_WEBHOOKS_JSON='{"nitzan": "https://hooks.slack.com/..."}'

# One-time, per person, to find their admin ID:
python intercom_auto_closer.py --list-admins

# See what would match and why, for everyone in team.yaml — sends nothing:
python intercom_auto_closer.py --debug

# Find matches, write the log, post dry-run Slack summaries — sends/closes nothing:
python intercom_auto_closer.py --dry-run

# Run for just one person:
python intercom_auto_closer.py --only nitzan

# Run for real, for the whole team:
python intercom_auto_closer.py
```

## Automation

`.github/workflows/intercom-auto-closer.yml` runs the tests on every PR and
push, and runs the real closer daily at 10:30am Israel time (adjust the cron
for DST — see the comment in the workflow file) plus on-demand via the
Actions tab's "Run workflow" button.

## Development

```bash
pip install -r requirements-dev.txt
pytest -v
```

Tests mock all HTTP calls — no real Intercom or Slack traffic happens when
running the suite.

## Known limitations / good next steps

- Rate-limit handling uses automatic retry with backoff, but very large
  inboxes will still be slow (one API call per open conversation to check
  its last message, plus one more to resolve the customer's name).
- The Israel DST cron shift is still manual twice a year.
- Slack webhook URLs currently need to be added by whoever manages the
  `SLACK_WEBHOOKS_JSON` secret — could move to a self-serve Slack app
  install flow later if the team grows.
