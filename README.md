# JI WWW Watcher

Posts new bullets from Jewish Insider's
[What We're Watching](https://jewishinsider.com/what-we-are-watching/) page to a
Slack channel, within ~5–15 minutes of publish.

Runs 24/7 as a scheduled GitHub Actions workflow — it does **not** depend on any
personal machine being on.

## Live Briefing nudge

The same run also nudges the **#live-briefing** channel when the page has gone
too long without a **new** entry, so the Live Briefing never goes stale
unnoticed. Escalation, measured from the last new entry: **2h → 5h → 9h → 11h →
14h → 18h …** (a repeating 2 / 3 / 4-hour cycle). A new entry resets the clock to
zero. It never posts on **Saturday** (ET), and stops nudging entirely if the page
has been static longer than 72h (`DORMANT_HOURS`). Set `LIVE_BRIEFING_WEBHOOK` to
turn it on; leave it unset and only the WWW-watcher posting runs.

## Cookie refresh (every ~2 months)

The page shows the full live list only to logged-in subscribers, so the workflow
sends a subscriber Cookie header stored in the `JI_COOKIE` repo secret. WordPress
auth cookies expire — when the workflow starts silently only seeing the morning
Kickoff snapshot (short posts) instead of the full list, refresh the cookie:

1. Log in to jewishinsider.com in Chrome, confirm you see the full WWW list.
2. Open DevTools → Application → Cookies → `https://jewishinsider.com`.
3. Concatenate every cookie as `name=value; name=value; ...` — or use the
   Cookie header from Network → any request → Request Headers → Cookie.
4. GitHub → Settings → Secrets and variables → Actions → update `JI_COOKIE`.

## Secrets

- `SLACK_WEBHOOK_URL` — incoming webhook for the target channel (WWW bullets).
- `LIVE_BRIEFING_WEBHOOK` — incoming webhook for **#live-briefing** (nudges).
  Optional; nudging is skipped if unset.
- `JI_COOKIE` — full subscriber Cookie header for jewishinsider.com.
- `JI_BYPASS_TOKEN` — optional, sent as `X-JI-Watcher-Token` for Cloudflare
  allowlisting. Add if requests start getting blocked from GitHub's IPs.

## Manual trigger

Actions tab → "JI WWW watcher" → "Run workflow".

## Local test

```bash
pip install -r requirements.txt
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."
export JI_COOKIE="wordpress_logged_in_...=...; wordpress_sec_...=..."
python3 poll.py
```

## First run

On the very first run (no `watcher_state.json`) it records the current bullets
as a baseline and posts a single "watcher is live" message. It does not backfill.
