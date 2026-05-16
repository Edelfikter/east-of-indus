"""
Hourly Pulse generator for East of Indus.

Fetches the Induschan catalog (via the same INDUSCHAN_BASE used by scrape.py,
which is normally a Cloudflare Worker proxy), computes live metrics, counts
new threads since the last published issue, generates a single-sentence
"ticker" via Groq, and uploads pulse.json to Supabase Storage.

Usage: python pulse.py
"""
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from html import unescape

import httpx
from curl_cffi import requests as cffi_requests
from dotenv import load_dotenv

# Re-use everything we already wrote
from scrape import (
    BASE,
    BOARD,
    BROWSER_HEADERS,
    IST,
    compute_metrics,
    parse_iso,
    rate_activity,
    strip_html,
)
from compose import BOT_NAMES, is_bot_author

load_dotenv()

SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").rstrip("/")
SERVICE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or ""
BUCKET = os.getenv("EOI_BUCKET") or "eoi"

PROVIDER = (os.getenv("AI_PROVIDER") or "groq").lower().strip()
GROQ_MODEL = os.getenv("GROQ_MODEL") or "meta-llama/llama-4-scout-17b-16e-instruct"


TICKER_SHARED = """You are writing the LIVE TICKER line for East of Indus, a small newspaper covering the Induschan /b/ imageboard.

You are given the MOST RECENT posts on /b/ right now, freshly from the wire. Use these specific posts as your raw material; the ticker should reflect what Anon JUST said, not what the day's debates were.

Hard rules:
- Concrete and specific to the actual posts given. No abstractions.
- No moralising. No labels. No quotes around clauses.
- No em dashes. No "the board is...", "the discussion of...", "today's threads..." framing. Just what Anon is doing or just said.
- No clichés ("a range of", "diverse", "lively"). Plain, dry register.
- Profanity is allowed where it lands.
- No bot patterns (@jaggu, indusLLM, "Available tools", etc).
"""


TICKER_MODES = [
    {
        "name": "currently",
        "instr": """Write ONE sentence of the form:
"Anon is currently [doing X], [doing Y], and [doing Z]."
Three clauses, comma-joined, last with "and". Each clause a short verb phrase (gerund preferred), 4-9 words. Build each clause from a different post in the source.""",
    },
    {
        "name": "just_now",
        "instr": """Write THREE short separate sentences, each starting with one of "Just now:", "A moment ago:", or "Right now:". One short clause each, 5-10 words, naming what one anon just said or did. Pick three different posts.""",
    },
    {
        "name": "on_the_board",
        "instr": """Write ONE compact dispatch: "On the board: X. Y. Z." Three claims separated by periods. Each 4-8 words. No "currently", no "and", no "is" verb except where unavoidable. Plain telegraphic register.""",
    },
    {
        "name": "verbs_first",
        "instr": """Write THREE comma-joined verb-phrase fragments with no subject: "Arguing X. Asking Y. Posting Z." Start each with a present-participle verb. 5-9 words each.""",
    },
]


def pick_ticker_mode() -> dict:
    return random.choice(TICKER_MODES)


def build_ticker_system(mode: dict) -> str:
    return TICKER_SHARED + "\nFORMAT FOR THIS CALL:\n" + mode["instr"] + "\n\nReturn ONE JSON object only:\n{\"ticker\": \"...\"}"


def fetch_catalog():
    url = f"{BASE}/{BOARD}/catalog.json"
    r = cffi_requests.get(url, headers=BROWSER_HEADERS, timeout=30, impersonate="chrome131")
    if r.status_code >= 400:
        sys.exit(f"GET {url} -> {r.status_code}")
    return r.json()


def fetch_latest_issue_composed_at() -> datetime | None:
    """Read latest.json from Supabase to know when the last issue was composed."""
    if not SUPABASE_URL:
        return None
    url = f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/latest.json"
    try:
        r = httpx.get(url, timeout=15)
        if r.status_code >= 400:
            return None
        return parse_iso((r.json() or {}).get("composed_at"))
    except Exception:
        return None


def count_threads_since(catalog: list, since: datetime | None) -> int:
    if not since:
        return 0
    n = 0
    for t in catalog:
        d = parse_iso(t.get("date"))
        if d and d > since:
            n += 1
    return n


