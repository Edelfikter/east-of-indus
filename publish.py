"""
Upload the latest data/issue_NNN.json to Supabase Storage.

Writes the file to two paths in the public `eoi` bucket:
  eoi/issue_NNN.json   archived under its issue number
  eoi/latest.json      overwritten every run, the file the theme fetches

Env required:
  SUPABASE_URL                 e.g. https://nfpdtjqncwibgyrzvffr.supabase.co
  SUPABASE_SERVICE_ROLE_KEY    service-role secret from Supabase dashboard
  EOI_BUCKET                   defaults to "eoi"

Usage: python publish.py
"""
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

DATA_DIR = Path(__file__).parent / "data"
SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").rstrip("/")
SERVICE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or ""
BUCKET = os.getenv("EOI_BUCKET") or "eoi"


def latest_issue() -> Path:
    issues = []
    for p in DATA_DIR.glob("issue_*.json"):
        m = re.search(r"issue_(\d+)\.json$", p.name)
        if m:
            issues.append((int(m.group(1)), p))
    if not issues:
        sys.exit("No issue_*.json in data/. Run compose.py first.")
    issues.sort()
    return issues[-1][1]


def upload(client: httpx.Client, path: str, body: bytes, upsert: bool) -> None:
    url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{path}"
    headers = {
        "Authorization": f"Bearer {SERVICE_KEY}",
        "apikey": SERVICE_KEY,
        "Content-Type": "application/json",
        "Cache-Control": "no-cache, max-age=0",
    }
    if upsert:
        headers["x-upsert"] = "true"
    r = client.post(url, content=body, headers=headers, timeout=60)
    # Duplicate for archive uploads is fine — issue already exists, treat as success.
    # Supabase returns HTTP 400 with statusCode "409" / error "Duplicate" in the body.
    if not upsert and r.status_code in (400, 409) and ("Duplicate" in r.text or '"409"' in r.text):
        print(f"  {path} already in bucket (kept)")
        return
    if r.status_code >= 300:
        sys.exit(f"Upload to {path} failed: {r.status_code} {r.text}")


def fetch_index() -> dict:
    """Fetch the existing index.json from Supabase. Returns empty index if not present."""
    url = f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/index.json"
    try:
        r = httpx.get(url, timeout=15)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict) and "issues" in data:
                return data
    except Exception:
        pass
    return {"updated_at": None, "issues": []}


def leading_title(issue: dict) -> str:
    for a in issue.get("articles", []):
        if a.get("section") == "Leading" and a.get("title"):
            return a["title"]
    return ""


def issue_period(composed_at: str | None) -> str:
    """Tag morning / evening based on UTC hour. Cron runs at 01:30 UTC (morning IST)
    and 15:30 UTC (evening IST)."""
    if not composed_at:
        return ""
    try:
        dt = datetime.fromisoformat(composed_at.replace("Z", "+00:00"))
        return "morning" if dt.hour < 12 else "evening"
    except Exception:
        return ""


def update_covered_threads(client: httpx.Client, issue_obj: dict) -> None:
    """Track which thread IDs this issue covered so the next compose can exclude them.
    Keeps a rolling window of the last 1 issue (1-issue cooldown). Bump the slice
    to recent_issues[:2] if you want a stricter "no repeats in 2 consecutive issues"."""
    ids = []
    for art in (issue_obj.get("articles") or []):
        tid = art.get("source_thread_id")
        if tid is not None and tid not in ids:
            ids.append(tid)
    if not ids:
        return
    url = f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/covered_threads.json"
    existing = {"recent_issues": []}
    try:
        r = httpx.get(url, timeout=15)
        if r.status_code == 200:
            existing = r.json() or existing
    except Exception:
        pass
    new_entry = {
        "issue_no": issue_obj.get("issue_no"),
        "composed_at": issue_obj.get("composed_at"),
        "thread_ids": ids,
    }
    recent = existing.get("recent_issues") or []
    recent.insert(0, new_entry)
    recent = recent[:1]  # 1-issue cooldown; change to [:2] for stricter
    out = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "recent_issues": recent,
    }
    body = json.dumps(out, ensure_ascii=False, indent=2).encode("utf-8")
    upload(client, "covered_threads.json", body, upsert=True)
    total = sum(len(e.get("thread_ids") or []) for e in recent)
    print(f"  covered_threads.json: {total} thread IDs across {len(recent)} recent issue(s)")


