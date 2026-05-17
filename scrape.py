"""
Scrape Induschan /b/ catalog + top threads into a single raw JSON file.
Usage: python scrape.py
Writes: data/raw_<UTC-timestamp>.json
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path

import httpx
from curl_cffi import requests as cffi_requests
from dotenv import load_dotenv

load_dotenv()

BASE = os.getenv("INDUSCHAN_BASE", "https://induschan-proxy.asphocal.workers.dev").rstrip("/")
BOARD = os.getenv("INDUSCHAN_BOARD", "b")
TOP_N = int(os.getenv("TOP_N_THREADS", "10"))
LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "14"))

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Referer": "https://induschan.site/b/",
}

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def strip_html(s: str) -> str:
    if not s:
        return ""
    # jschan wraps quotes and greentext in spans/anchors. Drop tags, keep text.
    txt = TAG_RE.sub(" ", s)
    txt = unescape(txt)
    return WS_RE.sub(" ", txt).strip()


def normalize_post(post: dict) -> dict:
    return {
        "no": post.get("postId") or post.get("no"),
        "name": post.get("name") or "Anonymous",
        "subject": post.get("subject") or "",
        "body": strip_html(post.get("message") or post.get("nomarkup") or ""),
        "created": post.get("date") or post.get("time"),
        "files": [
            {
                "name": f.get("originalFilename") or f.get("filename"),
                "mimetype": f.get("mimetype"),
            }
            for f in (post.get("files") or [])
        ],
    }


def fetch_json(client, path: str) -> dict | list:
    """Use curl_cffi with Chrome TLS impersonation to bypass Cloudflare fingerprint checks."""
    url = f"{BASE}/{path.lstrip('/')}"
    r = cffi_requests.get(url, headers=BROWSER_HEADERS, timeout=30, impersonate="chrome131")
    if r.status_code >= 400:
        raise RuntimeError(f"GET {url} -> {r.status_code}")
    return r.json()


def parse_iso(s: str | None):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


IST = timezone(timedelta(hours=5, minutes=30))
SPARK_BLOCKS = "▁▂▃▄▅▆▇█"


def sparkline(values: list[int]) -> str:
    if not values:
        return ""
    m = max(values) or 1
    return "".join(SPARK_BLOCKS[min(7, int(v / m * 7))] for v in values)


def rate_activity(bumps_last_hour: int) -> str:
    """Rating based on the most recent hour of activity, so it swings through the day.
    Order (low -> high): Dead, Rotting, Stale, Brisk, Active, Hyper, Crazy."""
    if bumps_last_hour >= 14: return "Crazy"
    if bumps_last_hour >= 9:  return "Hyper"
    if bumps_last_hour >= 6:  return "Active"
    if bumps_last_hour >= 4:  return "Brisk"
    if bumps_last_hour >= 2:  return "Stale"
    if bumps_last_hour >= 1:  return "Rotting"
    return "Dead"


def compute_metrics(catalog: list, now: datetime) -> dict:
    """Compute board activity stats from the full catalog (every thread, not just top N).
    Uses each thread's `bumped` timestamp as a per-hour activity unit."""
    bumps = []
    for t in catalog:
        b = parse_iso(t.get("bumped") or t.get("date"))
        if b:
            bumps.append(b)

    window_24h = now - timedelta(hours=24)
    window_7d = now - timedelta(days=7)
    bumps_24h = [b for b in bumps if b >= window_24h]
    bumps_7d = [b for b in bumps if b >= window_7d]

    # 24-hour hourly distribution, oldest -> newest
    hourly = [0] * 24
    for b in bumps_24h:
        hours_ago = (now - b).total_seconds() / 3600.0
        bucket = 23 - int(hours_ago)
        if 0 <= bucket < 24:
            hourly[bucket] += 1

    # Peak hour, expressed in IST
    peak_bucket = max(range(24), key=lambda i: hourly[i]) if any(hourly) else 0
    peak_dt = now - timedelta(hours=(23 - peak_bucket))
    peak_ist = peak_dt.astimezone(IST)
    peak_count = hourly[peak_bucket] if hourly else 0

    # Quietest hour (only meaningful if we have any activity at all)
    quiet_bucket = min(range(24), key=lambda i: hourly[i]) if any(hourly) else 0
    quiet_dt = now - timedelta(hours=(23 - quiet_bucket))
    quiet_ist = quiet_dt.astimezone(IST)

    # Sum lifetime replyposts as a long-trend proxy
    total_lifetime_posts = sum(int(t.get("replyposts") or 0) for t in catalog)

    return {
        "threads_in_catalog": len(catalog),
        "threads_active_24h": len(bumps_24h),
        "threads_active_7d": len(bumps_7d),
        "bumps_last_hour": hourly[23] if hourly else 0,
        "hourly_buckets_24h": hourly,
        "hourly_sparkline": sparkline(hourly),
        "peak_hour_ist": peak_ist.strftime("%H:00 IST"),
        "peak_count": peak_count,
        "quiet_hour_ist": quiet_ist.strftime("%H:00 IST"),
        "rating": rate_activity(hourly[23] if hourly else 0),
        "total_lifetime_posts": total_lifetime_posts,
        "computed_at": now.isoformat(),
    }