def freshest_replies(catalog: list, top_k_threads: int = 5, max_posts: int = 10) -> list[dict]:
    """Fetch the K most-recently-bumped threads in full, then return the N most recent
    posts across them (by timestamp). This drives a ticker that actually moves hour to
    hour, since replies happen every few minutes even when threads barely change."""
    def bumped_at(t):
        return parse_iso(t.get("bumped") or t.get("date")) or datetime.min.replace(tzinfo=timezone.utc)

    recent_threads = sorted(catalog, key=bumped_at, reverse=True)[:top_k_threads]
    print(f"  Fetching {len(recent_threads)} most-recently-bumped threads for fresh replies...")

    all_posts = []
    for t in recent_threads:
        tid = t.get("postId") or t.get("no")
        if not tid:
            continue
        try:
            url = f"{BASE}/{BOARD}/thread/{tid}.json"
            r = cffi_requests.get(url, headers=BROWSER_HEADERS, timeout=20, impersonate="chrome131")
            if r.status_code != 200:
                continue
            thread = r.json()
        except Exception as e:
            print(f"    skip thread {tid}: {e}")
            continue
        time.sleep(0.3)

        subject = (thread.get("subject") or "").strip()[:60]
        # OP post itself
        op_body = strip_html(thread.get("message") or thread.get("nomarkup") or "")
        if op_body and len(op_body) > 20 and not is_bot_author(thread.get("name")):
            all_posts.append({
                "no": thread.get("postId") or thread.get("no"),
                "body": op_body[:220],
                "created": thread.get("date"),
                "thread_subject": subject,
            })
        # Replies
        for r_post in (thread.get("replies") or []):
            if is_bot_author(r_post.get("name")):
                continue
            body = strip_html(r_post.get("message") or r_post.get("nomarkup") or "")
            if not body or len(body) < 20:
                continue
            # Skip obvious bot-tool content
            if re.search(r"@jaggu|indusLLM|IMPORTANT:\s*Respond|Available\s+tools|Write_file", body, re.IGNORECASE):
                continue
            all_posts.append({
                "no": r_post.get("postId") or r_post.get("no"),
                "body": body[:220],
                "created": r_post.get("date"),
                "thread_subject": subject,
            })

    def created_at(p):
        return parse_iso(p.get("created")) or datetime.min.replace(tzinfo=timezone.utc)

    all_posts.sort(key=created_at, reverse=True)
    return all_posts[:max_posts]


def call_groq_ticker(posts: list[dict], mode: dict) -> str:
    from groq import Groq
    key = os.getenv("GROQ_API_KEY")
    if not key:
        sys.exit("GROQ_API_KEY not set.")
    client = Groq(api_key=key)
    system = build_ticker_system(mode)
    payload = [{"body": p["body"]} for p in posts]
    user = "Most recent posts on /b/ (newest first):\n" + json.dumps(payload, ensure_ascii=False, indent=2)
    for attempt in range(2):
        try:
            resp = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format={"type": "json_object"},
                temperature=1.0,
                max_tokens=250,
            )
            raw = resp.choices[0].message.content
            obj = json.loads(raw)
            t = obj.get("ticker") or ""
            # Llama sometimes returns a list of clauses; flatten it
            if isinstance(t, list):
                t = " ".join(str(x) for x in t)
            ticker = str(t).strip()
            ticker = ticker.replace("—", ", ").replace("–", "-")
            if ticker:
                return ticker
        except Exception as e:
            print(f"    ticker call failed (attempt {attempt+1}): {str(e)[:200]}")
            if attempt == 0:
                time.sleep(2)
    return ""


def upload(path: str, body: bytes) -> None:
    if not SUPABASE_URL or not SERVICE_KEY:
        sys.exit("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY required.")
    url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{path}"
    r = httpx.post(
        url,
        content=body,
        headers={
            "Authorization": f"Bearer {SERVICE_KEY}",
            "apikey": SERVICE_KEY,
            "Content-Type": "application/json",
            "Cache-Control": "no-cache, max-age=0",
            "x-upsert": "true",
        },
        timeout=60,
    )
    if r.status_code >= 300:
        sys.exit(f"Upload {path} failed: {r.status_code} {r.text}")


def main() -> int:
    print("Fetching catalog...")
    catalog = fetch_catalog()
    if not isinstance(catalog, list):
        catalog = catalog.get("threads") or []
    print(f"  {len(catalog)} threads in catalog")

    now = datetime.now(timezone.utc)
    metrics = compute_metrics(catalog, now)
    print(f"  Activity: {metrics['threads_active_24h']} threads in 24h, rating={metrics['rating']}")

    issue_composed_at = fetch_latest_issue_composed_at()
    delta = count_threads_since(catalog, issue_composed_at)
    print(f"  New threads since last issue: {delta}")

    fresh = freshest_replies(catalog)
    mode = pick_ticker_mode()
    print(f"  Ticker source: {len(fresh)} freshest posts. Mode: {mode['name']}")
    ticker = call_groq_ticker(fresh, mode) if fresh else ""
    if not ticker:
        ticker = "Anon is currently posting, replying, and refusing to leave."
        print("  (ticker generation failed; using fallback)")
    else:
        print(f"  Ticker: {ticker}")

    pulse = {
        "ticker": ticker,
        "synced_at": now.isoformat(),
        "threads_since_issue": delta,
        "metrics": metrics,
    }

    body = json.dumps(pulse, ensure_ascii=False, indent=2).encode("utf-8")
    upload("pulse.json", body)
    print(f"Published pulse.json to {SUPABASE_URL}/storage/v1/object/public/{BUCKET}/pulse.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
