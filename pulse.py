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
import re
import sys
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

load_dotenv()

SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").rstrip("/")
SERVICE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or ""
BUCKET = os.getenv("EOI_BUCKET") or "eoi"

PROVIDER = (os.getenv("AI_PROVIDER") or "groq").lower().strip()
GROQ_MODEL = os.getenv("GROQ_MODEL") or "meta-llama/llama-4-scout-17b-16e-instruct"


TICKER_SYSTEM = """You are writing the live ticker line for East of Indus, a small newspaper covering the Induschan /b/ imageboard.

You are given the subjects and first lines of the most-active threads on /b/ right now. Write ONE sentence summarising what Anon is currently doing, in the form:

"Anon is currently [doing X], [doing Y], and [doing Z]."

Examples of the right shape (do not reuse subject matter, only form):
"Anon is currently arguing caste, asking how to bulk, and posting fish gore."
"Anon is currently planning a town, mourning a streamer, and rejecting his mother."

Rules:
- One sentence. Three clauses joined by commas, last with "and".
- Each clause is a short verb phrase (gerund preferred). 4-9 words each.
- Concrete and specific to today's actual threads. No abstractions.
- No moralising, no quotes, no labels.
- No em dashes. No "the board is..." or "today's discussion..." framing. Just what Anon is doing.
- No clichés ("a range of", "diverse", "lively"). Plain, dry register.
- Profanity allowed where it serves the sentence.

Return ONE JSON object only:
{"ticker": "Anon is currently ..."}
"""


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


def top_thread_briefs(catalog: list, k: int = 7) -> list[dict]:
    """Pick top-k bumped-recently threads and return compact OP briefs."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)
    recent = [
        t for t in catalog
        if (parse_iso(t.get("bumped") or t.get("date")) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff
    ]
    recent.sort(key=lambda t: int(t.get("replyposts") or 0), reverse=True)
    briefs = []
    for t in recent[:k]:
        briefs.append({
            "subject": (t.get("subject") or "").strip()[:80],
            "opener": strip_html(t.get("message") or t.get("nomarkup") or "")[:160],
            "replies": int(t.get("replyposts") or 0),
        })
    return briefs


def call_groq_ticker(briefs: list[dict]) -> str:
    from groq import Groq
    key = os.getenv("GROQ_API_KEY")
    if not key:
        sys.exit("GROQ_API_KEY not set.")
    client = Groq(api_key=key)
    user = "Active threads on /b/ right now:\n" + json.dumps(briefs, ensure_ascii=False, indent=2)
    resp = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": TICKER_SYSTEM},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        temperature=0.8,
        max_tokens=200,
    )
    raw = resp.choices[0].message.content
    try:
        obj = json.loads(raw)
        return (obj.get("ticker") or "").strip()
    except Exception:
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

    briefs = top_thread_briefs(catalog)
    print(f"  Generating ticker from top {len(briefs)} threads...")
    ticker = call_groq_ticker(briefs)
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
