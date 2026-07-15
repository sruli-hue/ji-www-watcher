#!/usr/bin/env python3
"""
Single-shot poller for the JI "What We're Watching" / Live Briefing page.

Two jobs, every run:

1. WWW watcher (unchanged): posts any newly added bullets to the main Slack
   webhook (SLACK_WEBHOOK_URL).

2. Live Briefing nudge: if the page has gone too long without a NEW entry, it
   nudges the #live-briefing channel (LIVE_BRIEFING_WEBHOOK). Escalation, measured
   from the last new entry:

       nudge #0 at  2h of silence
       nudge #1 at  5h   (+3h)
       nudge #2 at  9h   (+4h)
       nudge #3 at 11h   (+2h, cycle restarts)
       ...  the 2/3/4-hour cycle repeats.

   As soon as a new entry appears, the clock resets to zero. Never posts on
   Saturday (America/New_York) — see SKIP_SATURDAY. If the page has been static
   longer than DORMANT_HOURS, it stops nudging until something new appears.

Auth: sends JI_COOKIE (a full subscriber Cookie header) so the page renders the
live subscriber list rather than the public morning-Kickoff snapshot. Also sends
X-JI-Watcher-Token (JI_BYPASS_TOKEN) if set, so JI's Cloudflare allowlists this
watcher past its bot block.

State (watcher_state.json, committed back to the repo after each run):
  { "seen": [bullet ids...], "last_new": <epoch when a new bullet last appeared>,
    "nudges": <how many nudges sent since that last new bullet> }
Legacy state files (a bare JSON array of ids) are migrated automatically.
"""

import hashlib
import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, NavigableString, Tag

WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
LIVE_BRIEFING_WEBHOOK = os.environ.get("LIVE_BRIEFING_WEBHOOK", "")
JI_COOKIE = os.environ.get("JI_COOKIE", "")
BYPASS_TOKEN = os.environ.get("JI_BYPASS_TOKEN", "")

PAGE_URL = "https://jewishinsider.com/what-we-are-watching/"
STATE_FILE = Path(__file__).parent / "watcher_state.json"
REPO = STATE_FILE.parent
MIN_BULLET_LENGTH = 80
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
GIT_ID = [
    "-c", "user.name=github-actions[bot]",
    "-c", "user.email=github-actions[bot]@users.noreply.github.com",
]

# --- Live Briefing nudge config ---
CYCLE = [2, 3, 4]          # escalation increments (hours), repeated forever
DORMANT_HOURS = 72         # stop nudging if the page is static longer than this
SKIP_SATURDAY = True       # no nudges on Saturday (America/New_York)
TZ = ZoneInfo("America/New_York")
def nudge_text(elapsed_h):
    hrs = round(elapsed_h)
    unit = "hour" if hrs == 1 else "hours"
    return (
        "What's happening now that readers need to know? Make sure to put it up "
        f"on the Live Briefing — it's been about {hrs} {unit} since the last update."
    )


def cumulative_offset(n):
    """Hours of silence at which the n-th nudge (0-indexed) is due."""
    return sum(CYCLE[i % len(CYCLE)] for i in range(n + 1))


def slack_escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def node_to_slack(node):
    if isinstance(node, NavigableString):
        return slack_escape(str(node))
    if not isinstance(node, Tag):
        return ""
    inner = "".join(node_to_slack(c) for c in node.children)
    name = node.name.lower()
    if name in ("b", "strong"):
        stripped = inner.strip()
        return f" *{stripped}* " if stripped else ""
    if name in ("i", "em"):
        stripped = inner.strip()
        return f" _{stripped}_ " if stripped else ""
    if name == "a":
        href = node.get("href", "").strip()
        label = inner.strip() or href
        return f"<{href}|{label}>" if href else label
    if name == "br":
        return "\n"
    return inner


def fetch_bullets():
    headers = {"User-Agent": UA}
    if JI_COOKIE:
        headers["Cookie"] = JI_COOKIE
    if BYPASS_TOKEN:
        headers["X-JI-Watcher-Token"] = BYPASS_TOKEN
    with urlopen(Request(PAGE_URL, headers=headers), timeout=30) as r:
        html = r.read().decode()
    soup = BeautifulSoup(html, "html.parser")
    bullets = []
    for li in soup.find_all("li", class_="ww-item"):
        if li.find(class_="wr_item_container"):
            continue  # skip Worthy Reads section — different content, not wanted
        plain = li.get_text(separator=" ", strip=True)
        if len(plain) < MIN_BULLET_LENGTH:
            continue
        formatted = node_to_slack(li)
        lines = [" ".join(line.split()) for line in formatted.split("\n")]
        formatted = "\n".join(l for l in lines if l)
        formatted = re.sub(r" +([,.;:!?)])", r"\1", formatted)
        formatted = re.sub(r"([(]) +", r"\1", formatted)
        bullets.append((plain, formatted))
    return bullets


def bullet_id(text):
    normalized = " ".join(text.lower().split())[:80]
    return hashlib.md5(normalized.encode()).hexdigest()


def _parse_state(text):
    """Return {'seen': set, 'last_new': float|None, 'nudges': int}. Migrates the
    legacy format (a bare JSON array of ids)."""
    try:
        data = json.loads(text)
    except Exception:
        return {"seen": set(), "last_new": None, "nudges": 0}
    if isinstance(data, list):  # legacy: just the seen ids
        return {"seen": set(data), "last_new": None, "nudges": 0}
    return {
        "seen": set(data.get("seen", [])),
        "last_new": data.get("last_new"),
        "nudges": data.get("nudges", 0),
    }


