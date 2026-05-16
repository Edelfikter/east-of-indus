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
    if r.status_code >= 300:
        sys.exit(f"Upload to {path} failed: {r.status_code} {r.text}")


def main() -> int:
    if not SUPABASE_URL or not SERVICE_KEY:
        sys.exit("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in env.")

    issue_path = latest_issue()
    print(f"Uploading {issue_path.name} to bucket '{BUCKET}'")
    body = issue_path.read_bytes()

    with httpx.Client() as client:
        # Archive (don't overwrite if it already exists — issues shouldn't be rewritten)
        upload(client, issue_path.name, body, upsert=False)
        # Latest pointer (always overwrite)
        upload(client, "latest.json", body, upsert=True)

    public_url = f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/latest.json"
    print(f"Published. latest.json now at:\n  {public_url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
