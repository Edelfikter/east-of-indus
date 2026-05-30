"""
Indiachan.top (Simplechan engine) HTML scraper.

Replaces the dead Induschan jschan JSON API. Two functions:

    fetch_catalog(board)      -> list of catalog entries (newest-first by site order)
    fetch_thread(board, tid)  -> OP dict with `replies` list

Both return plain dicts in a consistent internal shape. The rest of the EoI
pipeline (scrape.py, pulse.py, compose.py) consumes these shapes directly,
which is why this module is the only place that knows about HTML parsing.

The base URL comes from IMAGEBOARD_BASE; INDUSCHAN_BASE is read as a
deprecated fallback for one rollout cycle. The default is the public CF
Worker proxy. Override to https://indiachan.top to bypass the proxy
(works because Indiachan does not gate behind Cloudflare anti-bot).
"""
import os
import re
import time
from datetime import datetime, timezone
from html import unescape
from typing import Optional

import httpx
from bs4 import BeautifulSoup, NavigableString


BASE = (
    os.getenv("IMAGEBOARD_BASE")
    or os.getenv("INDUSCHAN_BASE")
    or "https://indiachan.top"
).rstrip("/")

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://indiachan.top/",
}


# CSS selectors and class names — patch here if Simplechan ships a redesign.
POST_CONTAINER_SEL = "div.post-container"
POST_THREADVIEW_SEL = "div.post-threadview"
OP_CLASS = "op-post"
CATALOG_LINK_SEL = "a.thread-catalog-link"
POST_MESSAGE_SEL = "blockquote.post-message"
POST_DATE_SEL = "span.post-date"
POST_NAME_SEL = "span.bold.name"
POST_TITLE_SEL = "span.bold.title"
FILE_INFO_SEL = "div.file-info"
GREENTEXT_CLASS = "greentext"
QUOTE_LINK_CLASS = "quote-link"

THREAD_HREF_RE = re.compile(r"/boards/[^/]+/thread/(\d+)")
PID_RE = re.compile(r"^p(\d+)$")
WS_RE = re.compile(r"[ \t]+")
NL3_RE = re.compile(r"\n{3,}")

EXT_MIME = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
    "mp4": "video/mp4",
    "webm": "video/webm",
}


