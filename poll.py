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
from urllib.error import HTTPError, URLError
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


# Strings that show up when we get a Cloudflare challenge or a logged-out /
# paywalled page instead of the real subscriber list. If we see any of these
# (or simply zero bullets on a non-first run), the cookie is almost certainly
# expired and we must alert rather than silently watch the wrong content.
AUTH_WALL_MARKERS = (
    "just a moment",            # Cloudflare interstitial
    "cf-browser-verification",
    "attention required",
    "enable javascript and cookies",
    "subscribe to continue",
    "sign in to continue",
)
ALERT_THROTTLE_H = 6            # don't re-alert more than once per this many hours


def fetch_bullets():
    headers = {"User-Agent": UA}
    if JI_COOKIE:
        headers["Cookie"] = JI_COOKIE
    if BYPASS_TOKEN:
        headers["X-JI-Watcher-Token"] = BYPASS_TOKEN
    # Retry transient JI-side hiccups (5xx, network timeouts, connection resets)
    # so a brief upstream blip doesn't fail the whole run. Give up on 4xx
    # (401/403 = cookie problem — retrying won't help; let it bubble up).
    last_err = None
    for attempt in range(3):
        try:
            with urlopen(Request(PAGE_URL, headers=headers), timeout=30) as r:
                html = r.read().decode()
            break
        except HTTPError as e:
            last_err = e
            if e.code < 500:
                raise  # 4xx — real problem, don't paper over it
        except (URLError, TimeoutError) as e:
            last_err = e
        if attempt < 2:
            time.sleep(3 + 2 * attempt)  # 3s, 5s
    else:
        raise last_err
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
    return bullets, html


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
        return {"seen": set(data), "last_new": None, "nudges": 0,
                "alerted_at": None, "page_updated": None}
    return {
        "seen": set(data.get("seen", [])),
        "last_new": data.get("last_new"),
        "nudges": data.get("nudges", 0),
        "alerted_at": data.get("alerted_at"),
        "page_updated": data.get("page_updated"),
    }


def load_state():
    return _parse_state(STATE_FILE.read_text()) if STATE_FILE.exists() else \
        {"seen": set(), "last_new": None, "nudges": 0,
         "alerted_at": None, "page_updated": None}


