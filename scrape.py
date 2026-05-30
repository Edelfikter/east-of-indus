"""
Scrape Indiachan.top /b/ (Simplechan engine) into a single raw JSON file.
Usage: python scrape.py
Writes: data/raw_<UTC-timestamp>.json

HTML parsing lives in simplechan.py. This file ranks threads, fetches the
top N in full, computes board metrics, and writes out the raw bundle that
compose.py consumes.
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

import simplechan
from simplechan import BASE, BROWSER_HEADERS

load_dotenv()

BOARD = os.getenv("IMAGEBOARD_BOARD", os.getenv("INDUSCHAN_BOARD", "b"))
TOP_N = int(os.getenv("TOP_N_THREADS", "10"))
LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "14"))

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def strip_html(s: str) -> str:
    """Kept for compatibility with pulse.py callers. simplechan already
    returns plain text, but downstream code occasionally feeds in raw
    Simplechan HTML snippets (e.g. when reusing the catalog teaser)."""
    if not s:
        return ""
    txt = re.sub(r"<[^>]+>", " ", s)
    from html import unescape
    txt = unescape(txt)
    return re.sub(r"\s+", " ", txt).strip()


IST = timezone(timedelta(hours=5, minutes=30))
SPARK_BLOCKS = "▁▂▃▄▅▆▇█"


def sparkline(values: list) -> str:
    if not values:
        return ""
    m = max(values) or 1
    return "".join(SPARK_BLOCKS[min(7, int(v / m * 7))] for v in values)


def rate_activity(bumps_last_hour: int) -> str:
    """Order (low -> high): Dead, Rotting, Stale, Brisk, Active, Hyper, Crazy."""
    if bumps_last_hour >= 20: return "Crazy"
    if bumps_last_hour >= 14: return "Hyper"
    if bumps_last_hour >= 7:  return "Active"
    if bumps_last_hour >= 4:  return "Brisk"
    if bumps_last_hour >= 2:  return "Stale"
    if bumps_last_hour >= 1:  return "Rotting"
    return "Dead"


def compute_metrics(catalog: list, now: datetime) -> dict:
    """Board activity stats from the full catalog. Each entry must have
    `bumped` (ISO) and `reply_count` (int)."""
    bumps = [parse_iso(t.get("bumped")) for t in catalog]
    bumps = [b for b in bumps if b]

    window_24h = now - timedelta(hours=24)
    window_7d = now - timedelta(days=7)
    bumps_24h = [b for b in bumps if b >= window_24h]
    bumps_7d = [b for b in bumps if b >= window_7d]

    hourly = [0] * 24
    for b in bumps_24h:
        hours_ago = (now - b).total_seconds() / 3600.0
        bucket = 23 - int(hours_ago)
        if 0 <= bucket < 24:
            hourly[bucket] += 1

    peak_bucket = max(range(24), key=lambda i: hourly[i]) if any(hourly) else 0
    peak_dt = now - timedelta(hours=(23 - peak_bucket))
    peak_ist = peak_dt.astimezone(IST)
    peak_count = hourly[peak_bucket] if hourly else 0

    quiet_bucket = min(range(24), key=lambda i: hourly[i]) if any(hourly) else 0
    quiet_dt = now - timedelta(hours=(23 - quiet_bucket))
    quiet_ist = quiet_dt.astimezone(IST)

    total_lifetime_posts = sum(int(t.get("reply_count") or 0) for t in catalog)

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
    print(f"Fetching catalog: {BASE}/boards/{BOARD}/catalog")
    catalog = simplechan.fetch_catalog(BOARD)

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=LOOKBACK_HOURS)

    metrics = compute_metrics(catalog, now)
    print(
        f"Activity: {metrics['threads_active_24h']} threads in 24h, "
        f"rating={metrics['rating']}, peak={metrics['peak_hour_ist']}"
    )

    # Recency filter — exclude pinned threads from the ranking pool (they never
    # bump but always sit at the top of the catalog) and require a recent bump.
    def bumped_at(t):
        return parse_iso(t.get("bumped")) or datetime.min.replace(tzinfo=timezone.utc)

    recent = [
        t for t in catalog
        if not t.get("pinned") and bumped_at(t) >= cutoff
    ]
    print(
        f"Catalog has {len(catalog)} threads, "
        f"{len(recent)} bumped within {LOOKBACK_HOURS}h (pinned excluded)."
    )

    pool = recent if recent else [t for t in catalog if not t.get("pinned")]
    ranked = sorted(pool, key=lambda t: int(t.get("reply_count") or 0), reverse=True)[:TOP_N]

    # Always include !eastofinch marker threads (guest submissions are usually
    # brand-new and short, so they rarely make the top-N reply ranking).
    marker_threads = [
        t for t in catalog
        if (t.get("subject") or "").lower().lstrip().startswith("!eastofinch")
    ]
    existing_ids = {t.get("no") for t in ranked}
    added = 0
    for mt in marker_threads:
        if mt.get("no") not in existing_ids:
            ranked.append(mt)
            existing_ids.add(mt.get("no"))
            added += 1
    print(f"Top {len(ranked)} threads picked (+{added} guest-submission marker thread{'s' if added != 1 else ''}).")

    threads_out = []
    for i, cat_entry in enumerate(ranked, 1):
        tid = cat_entry.get("no")
        if not tid:
            continue
        reply_count = int(cat_entry.get("reply_count") or 0)
        print(f"  [{i}/{len(ranked)}] thread {tid} (replies={reply_count})")
        try:
            thread = simplechan.fetch_thread(BOARD, tid)
        except Exception as e:
            print(f"    skip: {e}")
            continue
        time.sleep(0.4)  # be polite

        replies = thread.pop("replies", [])
        threads_out.append({
            "id": tid,
            "url": f"{BASE}/boards/{BOARD}/thread/{tid}/",
            "reply_count": reply_count,
            "bumped": cat_entry.get("bumped"),
            "op": thread,
            "replies": replies,
        })

    out = {
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "board": BOARD,
        "source": f"{BASE}/boards/{BOARD}/",
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
