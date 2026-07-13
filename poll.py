#!/usr/bin/env python3
"""
Single-shot poller: fetches the JI "What We're Watching" page and posts any
newly added bullets to Slack. Runs once per GitHub Actions invocation.

Auth: sends JI_COOKIE (a full subscriber Cookie header) so the page renders
the live subscriber list rather than the public morning-Kickoff snapshot.
Also sends X-JI-Watcher-Token (JI_BYPASS_TOKEN) if set, so JI's Cloudflare
allowlists this watcher past its bot block.

State: seen bullet IDs live in watcher_state.json, committed back to the repo
after each run. Concurrent-run safe: re-reads freshest state from origin/main
right before each post, and commits with a merge-and-retry loop on push.
"""

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup, NavigableString, Tag

WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
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


def _parse(text):
    try:
        return set(json.loads(text))
    except Exception:
        return set()


def load_seen():
    return _parse(STATE_FILE.read_text()) if STATE_FILE.exists() else set()


def save_seen(seen):
    STATE_FILE.write_text(json.dumps(sorted(seen)[-500:]))


def git(*args):
    return subprocess.run(
        ["git", "-C", str(REPO), *args], capture_output=True, text=True, timeout=30
    )


def latest_seen():
    git("fetch", "-q", "origin", "main")
    r = git("show", "origin/main:watcher_state.json")
    return _parse(r.stdout) if r.returncode == 0 else load_seen()


def record(seen):
    for _ in range(5):
        git("fetch", "-q", "origin", "main")
        remote = git("show", "origin/main:watcher_state.json")
        merged = seen | (_parse(remote.stdout) if remote.returncode == 0 else set())
        git("reset", "-q", "--hard", "origin/main")
        save_seen(merged)
        git("add", "watcher_state.json")
        if git("diff", "--cached", "--quiet").returncode == 0:
            return
        git(*GIT_ID, "commit", "-q", "-m", "Update watcher state [skip ci]")
        if git("push", "-q", "origin", "HEAD:main").returncode == 0:
            return


def post_to_slack(text):
    body = json.dumps({"text": text}).encode()
    urlopen(
        Request(WEBHOOK_URL, data=body, headers={"Content-Type": "application/json"}),
        timeout=15,
    ).read()


def main():
    if not WEBHOOK_URL:
        print("SLACK_WEBHOOK_URL not set yet — nothing to do.")
        return
    if not JI_COOKIE:
        print("JI_COOKIE not set — would only see public snapshot. Aborting.")
        return

    bullets = fetch_bullets()
    print(f"Fetched {len(bullets)} bullets from page.")

    if not STATE_FILE.exists():
        seen = {bullet_id(plain) for plain, _ in bullets}
        save_seen(seen)
        post_to_slack(
            f":eyes: Watcher is live on <{PAGE_URL}|What We're Watching> "
            f"(tracking {len(bullets)} existing bullets). New updates will appear here."
        )
        record(seen)
        print(f"First run — baseline of {len(bullets)} bullets saved.")
        return

    new_bullets = [(bid, formatted) for plain, formatted in bullets
                   if (bid := bullet_id(plain)) not in latest_seen()]

    if not new_bullets:
        print("No new bullets.")
        return

    posted = 0
    for bid, formatted in new_bullets:
        seen = latest_seen()
        if bid in seen:
            continue
        post_to_slack(
            f":newspaper: *New on <{PAGE_URL}|What We're Watching>:*\n{formatted}"
        )
        seen.add(bid)
        save_seen(seen)
        record(seen)
        posted += 1
    print(f"Posted {posted} new bullet(s).")


if __name__ == "__main__":
    main()