def save_state(state):
    STATE_FILE.write_text(json.dumps({
        "seen": sorted(state["seen"])[-500:],
        "last_new": state.get("last_new"),
        "nudges": state["nudges"],
        "alerted_at": state.get("alerted_at"),
        "page_updated": state.get("page_updated"),
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


def body_class(html):
    """The <body> class attribute, lowercased ('' if not found). WordPress tags
    it 'freeuser' when NOT logged in as a subscriber; a valid cookie removes it."""
    m = re.search(r"<body[^>]*\bclass=[\"']([^\"']*)[\"']", html, re.IGNORECASE)
    return m.group(1).lower() if m else ""


def parse_page_updated(html):
    """Epoch (seconds) of the page's own '<div class="ww-updated"> Last updated
    <Month> <D>, <YYYY> - <H:MM> <AM/PM> ET' banner — the editors' own last-touch
    time, which is what the Live Briefing nudge measures staleness against.
    Returns None if the banner is missing or unparseable. Interpreted in ET."""
    soup = BeautifulSoup(html, "html.parser")
    el = soup.find(class_="ww-updated")
    text = el.get_text(" ", strip=True) if el else ""
    m = re.search(r"Last updated\s+(.+?)\s*ET\b", text, re.IGNORECASE)
    if not m:
        return None
    # e.g. "July 16, 2026 - 12:56 PM" -> "July 16 2026 12:56 PM"
    stamp = " ".join(m.group(1).replace("-", " ").replace(",", " ").split())
    for fmt in ("%B %d %Y %I:%M %p", "%b %d %Y %I:%M %p"):
        try:
            return datetime.strptime(stamp, fmt).replace(tzinfo=TZ).timestamp()
        except ValueError:
            continue
    return None


# Verified 2026-07-16 against a real authenticated Actions run: the subscriber
# page's <body> carries 'logged-in' and NOT 'freeuser'; the logged-out/expired-
# cookie page carries 'freeuser'. So blocking on 'freeuser' cannot false-positive
# on the working page.
BLOCK_ON_FREEUSER = True


def looks_like_auth_wall(html, bullets):
    """True if the fetched page is logged-out / challenged rather than the real
    subscriber list.

    Signals, safest first:
      - 'freeuser' body class = WordPress served the free/logged-out page. This is
        the definitive expired-cookie signal for this site (an expired cookie does
        NOT 403 — JI silently serves a different public snapshot). Gated behind
        BLOCK_ON_FREEUSER until verified against an authenticated run.
      - Cloudflare challenge / hard-block markers.
      - Zero bullets at all.
    """
    if BLOCK_ON_FREEUSER and "freeuser" in body_class(html):
        return True
    low = html.lower()
    if any(m in low for m in AUTH_WALL_MARKERS):
        return True
    return len(bullets) == 0


def alert_auth_failure(state, now):
    """Post a throttled Slack alert that the cookie looks dead. Persists
    alerted_at so we warn at most once per ALERT_THROTTLE_H hours."""
    last = state.get("alerted_at")
    if last is not None and (now - last) < ALERT_THROTTLE_H * 3600:
        print(f"Auth wall, but alerted {round((now-last)/3600,1)}h ago — throttled.")
        return
    post_to_slack(
        WEBHOOK_URL,
        ":warning: *What We're Watching watcher can't read the subscriber page.* "
        "The JI cookie has almost certainly expired (or Cloudflare is blocking the "
        "watcher). Until `JI_COOKIE` is refreshed, *no new-entry detection and no "
        "Live Briefing nudges will fire.* Refresh the cookie in the "
        "`ji-www-watcher` repo secrets.",
    )
    state["alerted_at"] = now
    save_state(state)
    record(state)
    print("Posted auth-failure alert.")


def maybe_nudge(state, now, page_updated_ts):
    """Post the Live Briefing nudge if one is due, measuring staleness against the
    page's OWN 'Last updated' time (page_updated_ts) — not our hash-based bullet
    detection, which a mid-bullet edit would falsely trip. The nudge count resets
    only when that timestamp actually advances. Mutates and returns state."""
    if not LIVE_BRIEFING_WEBHOOK:
        print("LIVE_BRIEFING_WEBHOOK not set — skipping nudge check.")
        return state
    if page_updated_ts is None:
        print("No page 'Last updated' time — cannot measure staleness, skipping nudge.")
        return state

    # A genuinely newer page-update time = fresh content → restart the clock.
    if state.get("page_updated") != page_updated_ts:
        state["page_updated"] = page_updated_ts
        state["nudges"] = 0

    if SKIP_SATURDAY and datetime.fromtimestamp(now, TZ).weekday() == 5:
        print("It's Saturday (ET) — no nudge.")
        return state

    elapsed_h = (now - page_updated_ts) / 3600
    stamp = datetime.fromtimestamp(page_updated_ts, TZ).strftime("%-I:%M %p ET")
    if elapsed_h > DORMANT_HOURS:
        print(f"Page last updated {stamp}, {elapsed_h:.1f}h ago (> {DORMANT_HOURS}h) "
              f"— dormant, no nudge.")
        return state

    due_at = cumulative_offset(state["nudges"])
    print(f"Page last updated {stamp}, {elapsed_h:.2f}h ago; "
          f"{state['nudges']} nudge(s) sent; next due at {due_at}h.")
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
    bullets, html = fetch_bullets()
    bc = body_class(html)
    print(f"Fetched {len(bullets)} bullets from page. "
          f"Body class: '{bc}' (freeuser={'freeuser' in bc}).")

    # Guard: a logged-out / Cloudflare-challenged page silently returns the wrong
    # content. Detect it and alert instead of poisoning the state. (First-ever run
    # with no state is exempt — nothing to protect yet.)
    if STATE_FILE.exists() and looks_like_auth_wall(html, bullets):
        print("Auth wall detected — page did not render the subscriber list.")
        prev = latest_state()
        alert_auth_failure(prev, now)
        return

    page_updated_ts = parse_page_updated(html)
    if page_updated_ts:
        print("Page 'Last updated' = "
              f"{datetime.fromtimestamp(page_updated_ts, TZ).strftime('%Y-%m-%d %-I:%M %p ET')}")
    else:
        print("Could not parse the page's 'Last updated' banner.")

    current_ids = {bullet_id(plain) for plain, _ in bullets}

    if not STATE_FILE.exists():
        # First ever run — baseline silently, start the nudge clock now.
        state = {"seen": current_ids, "last_new": now, "nudges": 0,
                 "page_updated": page_updated_ts}
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

    # Post any newly-seen bullets to Slack (hash-based surfacing of new items).
    # This no longer touches the nudge clock — the nudge is driven purely by the
    # page's own 'Last updated' time below, so a mid-bullet edit can't reset it.
    posted = 0
    for bid, formatted in new_bullets:
        post_to_slack(
            WEBHOOK_URL,
            f":newspaper: *New on <{PAGE_URL}|What We're Watching>:*\n{formatted}",
        )
        seen.add(bid)
        posted += 1
    if posted:
        print(f"Posted {posted} new bullet(s) to Slack.")

    # Live Briefing nudge — measured against the page's own last-updated time.
    state = {
        "seen": seen,
        "last_new": prev.get("last_new"),
        "nudges": prev.get("nudges", 0),
        "alerted_at": prev.get("alerted_at"),
        "page_updated": prev.get("page_updated"),
    }
    before = (state["nudges"], state.get("page_updated"))
    state = maybe_nudge(state, now, page_updated_ts)
    after = (state["nudges"], state.get("page_updated"))
    if posted or after != before:
        save_state(state)
        record(state)
    else:
        print("Nothing new to post; no nudge due.")


if __name__ == "__main__":
    main()