def load_state():
    return _parse_state(STATE_FILE.read_text()) if STATE_FILE.exists() else \
        {"seen": set(), "last_new": None, "nudges": 0}


def save_state(state):
    STATE_FILE.write_text(json.dumps({
        "seen": sorted(state["seen"])[-500:],
        "last_new": state["last_new"],
        "nudges": state["nudges"],
    }))


def git(*args):
    return subprocess.run(
        ["git", "-C", str(REPO), *args], capture_output=True, text=True, timeout=30
    )


def latest_state():
    """Freshest committed state from origin."""
    git("fetch", "-q", "origin", "main")
    r = git("show", "origin/main:watcher_state.json")
    return _parse_state(r.stdout) if r.returncode == 0 else load_state()


def record(state):
    """Push state; union the seen-set with origin's, keep our nudge metadata.
    Retries on a concurrent-push race."""
    for _ in range(5):
        git("fetch", "-q", "origin", "main")
        remote = git("show", "origin/main:watcher_state.json")
        rstate = _parse_state(remote.stdout) if remote.returncode == 0 else \
            {"seen": set()}
        merged = dict(state)
        merged["seen"] = set(state["seen"]) | rstate.get("seen", set())
        git("reset", "-q", "--hard", "origin/main")
        save_state(merged)
        git("add", "watcher_state.json")
        if git("diff", "--cached", "--quiet").returncode == 0:
            return
        git(*GIT_ID, "commit", "-q", "-m", "Update watcher state [skip ci]")
        if git("push", "-q", "origin", "HEAD:main").returncode == 0:
            return


def post_to_slack(webhook, text):
    body = json.dumps({"text": text}).encode()
    urlopen(
        Request(webhook, data=body, headers={"Content-Type": "application/json"}),
        timeout=15,
    ).read()


def maybe_nudge(state, now):
    """Post the Live Briefing nudge if one is due. Mutates and returns state."""
    if not LIVE_BRIEFING_WEBHOOK:
        print("LIVE_BRIEFING_WEBHOOK not set — skipping nudge check.")
        return state
    if SKIP_SATURDAY and datetime.fromtimestamp(now, TZ).weekday() == 5:
        print("It's Saturday (ET) — no nudge.")
        return state

    last_new = state["last_new"] or now
    elapsed_h = (now - last_new) / 3600
    if elapsed_h > DORMANT_HOURS:
        print(f"Page static {elapsed_h:.1f}h (> {DORMANT_HOURS}h) — dormant, no nudge.")
        return state

    due_at = cumulative_offset(state["nudges"])
    print(f"{elapsed_h:.2f}h since last new entry; {state['nudges']} nudge(s) sent; "
          f"next due at {due_at}h.")
    if elapsed_h >= due_at:
        post_to_slack(LIVE_BRIEFING_WEBHOOK, nudge_text(elapsed_h))
        state["nudges"] += 1
        print(f"Posted nudge #{state['nudges'] - 1}.")
    return state


def main():
    if not WEBHOOK_URL:
        print("SLACK_WEBHOOK_URL not set yet — nothing to do.")
        return
    if not JI_COOKIE:
        print("JI_COOKIE not set — would only see public snapshot. Aborting.")
        return

    now = time.time()
    bullets = fetch_bullets()
    print(f"Fetched {len(bullets)} bullets from page.")
    current_ids = {bullet_id(plain) for plain, _ in bullets}

    if not STATE_FILE.exists():
        # First ever run — baseline silently, start the nudge clock now.
        state = {"seen": current_ids, "last_new": now, "nudges": 0}
        save_state(state)
        post_to_slack(
            WEBHOOK_URL,
            f":eyes: Watcher is live on <{PAGE_URL}|What We're Watching> "
            f"(tracking {len(bullets)} existing bullets). New updates will appear here.",
        )
        record(state)
        print(f"First run — baseline of {len(bullets)} bullets saved.")
        return

    prev = latest_state()
    seen = set(prev["seen"])
    new_bullets = [(bid, formatted) for plain, formatted in bullets
                   if (bid := bullet_id(plain)) not in seen]

    if new_bullets:
        # New entries → post them and RESET the nudge clock.
        for bid, formatted in new_bullets:
            post_to_slack(
                WEBHOOK_URL,
                f":newspaper: *New on <{PAGE_URL}|What We're Watching>:*\n{formatted}",
            )
            seen.add(bid)
        state = {"seen": seen, "last_new": now, "nudges": 0}
        save_state(state)
        record(state)
        print(f"Posted {len(new_bullets)} new bullet(s); nudge clock reset.")
        return

    # Nothing new → carry the clock forward and nudge if it's time.
    state = {"seen": seen, "last_new": prev["last_new"], "nudges": prev["nudges"]}
    before = state["nudges"]
    state = maybe_nudge(state, now)
    if state["nudges"] != before or prev["last_new"] is None:
        # Persist a bumped nudge count (or backfill last_new on a migrated file).
        if prev["last_new"] is None:
            state["last_new"] = now
        save_state(state)
        record(state)
    else:
        print("No new bullets; no nudge due.")


if __name__ == "__main__":
    main()
