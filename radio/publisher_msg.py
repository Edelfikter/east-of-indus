"""
From the Publisher: render one admin-written note in William's voice and splice
it into the live manifest.json. Triggered by the radio-message.yml workflow,
which the Supabase edge function dispatches when the admin hits "Send to William".

Reuses run_block's render() + Supabase helpers so the voice + storage path are
identical to a normal block. Run:  python -m radio.publisher_msg
"""
import json
import os
import time
import urllib.request

from radio import run_block as rb

BASE = f"{rb.SUPABASE_URL}/storage/v1/object/public/{rb.BUCKET}/"


def fetch_manifest():
    req = urllib.request.Request(BASE + "manifest.json", headers={"Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def upload(path_or_bytes, dest, ctype):
    data = path_or_bytes if isinstance(path_or_bytes, (bytes, bytearray)) else path_or_bytes.read_bytes()
    st, body = rb.sb("POST", f"{rb.SUPABASE_URL}/storage/v1/object/{rb.BUCKET}/{dest}",
                     data=data, ctype=ctype, upsert=True)
    return st if st < 300 else f"FAIL {st} {body[:120]}"


def main():
    if not rb.SERVICE_KEY:
        raise SystemExit("SUPABASE_SERVICE_ROLE_KEY not set")
    title = (os.getenv("PUB_TITLE") or "From the Publisher").strip()
    text = (os.getenv("PUB_TEXT") or "").strip()
    try:
        at = int(os.getenv("PUB_AT_INDEX") or "-1")
    except ValueError:
        at = -1
    if len(text) < 2:
        raise SystemExit("empty message")

    print(f"=== FROM THE PUBLISHER · {title[:60]} · at_index={at} ===")
    stem = f"seg_pub_{int(time.time())}"
    cues = rb.render(text, stem, *rb.HOST_VOICE)          # William, same processing as any segment
    mp3 = rb.WORK / f"{stem}.mp3"
    dur = rb.dur_of(mp3)
    print("audio:", upload(mp3, f"{stem}.mp3", "audio/mpeg"), f"({dur:.0f}s)")

    item = {
        "type": "segment", "kind": "publisher",
        "label": "FROM THE PUBLISHER — " + title[:60],
        "audio": f"{stem}.mp3", "cues": cues, "duration": round(dur, 3),
    }

    man = fetch_manifest()
    items = man.get("items", [])
    if at < 0 or at > len(items):
        at = len(items)
    items.insert(at, item)
    man["items"] = items
    payload = json.dumps(man, ensure_ascii=False).encode("utf-8")
    print("manifest:", upload(payload, "manifest.json", "application/json"))
    print(f"DONE: spliced at {at}, {dur:.0f}s, {len(items)} items total")


if __name__ == "__main__":
    main()
