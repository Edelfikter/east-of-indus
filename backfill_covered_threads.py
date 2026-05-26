"""
One-time backfill: rebuild covered_threads.json from every archived issue in
Supabase so the permanent cooldown knows every thread the paper has ever covered.

Reads index.json, fetches every issue_NNN.json, extracts source_thread_ids from
articles, writes the consolidated file back to the bucket.

Usage: python backfill_covered_threads.py
Env: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, EOI_BUCKET (default 'eoi')
"""
import json
import os
import sys
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").rstrip("/")
SERVICE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or ""
BUCKET = os.getenv("EOI_BUCKET") or "eoi"


def public(path: str) -> str:
    return f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{path}"


def upload(client: httpx.Client, path: str, body: bytes) -> None:
    url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{path}"
    headers = {
        "Authorization": f"Bearer {SERVICE_KEY}",
        "apikey": SERVICE_KEY,
        "Content-Type": "application/json",
        "Cache-Control": "no-cache, max-age=0",
        "x-upsert": "true",
    }
    r = client.post(url, content=body, headers=headers, timeout=60)
    if r.status_code >= 300:
        sys.exit(f"Upload failed: {r.status_code} {r.text}")


def main() -> int:
    if not SUPABASE_URL or not SERVICE_KEY:
        sys.exit("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set.")

    with httpx.Client() as client:
        idx = client.get(public("index.json"), timeout=30).json()
        issues = idx.get("issues") or []
        print(f"Found {len(issues)} archived issues. Fetching each…")

        # Newest first in index; build entries in same order
        entries = []
        for meta in issues:
            fn = meta.get("filename")
            if not fn:
                continue
            try:
                r = client.get(public(fn), timeout=30)
                if r.status_code != 200:
                    print(f"  {fn}: HTTP {r.status_code}, skipped")
                    continue
                obj = r.json()
            except Exception as e:
                print(f"  {fn}: {e}, skipped")
                continue

            tids = []
            for art in (obj.get("articles") or []):
                tid = art.get("source_thread_id")
                if tid is not None and tid not in tids:
                    tids.append(tid)
            if not tids:
                # Pre-multi-pass issues used different keys; try a couple of fallbacks
                for k in ("thread_id", "source_id"):
                    for art in (obj.get("articles") or []):
                        tid = art.get(k)
                        if tid is not None and tid not in tids:
                            tids.append(tid)

            entries.append({
                "issue_no": obj.get("issue_no"),
                "composed_at": obj.get("composed_at"),
                "thread_ids": tids,
            })
            print(f"  {fn}: {len(tids)} thread ID(s)")

        out = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "recent_issues": entries,
        }
        body = json.dumps(out, ensure_ascii=False, indent=2).encode("utf-8")
        upload(client, "covered_threads.json", body)

        total_ids = sum(len(e["thread_ids"]) for e in entries)
        unique_ids = len({tid for e in entries for tid in e["thread_ids"]})
        print(f"\nWrote covered_threads.json: {len(entries)} issues, "
              f"{total_ids} total IDs, {unique_ids} unique threads.")
        print(f"  {public('covered_threads.json')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