def _epoch_to_iso(sec) -> str:
    if sec is None or sec == "":
        return ""
    try:
        return datetime.fromtimestamp(int(sec), tz=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return ""


def _date_to_iso(s: Optional[str]) -> str:
    """Parse Simplechan's post-page date format: '2026-05-21 18:19:33 UTC'."""
    if not s:
        return ""
    s = s.strip().replace(" UTC", "")
    try:
        return (
            datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
            .replace(tzinfo=timezone.utc)
            .isoformat()
        )
    except ValueError:
        return ""


def _fetch_html(url: str, *, timeout: float = 30.0) -> str:
    with httpx.Client(
        headers=BROWSER_HEADERS, follow_redirects=True, timeout=timeout
    ) as c:
        r = c.get(url)
        if r.status_code >= 400:
            raise RuntimeError(f"GET {url} -> {r.status_code}")
        return r.text


def _classes(el) -> list:
    return el.get("class") or []


def _extract_body(blockquote) -> str:
    """blockquote.post-message → plain text.

    Greentext spans become '>text' so quoted lines survive stripping.
    Quote-link anchors keep their '>>NNNN' text. <br> becomes newline.
    Other inline tags get unwrapped to their text content.
    """
    if blockquote is None:
        return ""
    parts = []
    for el in blockquote.children:
        if isinstance(el, NavigableString):
            parts.append(str(el))
            continue
        if el.name == "br":
            parts.append("\n")
            continue
        cls = _classes(el)
        if el.name == "span" and GREENTEXT_CLASS in cls:
            txt = el.get_text()
            parts.append(txt if txt.lstrip().startswith(">") else ">" + txt)
            continue
        if el.name == "a" and QUOTE_LINK_CLASS in cls:
            parts.append(el.get_text())
            continue
        parts.append(el.get_text())
    text = unescape("".join(parts))
    text = WS_RE.sub(" ", text)
    text = NL3_RE.sub("\n\n", text)
    return text.strip()


def _parse_files(post) -> list:
    """A post can have 0-N file attachments. Simplechan shows them as
    <div class=file-info> blocks each containing an <a> with title=filename
    and href=/static/uploads/NNN.ext. Pull what's available; mimetype is
    derived from the URL extension (cheap, accurate enough for our use)."""
    out = []
    for fi in post.select(FILE_INFO_SEL):
        a = fi.find("a")
        if not a:
            continue
        name = a.get("title") or a.get_text(strip=True)
        href = a.get("href") or ""
        ext = href.rsplit(".", 1)[-1].lower() if "." in href else ""
        mime = EXT_MIME.get(ext, "application/octet-stream")
        out.append({"name": name, "mimetype": mime})
    return out


def _parse_post(post) -> Optional[dict]:
    """Parse one <div class=post-threadview>. Returns None if the post number
    can't be determined (defensive — shouldn't happen on real pages)."""
    pid = post.get("id") or ""
    m = PID_RE.match(pid)
    if m:
        no = int(m.group(1))
    else:
        bn = post.get("data-board-num")
        if not bn:
            return None
        try:
            no = int(bn)
        except ValueError:
            return None

    name_el = post.select_one(POST_NAME_SEL)
    name = ""
    if name_el is not None:
        name = name_el.get("data-full-text") or name_el.get_text(strip=True) or ""
    if not name:
        name = "Anonymous"

    title_el = post.select_one(POST_TITLE_SEL)
    subject = ""
    if title_el is not None:
        subject = title_el.get("data-full-text") or title_el.get_text(strip=True) or ""

    date_el = post.select_one(POST_DATE_SEL)
    created = ""
    if date_el is not None:
        created = _date_to_iso(date_el.get("data-full-date") or date_el.get_text(strip=True))

    body = _extract_body(post.select_one(POST_MESSAGE_SEL))

    return {
        "no": no,
        "name": name,
        "subject": subject,
        "body": body,
        "created": created,
        "files": _parse_files(post),
    }


def fetch_catalog(board: str) -> list:
    """Fetch /boards/<board>/catalog and return parsed entries:

        {no, subject, body_teaser, bumped (ISO), created (ISO),
         reply_count, pinned, files: []}
    """
    url = f"{BASE}/boards/{board}/catalog"
    soup = BeautifulSoup(_fetch_html(url), "html.parser")
    out = []
    for card in soup.select(POST_CONTAINER_SEL):
        link = card.select_one(CATALOG_LINK_SEL)
        href = link.get("href") if link else ""
        m = THREAD_HREF_RE.search(href or "")
        if not m:
            continue
        tid = int(m.group(1))

        try:
            reply_count = int(card.get("data-replies") or 0)
        except ValueError:
            reply_count = 0

        # Subject + teaser: live in the last non-".small" <div> child of the card.
        subject = ""
        body_teaser = ""
        bottom_div = None
        for d in card.find_all("div", recursive=False):
            if "small" in _classes(d):
                continue
            bottom_div = d
        if bottom_div is not None:
            strong = bottom_div.find("strong")
            if strong is not None:
                subject = strong.get_text(strip=True)
                strong.extract()
            body_teaser = bottom_div.get_text(separator=" ", strip=True).lstrip(":").strip()

        out.append({
            "no": tid,
            "subject": subject,
            "body_teaser": body_teaser,
            "bumped": _epoch_to_iso(card.get("data-bump")),
            "created": _epoch_to_iso(card.get("data-created")),
            "reply_count": reply_count,
            "pinned": (card.get("data-pinned") or "0") == "1",
            "files": [],
        })
    return out


def fetch_thread(board: str, tid) -> dict:
    """Fetch /boards/<board>/thread/<tid>/ and return:

        {no, name, subject, body, created, files, replies: [<same shape>]}

    OP is identified by the `op-post` class on its post-threadview div.
    Raises RuntimeError if no posts are found (e.g. thread deleted)."""
    url = f"{BASE}/boards/{board}/thread/{tid}/"
    soup = BeautifulSoup(_fetch_html(url), "html.parser")
    op = None
    replies = []
    for post in soup.select(POST_THREADVIEW_SEL):
        parsed = _parse_post(post)
        if not parsed:
            continue
        if op is None and OP_CLASS in _classes(post):
            op = parsed
        else:
            replies.append(parsed)
    if op is None:
        if not replies:
            raise RuntimeError(f"No posts found in thread {tid}")
        op = replies.pop(0)
    op["replies"] = replies
    return op


if __name__ == "__main__":
    import json
    import sys

    board = sys.argv[1] if len(sys.argv) > 1 else "b"
    cat = fetch_catalog(board)
    print(f"catalog: {len(cat)} entries, first 3:")
    print(json.dumps(cat[:3], indent=2, ensure_ascii=False))
    if cat:
        tid = cat[0]["no"]
        time.sleep(0.5)
        t = fetch_thread(board, tid)
        print(f"\nthread {tid}: OP + {len(t['replies'])} replies")
        print(json.dumps({**t, "replies": t["replies"][:2]}, indent=2, ensure_ascii=False))