def main() -> int:
    print(f"Fetching catalog: {BASE}/{BOARD}/catalog.json")
    with httpx.Client(follow_redirects=True) as client:
        catalog = fetch_json(client, f"/{BOARD}/catalog.json")
        if not isinstance(catalog, list):
            # jschan sometimes wraps in {"threads": [...]}
            catalog = catalog.get("threads") or []

        def reply_count(t: dict) -> int:
            return int(t.get("replyposts") or t.get("replies") or 0)

        def bumped_at(t: dict):
            return parse_iso(t.get("bumped") or t.get("date"))

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=LOOKBACK_HOURS)

        # Compute board metrics from the full catalog before filtering
        metrics = compute_metrics(catalog, now)
        print(f"Activity: {metrics['threads_active_24h']} threads in 24h, rating={metrics['rating']}, peak={metrics['peak_hour_ist']}")

        # Recency filter: thread must have been bumped within the lookback window
        recent = [t for t in catalog if (bumped_at(t) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff]
        print(f"Catalog has {len(catalog)} threads, {len(recent)} bumped within {LOOKBACK_HOURS}h.")

        # Fallback: if nothing recent, take top by lifetime reply count (shouldn't happen on an active board)
        pool = recent if recent else catalog

        # Within the recency window, rank by reply count descending (most-discussed today)
        ranked = sorted(pool, key=reply_count, reverse=True)[:TOP_N]

        # Always include !eastofindus marker threads from the full catalog, even if they
        # didn't make the top N by reply count (guest submissions are usually brand new).
        marker_threads = [
            t for t in catalog
            if (t.get("subject") or "").lower().lstrip().startswith("!eastofindus")
        ]
        existing_ids = {t.get("postId") or t.get("no") for t in ranked}
        added = 0
        for mt in marker_threads:
            mt_id = mt.get("postId") or mt.get("no")
            if mt_id not in existing_ids:
                ranked.append(mt)
                existing_ids.add(mt_id)
                added += 1
        print(f"Top {len(ranked)} threads picked (+{added} guest-submission marker thread{'s' if added != 1 else ''}).")

        threads_out = []
        for i, op in enumerate(ranked, 1):
            tid = op.get("postId") or op.get("no")
            if not tid:
                continue
            print(f"  [{i}/{len(ranked)}] thread {tid} (replies={reply_count(op)})")
            try:
                full = fetch_json(client, f"/{BOARD}/thread/{tid}.json")
            except Exception as e:
                print(f"    skip: {e}")
                continue
            time.sleep(0.4)  # be polite

            op_norm = normalize_post(full)
            replies = [normalize_post(r) for r in (full.get("replies") or [])]
            threads_out.append({
                "id": tid,
                "url": f"{BASE}/{BOARD}/thread/{tid}.html",
                "reply_count": reply_count(op),
                "bumped": (op.get("bumped") or op.get("date")),
                "op": op_norm,
                "replies": replies,
            })

    out = {
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "board": BOARD,
        "source": f"{BASE}/{BOARD}/",
        "lookback_hours": LOOKBACK_HOURS,
        "window_start": cutoff.isoformat(),
        "thread_count": len(threads_out),
        "metrics": metrics,
        "threads": threads_out,
    }

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = DATA_DIR / f"raw_{ts}.json"
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
