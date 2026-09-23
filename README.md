# Intercom Support Automation

Two small, independent Intercom automations that live in this repo:

| Script | What it does | Scope |
|---|---|---|
| `intercom_auto_closer.py` | Closes tickets where your follow-up nudge went unanswered | Per-teammate |
| `intercom_slack_report.py` | Posts a queue health report (unassigned tickets, priority, aging) | Team-wide |

Both post to Slack, both are safe to `--dry-run` first, and both are covered
by tests that run automatically on every PR via GitHub Actions.

> ⚠️ **Security note:** an earlier version of the queue-report script had a
> live Intercom token hardcoded in it and was shared outside this system. If
> that token hasn't already been rotated, do it now: Intercom → Settings →
> Integrations → your app → Authentication → regenerate. Neither script
> reads secrets from anywhere except environment variables going forward.

---

## 1. Auto-Closer (`intercom_auto_closer.py`)

Finds each configured teammate's open conversations where their own
follow-up nudge was the last message and the customer never replied, then
posts the closing message and closes the ticket. Posts a per-person summary
to Slack.

### Setup

**Secrets** (Settings → Secrets and variables → Actions):

| Secret | What it is |
|---|---|
| `INTERCOM_TOKEN` | Shared Intercom API token (same one used by the queue report). |
| `SLACK_WEBHOOKS_JSON` | JSON object mapping each teammate's config `key` to their personal Slack webhook, e.g. `{"nitzan": "https://hooks.slack.com/..."}` |

**Config** (`config/team.yaml`, not secret, committed to the repo):

```yaml
teammates:
  - key: nitzan
    name: Nitzan
    team: Figma Weave Support
    admin_id: "6789012"   # find with: python intercom_auto_closer.py --list-admins
```

### Onboard a new teammate
1. They run `--list-admins` to find their Intercom admin ID.
2. Add an entry under `teammates:` in `config/team.yaml`.
3. Add their webhook to the `SLACK_WEBHOOKS_JSON` secret.
4. Open a PR — tests run automatically, nothing touches real tickets.

### Usage
```bash
python intercom_auto_closer.py --list-admins   # find your admin ID
python intercom_auto_closer.py --debug          # see matches, send nothing
python intercom_auto_closer.py --dry-run        # log + dry-run Slack summary, send nothing
python intercom_auto_closer.py --only nitzan    # run for just one person
python intercom_auto_closer.py                  # run for real, whole team
```

Runs daily via `.github/workflows/intercom-auto-closer.yml` (adjust the cron
for daylight saving — see the comment in that file) or on-demand from the
Actions tab.

---

## 2. Queue Report (`intercom_slack_report.py`)

Pulls every open conversation sitting in the **actual Unassigned inbox**
(no individual admin *and* no team claimed it — see the note in the script
about why both checks matter), computes a red/orange/green health status,
a topic breakdown, and posts one formatted report to a shared Slack channel.
Runs 4x/day.

### Setup

**Secrets:**

| Secret | Required? | What it is |
|---|---|---|
| `INTERCOM_TOKEN` | Yes | Same shared token as the auto-closer. |
| `QUEUE_REPORT_SLACK_WEBHOOK_URL` | Yes | Slack Incoming Webhook or Workflow Builder webhook for the team channel. |
| `ANTHROPIC_API_KEY` | No | If set, Claude classifies tickets into topics and writes the summary. If unset, falls back to counting Intercom tags — free, less nuanced. |
| `NOTION_TOKEN` / `NOTION_DAILY_UPDATES_DATA_SOURCE_ID` | No | Links the latest product update in the report. Skipped entirely if unset. |

**Tuning** (top of the script, no secrets involved):
- `THRESHOLDS` — the numbers driving red/orange/green.
- `URGENT_PRIORITY_VALUES` — which Priority values count as urgent/high.
- `TOPIC_LABELS` — friendly display names for raw Intercom tags (used by the
  tag-based fallback, and as the seed list for `TOPIC_TAXONOMY` below).
- `TOPIC_TAXONOMY` — the **fixed** list of topics Claude is allowed to pick
  from when classifying tickets. This is what makes topic counts consistent
  across reports — Claude must copy a label from this list exactly (or use
  "Other") instead of inventing new wording each run, which is the failure
  mode you'll see from Intercom's own built-in topic AI. Set to `[]` to let
  Claude generate open-ended labels instead.
- `MEDIAN_FRT_WINDOW_HOURS` (env var `INTERCOM_MEDIAN_FRT_WINDOW_HOURS`,
  default 24) — the rolling window used for the median first-response-time
  metric. This measures team responsiveness across *all* conversations
  created in that window (not just the current unassigned backlog), using
  Intercom's own precomputed `statistics.time_to_admin_reply`.

### Usage
```bash
python intercom_slack_report.py --list-attributes   # find your priority attribute key
python intercom_slack_report.py --dry-run            # print report, don't post
python intercom_slack_report.py --save report.json   # also save raw JSON
python intercom_slack_report.py                      # post to Slack for real
```

Runs 4x/day (06:00, 10:00, 14:00, 18:00 UTC) via
`.github/workflows/intercom-queue-report.yml` — edit the cron lines
(crontab.guru helps) or trigger manually from the Actions tab.

---

## Development

```bash
pip install -r requirements-dev.txt
pytest -v
```

All tests mock HTTP calls — no real Intercom, Slack, Claude, or Notion
traffic happens when running the suite. Each script has its own test file
under `tests/`, and each workflow only runs the tests relevant to that
script.

## Known limitations / good next steps

- The Israel DST cron shift (auto-closer) is still a manual edit twice a year.
- Rate-limit handling retries with backoff, but very large inboxes will
  still be slow — one Intercom API call per open conversation.
- Slack webhook URLs need to be added by whoever manages the repo's secrets;
  a self-serve Slack app install flow would remove that bottleneck as the
  team grows.
- The queue report's Claude-based classification is optional and costs a
  small amount per run if enabled — the tag-based fallback is free.