def update_published_guests(client: httpx.Client, issue_obj: dict) -> None:
    """Append this issue's guest letters to guests_published.json.
    Tracks post_nos (cross-issue dedup), body hashes (cross-thread dedup), and
    {name: last-published-iso} per named author (rate limiting)."""
    letters = issue_obj.get("guest_letters") or []
    if not letters:
        return
    url = f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/guests_published.json"
    existing = {"published_post_nos": [], "published_body_hashes": [], "named_authors": {}}
    try:
        r = httpx.get(url, timeout=15)
        if r.status_code == 200:
            data = r.json() or {}
            for k in existing.keys():
                if k in data:
                    existing[k] = data[k]
    except Exception:
        pass

    composed_at = issue_obj.get("composed_at") or datetime.now(timezone.utc).isoformat()
    for l in letters:
        if l.get("post_no") and l["post_no"] not in existing["published_post_nos"]:
            existing["published_post_nos"].append(l["post_no"])
        if l.get("body_hash") and l["body_hash"] not in existing["published_body_hashes"]:
            existing["published_body_hashes"].append(l["body_hash"])
        if l.get("name") and l["name"] != "Anon":
            existing["named_authors"][l["name"]] = composed_at

    out = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "published_post_nos": existing["published_post_nos"],
        "published_body_hashes": existing["published_body_hashes"],
        "named_authors": existing["named_authors"],
    }
    body = json.dumps(out, ensure_ascii=False, indent=2).encode("utf-8")
    upload(client, "guests_published.json", body, upsert=True)
    print(f"  guests_published.json: {len(out['published_post_nos'])} post_nos, "
          f"{len(out['published_body_hashes'])} body hashes, "
          f"{len(out['named_authors'])} named authors tracked")


def update_index(client: httpx.Client, issue_filename: str, issue_obj: dict) -> None:
    """Append (or replace) this issue's metadata in index.json and re-upload."""
    index = fetch_index()
    issues = index.get("issues", [])
    entry = {
        "issue_no": issue_obj.get("issue_no"),
        "date": issue_obj.get("date"),
        "composed_at": issue_obj.get("composed_at"),
        "period": issue_period(issue_obj.get("composed_at")),
        "leading_title": leading_title(issue_obj),
        "rating": (issue_obj.get("metrics") or {}).get("rating"),
        "filename": issue_filename,
        "url": f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{issue_filename}",
    }
    # Replace if this issue is already indexed, otherwise prepend (newest first).
    issues = [e for e in issues if e.get("issue_no") != entry["issue_no"]]
    issues.insert(0, entry)
    index["issues"] = issues
    index["updated_at"] = datetime.now(timezone.utc).isoformat()
    body = json.dumps(index, ensure_ascii=False, indent=2).encode("utf-8")
    upload(client, "index.json", body, upsert=True)
    print(f"  index.json updated, {len(issues)} issues archived")


def main() -> int:
    if not SUPABASE_URL or not SERVICE_KEY:
        sys.exit("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in env.")

    issue_path = latest_issue()
    print(f"Uploading {issue_path.name} to bucket '{BUCKET}'")
    body = issue_path.read_bytes()
    issue_obj = json.loads(body.decode("utf-8"))

    with httpx.Client() as client:
        # Archive (don't overwrite if it already exists — issues shouldn't be rewritten)
        upload(client, issue_path.name, body, upsert=False)
        # Latest pointer (always overwrite)
        upload(client, "latest.json", body, upsert=True)
        # Maintain the public index of all archived issues
        update_index(client, issue_path.name, issue_obj)
        # Track covered thread IDs so the next compose skips them (1-issue cooldown)
        update_covered_threads(client, issue_obj)
        # Mark any guest letters in this issue as published so they don't reprint
        update_published_guests(client, issue_obj)

    public_url = f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/latest.json"
    print(f"Published. latest.json now at:\n  {public_url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
