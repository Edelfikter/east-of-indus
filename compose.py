"""
Read the latest data/raw_*.json and compose a multi-section East of Indus issue
via per-thread AI calls plus one Observations call. Output: data/issue_<n>.json.

Multi-pass design:
- One AI call per assigned thread, focused on summarising that thread alone.
- One AI call for the Observations column, given a brief of each thread.
- Sleep between calls to stay under per-minute token caps.

Usage: python compose.py
Env: AI_PROVIDER=groq|anthropic|openai, corresponding API key.
"""
import hashlib
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DATA_DIR = Path(__file__).parent / "data"
PROVIDER = (os.getenv("AI_PROVIDER") or "groq").lower().strip()
GROQ_MODEL = os.getenv("GROQ_MODEL") or "meta-llama/llama-4-scout-17b-16e-instruct"
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL") or "claude-opus-4-7"
OPENAI_MODEL = os.getenv("OPENAI_MODEL") or "gpt-5"

MAX_REPLIES_PER_THREAD = int(os.getenv("MAX_REPLIES_PER_THREAD", "20"))  # bigger now, one thread per call
MAX_BODY_CHARS = int(os.getenv("MAX_BODY_CHARS", "400"))
MIN_OP_CHARS = int(os.getenv("MIN_OP_CHARS", "15"))
SLEEP_BETWEEN_CALLS_S = float(os.getenv("SLEEP_BETWEEN_CALLS_S", "16"))


SHARED_VOICE = """You are the sole writer of EAST OF INDUS, a small printed newspaper covering an anonymous Indian imageboard called Induschan /b/.

VOICE
Dry, observational, reportorial. Concrete. Specific. You report what posters actually said, claimed, asked, denied, or joked. The narrator may judge briefly when warranted (one short aside per piece, no more), but does not lecture, does not advise the public, and does not summarise in the abstract.

Posters are Anon by default. If a post has a "name" field set to something other than "Anonymous", use that name. Otherwise refer to them as Anon, an anon, one anon, another anon, a respondent, etc.

UNCENSORED
The source is an anonymous imageboard: profane, racist, obscene, often stupid. Report what was said as it was said. You may reproduce slurs when reporting what Anon said. You may use profanity when it serves the sentence. Do not euphemise topics. "A graphic scenario" is wrong; name what happened.

NO INVENTION
Every claim, place, number, person, action in your prose must come from a post in the source provided. Do not add background, do not extrapolate, do not invent recurring characters.

NO META SENTENCES
Do not write sentences about the thread itself ("the thread ran its course", "the discussion was lively", "the thread was filled with X"). Write what was said.

NO SENTENCE FRAGMENTS
Every sentence is complete with subject, verb, and full stop.

FORBIDDEN PHRASES
Never use any of: "a range of", "diverse", "eclectic", "reflects", "reflecting", "highlights", "underscores", "demonstrates", "showcases", "testament to", "deep-seated", "complexities of", "speaks to", "facilitate", "space where", "the importance of", "the need for", "greater understanding", "wider world", "personal growth", "self-improvement", "engaging", "vibrant", "rich tapestry", "delve", "delved", "sparked a heated", "a mix of", "humorous exchanges", "ran its course", "graphic scenario", "popped up", "garnered", "the thread was filled with", "got salty", "trolling the OP", "kicked off the day", "the conversation revolved around", "the discussion was lively", "marked by", "today's board was marked by", "weighing in", "the tenor of the board", "various brutal actions", "drew significant attention", "caught attention", "various suggestions", "lighthearted", "in contrast", "overall", "stood out", "stood notably", "in a notably", "notably toxic", "left the impression", "in a conventional manner", "in line with", "tone seemed", "one might wonder".

OTHER RULES
- No em dashes. Use commas, periods, parens, or hyphens.
- No thread numbers, post numbers, or reply counts in the prose.
- No identifiable real living people. Anon stays Anon.
- No moralising. You report. You do not say "India needs X" or "the board should consider Y."
- Paraphrase the source. Do not quote at length.

PARAGRAPH STRUCTURE
- Every article body must be broken into MULTIPLE PARAGRAPHS separated by blank lines (\\n\\n).
- The FIRST paragraph (the lede) is ONE or TWO short sentences. It names what Anon was doing in a single beat. No "the thread was about X" framing; just the thing itself.
- Each subsequent paragraph is 2-4 sentences and covers one development: a specific claim, a specific reply, a counter, a return.
- For a Leading article (350-550 words), aim for 5-7 paragraphs. For Discourse (200-350 words), 3-5 paragraphs. For Notices (60-120 words), 2-3 paragraphs. For Observations (120-180 words), 2-3 paragraphs.
- Do NOT produce a single wall-of-text paragraph. Do NOT cram everything into one or two paragraphs. Breaks are mandatory.
"""


THREAD_INSTR = """YOUR JOB FOR THIS CALL
You are writing ONE article for one thread of today's issue. The user message will give you the thread, its section assignment, and a target word count. Write the article in the voice above.

Open with what the poster was actually saying or doing in this thread. Use the replies for substance: name specific positions, refusals, counter-claims, jokes, returns. If the OP is short, do not say so; dive into what people in the thread said.

Return ONE JSON object only, no surrounding prose:
{
  "title": "short headline, no thread IDs, no reply counts",
  "body": "the article. Paragraphs separated by \\n\\n. Hit the target word count within +/- 20%."
}
"""


OBS_INSTR = """YOUR JOB FOR THIS CALL
You are writing the WEATHER FOR INDUS column for today's issue. It is the day's mood report, written in meteorological terms.

The title is ALWAYS exactly: "Weather for Indus today"

The body is 120-180 words. It reports the day's discourse climate as if it were weather. Each thread the user briefs you on becomes a weather phenomenon. Examples of the right register (do not reproduce, only follow):
- "A high pressure system of caste hatred sat over the board through the afternoon, drawing the usual squalls of reply."
- "Late in the evening, a fog of incel anxiety rolled in. No visibility for hours."
- "A scattered drizzle of livestream-death gossip in the morning, brief but persistent."
- "Buddhism continued its long bad season, with another front of insults blowing in from the west of the board."
- "The OBC reservation argument returned after a week's absence, like an old monsoon. Anon was glad to see it. Anon was always glad to see it."

Rules:
- ALL threads you are briefed on must be touched. None can be skipped.
- Use meteorological vocabulary throughout: pressure, fronts, squalls, fog, drizzle, monsoon, cold snap, heat wave, overcast, clear, drought, downpour, etc.
- The forecast tone is observational, not cute. NEVER write "Today's forecast: cloudy with a chance of X" or other novelty headline parody.
- Do not name the weather metaphor explicitly ("the discussion was like a storm"). Use it directly.
- Paragraphs: 2-3 short paragraphs, separated by blank lines.
- All other voice rules from the system message still apply: no clichés, no moralising, uncensored, etc.

Return ONE JSON object only, no surrounding prose:
{
  "title": "Weather for Indus today",
  "body": "the column. 120-180 words. 2-3 paragraphs."
}
"""


def next_issue_number() -> int:
    nums = []
    for p in DATA_DIR.glob("issue_*.json"):
        m = re.search(r"issue_(\d+)\.json$", p.name)
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


def latest_raw() -> Path:
    raws = sorted(DATA_DIR.glob("raw_*.json"))
    if not raws:
        sys.exit("No raw_*.json in data/. Run scrape.py first.")
    return raws[-1]


DEFAULT_NAMES = {"ananmas", "anonymous", "anon", "", None}
BOT_NAMES = {"jaggu", "indusllm"}  # exclude these handles from quotes / guest letters


def is_bot_author(raw_name) -> bool:
    return (raw_name or "").strip().lower() in BOT_NAMES


def fmt_post(p: dict) -> dict:
    """Reduce a normalized post. Normalize default board names ('Ananmas',
    'Anonymous') to 'Anon' so the AI does not invent a recurring character."""
    raw_name = p.get("name")
    name = raw_name if (raw_name and raw_name.strip().lower() not in DEFAULT_NAMES) else "Anon"
    return {
        "name": name,
        "time": p.get("created"),
        "body": (p.get("body") or "")[:MAX_BODY_CHARS],
    }


BOT_PATTERNS = re.compile(
    r"@jaggu|indusLLM|IMPORTANT:\s*Respond\s+with|Available\s+tools|Args:|Returns:|Write_file|path:\s*string|code:\s*string|only\s+json\s+text",
    re.IGNORECASE,
)


def is_bot_thread(thread: dict) -> bool:
    """True if a majority of the visible replies look like on-board LLM tool-call spam.
    These threads have lots of replies but no human substance — bad Leading material."""
    replies = thread.get("replies") or []
    if not replies:
        return False
    bot_count = sum(1 for r in replies if BOT_PATTERNS.search(r.get("body") or ""))
    # Also count OP being a bot prompt
    op_body = (thread.get("op", {}).get("body") or "")
    if BOT_PATTERNS.search(op_body):
        bot_count += 1
    return bot_count >= max(2, len(replies) // 2)


def assign_threads(raw: dict) -> list:
    """Trim threads, rank by reply count, demote bot-spam threads, assign sections.
    Returns list of thread dicts with: id, reply_count, op, replies, assignment, target_words."""
    threads = []
    for t in raw.get("threads", []):
        op_body = (t["op"].get("body") or "").strip()
        if len(op_body) < MIN_OP_CHARS:
            substantial = sum(1 for r in (t.get("replies") or []) if len((r.get("body") or "").strip()) > 30)
            if substantial < 6:
                continue
        replies_sorted = sorted(
            (r for r in (t.get("replies") or []) if (r.get("body") or "").strip()),
            key=lambda r: len(r.get("body") or ""),
            reverse=True,
        )
        threads.append({
            "id": t["id"],
            "reply_count": t.get("reply_count"),
            "op": fmt_post(t["op"]),
            "replies": [fmt_post(r) for r in replies_sorted[:MAX_REPLIES_PER_THREAD]],
        })

    threads.sort(key=lambda x: x.get("reply_count") or 0, reverse=True)

    # Push bot-spam threads to the back so they don't become the Leading
    real_threads = [t for t in threads if not is_bot_thread(t)]
    bot_threads = [t for t in threads if is_bot_thread(t)]
    if bot_threads:
        print(f"  Demoted {len(bot_threads)} bot thread(s) (LLM tool-call spam): {[t['id'] for t in bot_threads]}")
    threads = real_threads + bot_threads

    for i, t in enumerate(threads):
        if i == 0:
            t["assignment"] = "Leading"
            t["target_words"] = 450
        elif i in (1, 2):
            t["assignment"] = "Discourse"
            t["target_words"] = 275
        elif i in (3, 4):
            t["assignment"] = "Notices"
            t["target_words"] = 90
        else:
            t["assignment"] = "skip"
            t["target_words"] = 0
    return threads


# ---------- providers ----------

def call_groq(system: str, user: str, max_tokens: int = 1200) -> str:
    from groq import Groq
    key = os.getenv("GROQ_API_KEY")
    if not key:
        sys.exit("GROQ_API_KEY not set.")
    client = Groq(api_key=key)
    resp = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        temperature=0.8,
        max_tokens=max_tokens,
    )
    return resp.choices[0].message.content


def call_openai(system: str, user: str, max_tokens: int = 1200) -> str:
    from openai import OpenAI
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        sys.exit("OPENAI_API_KEY not set.")
    client = OpenAI(api_key=key)
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
    )
    return resp.choices[0].message.content


def call_anthropic(system: str, user: str, max_tokens: int = 1200) -> str:
    import anthropic
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("ANTHROPIC_API_KEY not set.")
    client = anthropic.Anthropic(api_key=key)
    resp = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=max_tokens,
        system=system + "\n\nReturn JSON only, no surrounding prose.",
        messages=[{"role": "user", "content": user}],
    )
    parts = []
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "".join(parts)


def call_ai(system: str, user: str, max_tokens: int = 1200) -> str:
    if PROVIDER == "anthropic":
        return call_anthropic(system, user, max_tokens)
    if PROVIDER == "openai":
        return call_openai(system, user, max_tokens)
    return call_groq(system, user, max_tokens)


# ---------- scrubbing ----------

THREAD_ID_RE = re.compile(r"\b(?:thread|post|reply)\s*(?:no\.?|number|#)?\s*\d+\b", re.IGNORECASE)
IN_THREAD_RE = re.compile(r"\bin\s+(?:the\s+)?thread\s*\d+\s*,?\s*", re.IGNORECASE)
REPLY_COUNT_RE = re.compile(r",?\s*(?:drew|got|with|attracted|had|gained|saw|gathered|received)\s+(?:over\s+|nearly\s+|some\s+|about\s+|around\s+)?\d+\s+(?:replies|posts|comments|responses)\.?", re.IGNORECASE)
BARE_REPLY_COUNT_RE = re.compile(r"\(\s*\d+\s+(?:replies|posts|comments|responses)\s*\)", re.IGNORECASE)


def scrub_text(text: str) -> str:
    if not text:
        return text
    text = IN_THREAD_RE.sub("", text)
    text = THREAD_ID_RE.sub(lambda m: "a thread" if m.group(0).lower().startswith("thread") else "a post", text)
    text = REPLY_COUNT_RE.sub(".", text)
    text = BARE_REPLY_COUNT_RE.sub("", text)
    text = re.sub(r"\.\s*\.", ".", text)
    text = re.sub(r"\s+,", ",", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z'\"])")
MAX_PARA_WORDS = 70


# ---------- Quote of the Day filter + AI pick ----------

URL_RE = re.compile(r"https?://", re.IGNORECASE)
BOT_QUOTE_RE = re.compile(r"@jaggu|indusLLM|IMPORTANT:\s*Respond|Available\s+tools|Write_file|Args:|Returns:", re.IGNORECASE)
REPLY_REF_RE = re.compile(r">>\d+")
PURE_NOISE_RE = re.compile(r"^(based|kek|bump|saged?|sage|cope|seethe|this|same|true|kys|kms|lol|lmao|hmm+|ok)\.?$", re.IGNORECASE)


def is_good_quote_candidate(body: str) -> bool:
    s = (body or "").strip()
    if len(s) < 60 or len(s) > 300:
        return False
    if URL_RE.search(s) or BOT_QUOTE_RE.search(s):
        return False
    if PURE_NOISE_RE.match(s):
        return False
    # Strip reply refs and require >50 chars of actual content
    stripped = REPLY_REF_RE.sub("", s).strip()
    if len(stripped) < 50:
        return False
    if " " not in stripped:
        return False
    return True


def collect_quote_candidates(raw_threads: list) -> list[dict]:
    """Walk every post in the raw scrape, keep ones that pass the heuristic filter."""
    out = []
    seen_bodies = set()
    for t in raw_threads:
        op = t.get("op") or {}
        op_subject = (op.get("subject") or "").strip()[:80]
        for post in [op] + (t.get("replies") or []):
            body = (post.get("body") or "").strip()
            if not is_good_quote_candidate(body):
                continue
            if body in seen_bodies:
                continue
            raw_name = post.get("name") or "Anonymous"
            if is_bot_author(raw_name):
                continue
            seen_bodies.add(body)
            name = raw_name if raw_name.strip().lower() not in DEFAULT_NAMES else "Anon"
            out.append({
                "body": body,
                "no": post.get("no"),
                "name": name,
                "thread_id": t.get("id"),
                "thread_subject": op_subject,
            })
    # Prefer longer, more substantive candidates; cap at 30
    out.sort(key=lambda x: len(x["body"]), reverse=True)
    return out[:30]


QUOTE_SYSTEM = """You are picking the QUOTE OF THE DAY for East of Indus, a newspaper covering the Induschan /b/ imageboard.

From the candidates the user gives you, pick exactly ONE post that could be printed verbatim on the front page of a small paper and survive without context. Good quotes have at least one of: distinctive voice, a strange specific image, a memorable line, sincere admission, dry wit, or genuinely bleak honesty.

AVOID:
- Pure slurs with no surrounding sentence
- Generic agreement ("based", "kek", "this", "saged")
- Sales talk, spam, bot prompts
- Posts that need the thread context to make sense
- Posts containing em dashes (—). Prefer posts with regular punctuation. If every candidate has em dashes, still pick the best one — the body will be scrubbed.

You may pick posts containing profanity or controversial views if the voice is good. The paper is uncensored. Do not moralise.

The "topic" label you return MUST NOT contain em dashes. Use commas or periods.

Return ONE JSON object only:
{
  "index": <integer index of the chosen candidate>,
  "topic": "short label, 3-7 words, of what the post is about, e.g. 'incel transformation advice', 'caste hatred', 'a strange dream'"
}
"""


def strip_em_dashes(text: str) -> str:
    """Replace em dashes (and en dashes) with hyphens. Paper-wide typographic rule."""
    if not text:
        return text
    # U+2014 em dash, U+2013 en dash, U+2015 horizontal bar
    return text.replace("—", ", ").replace("–", "-").replace("―", "-")


def compose_quote(candidates: list[dict]) -> dict | None:
    if not candidates:
        return None
    payload = [{"i": i, "body": c["body"]} for i, c in enumerate(candidates)]
    user = "Candidates from today's /b/:\n" + json.dumps(payload, ensure_ascii=False, indent=2)
    try:
        raw_resp = call_ai(QUOTE_SYSTEM, user, max_tokens=200)
        obj = extract_json(raw_resp)
    except Exception as e:
        print(f"  [quote] FAILED: {e}")
        return None

    idx = obj.get("index")
    if not isinstance(idx, int) or idx < 0 or idx >= len(candidates):
        print(f"  [quote] AI returned bad index: {idx}")
        return None

    chosen = candidates[idx]
    topic = strip_em_dashes((obj.get("topic") or "").strip()[:80])

    # Sanity scrub on output
    text = strip_em_dashes(chosen["body"].strip())
    if URL_RE.search(text) or BOT_QUOTE_RE.search(text):
        return None
    if len(text) > 320:
        text = text[:300].rstrip() + "..."

    return {
        "text": text,
        "post_no": chosen["no"],
        "name": chosen["name"],
        "topic": topic,
        "source_thread_id": chosen["thread_id"],
    }


# ---------- Letters from /b/ — !eastofindus marker submissions ----------

MARKER_RE = re.compile(r"^\s*!\s*eastofindus\s*[:\-]?\s*(.*)$", re.IGNORECASE)
MIN_LETTER_WORDS = 50
MAX_LETTERS_PER_ISSUE = 3
AUTHOR_RATE_LIMIT_DAYS = 7

CONSECUTIVE_CHAR_RE = re.compile(r"(.)\1{10,}", re.UNICODE)  # 11+ of same char in a row
LONG_WORD_RE = re.compile(r"\S{45,}")  # any token over 45 chars


def body_hash(body: str) -> str:
    """Stable 16-char hash of the body for cross-thread duplicate detection."""
    normalized = re.sub(r"\s+", " ", body).strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def passes_cheap_heuristics(body: str) -> tuple[bool, str]:
    """Returns (ok, reason). Catches low-effort trolling that survives word count."""
    letters = [c for c in body if c.isalpha()]
    if letters:
        upper_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
        if upper_ratio > 0.6:
            return False, "all-caps shouting"
    # Word repetition: same word >5x in a row
    words = body.lower().split()
    if words:
        consec = 1
        for i in range(1, len(words)):
            if words[i] == words[i - 1]:
                consec += 1
                if consec > 5:
                    return False, "word repetition"
            else:
                consec = 1
    # Same character repeated 11+ in a row (aaaaaaaaaaaa)
    if CONSECUTIVE_CHAR_RE.search(body):
        return False, "character spam"
    # Any single token >45 chars (concatenated slur or url-without-protocol)
    if LONG_WORD_RE.search(body):
        return False, "abnormal token"
    return True, ""


QUALITY_GATE_SYSTEM = """You are screening guest submissions to East of Indus for spam / low-effort trolling.

Each candidate is supposed to be a piece of writing the author wanted published in a small newspaper.

REJECT (ok: false) if the submission is one of:
- lorem ipsum, gibberish, or random keyboard mash
- pure repetition (same word, phrase, or idea over and over)
- shouting with no content
- advertising, sales pitch, or shilling
- a single sentence padded out with filler
- copy-paste of obviously well-known text (song lyrics, famous book passages, anthems)

ACCEPT (ok: true) if the submission is coherent prose making at least one real point. The paper is uncensored, so accept profane, crude, controversial, racist, or obscene content as long as it is REAL WRITING and not spam.

Return ONE JSON object only:
{"verdicts": [{"i": 0, "ok": true}, {"i": 1, "ok": false}, ...]}
"""


def quality_gate(letters: list[dict]) -> list[dict]:
    """Final AI screen for letters that passed heuristics. Drops spam/troll."""
    if not letters:
        return []
    payload = [{"i": i, "body": l["body"]} for i, l in enumerate(letters)]
    user = "Submissions to screen:\n" + json.dumps(payload, ensure_ascii=False, indent=2)
    try:
        raw_resp = call_ai(QUALITY_GATE_SYSTEM, user, max_tokens=400)
        obj = extract_json(raw_resp)
    except Exception as e:
        print(f"  [letters quality gate] FAILED ({e}); passing all candidates")
        return letters
    verdicts = {}
    for v in (obj.get("verdicts") or []):
        if isinstance(v, dict) and isinstance(v.get("i"), int):
            verdicts[v["i"]] = bool(v.get("ok"))
    accepted = []
    for i, l in enumerate(letters):
        ok = verdicts.get(i, True)  # default-accept if AI missed an index
        if ok:
            accepted.append(l)
        else:
            print(f"    AI rejected guest No. {l.get('post_no')}: spam/troll")
    return accepted


def collect_guest_letters(raw_threads: list, published: dict) -> list[dict]:
    """Find OPs whose subject starts with `!eastofindus`. Returns up to 3 letters,
    oldest first, after passing 4 defense layers:
      1. Hard filters (word count, URLs, bots, post-no dedup)
      2. Body-hash dedup (same text different post)
      3. Per-handle rate limit (named authors only)
      4. Cheap heuristics (caps, repetition, character spam)
      5. AI quality gate (final spam/troll screen)
    """
    from scrape import parse_iso as _parse_iso

    post_nos = published["post_nos"]
    body_hashes = published["body_hashes"]
    authors = published["authors"]
    now = datetime.now(timezone.utc)

    candidates = []
    for t in raw_threads:
        op = t.get("op") or {}
        subject = (op.get("subject") or "").strip()
        m = MARKER_RE.match(subject)
        if not m:
            continue
        title = m.group(1).strip()
        body = (op.get("body") or "").strip()
        body_clean = REPLY_REF_RE.sub("", body).strip()
        post_no = op.get("no")

        # Layer 1: hard filters
        if len(body_clean.split()) < MIN_LETTER_WORDS:
            continue
        if URL_RE.search(body_clean):
            continue
        if is_bot_author(op.get("name")):
            continue
        if post_no in post_nos:
            continue

        # Title fallback
        if not title:
            words = body_clean.split()
            title = " ".join(words[:8])
            if len(title) > 60:
                title = title[:60].rstrip() + "..."
            if not title:
                continue

        # Layer 2: body hash dedup (same text reposted)
        bhash = body_hash(body_clean)
        if bhash in body_hashes:
            print(f"    skip No. {post_no}: body already printed")
            continue

        # Layer 4: cheap heuristics (caps, repetition, etc.)
        ok, reason = passes_cheap_heuristics(body_clean)
        if not ok:
            print(f"    skip No. {post_no}: {reason}")
            continue

        # Layer 3: per-handle rate limit for named authors
        raw_name = op.get("name")
        name = raw_name if raw_name and raw_name.strip().lower() not in DEFAULT_NAMES else "Anon"
        if name != "Anon":
            last = authors.get(name)
            if last:
                last_dt = _parse_iso(last)
                if last_dt:
                    days_ago = (now - last_dt).days
                    if days_ago < AUTHOR_RATE_LIMIT_DAYS:
                        print(f"    skip No. {post_no}: '{name}' rate-limited ({days_ago}d ago)")
                        continue

        candidates.append({
            "title": title,
            "body": body_clean,
            "body_hash": bhash,
            "name": name,
            "post_no": post_no,
            "thread_id": t.get("id"),
            "created": op.get("created"),
        })

    # FIFO: oldest first
    def _key(c):
        ts = _parse_iso(c.get("created"))
        return ts or datetime.max.replace(tzinfo=timezone.utc)

    candidates.sort(key=_key)
    candidates = candidates[:MAX_LETTERS_PER_ISSUE]

    # Layer 5: AI quality gate (only call if we have something)
    if candidates:
        candidates = quality_gate(candidates)

    return candidates


def fetch_published_guests() -> dict:
    """Returns dict with sets of published post_nos, body hashes, and a {name → last
    published timestamp} map for rate limiting."""
    default = {"post_nos": set(), "body_hashes": set(), "authors": {}}
    base = (os.getenv("SUPABASE_URL") or "").rstrip("/")
    bucket = os.getenv("EOI_BUCKET") or "eoi"
    if not base:
        return default
    import httpx
    try:
        r = httpx.get(f"{base}/storage/v1/object/public/{bucket}/guests_published.json", timeout=15)
        if r.status_code == 200:
            data = r.json() or {}
            return {
                "post_nos": set(data.get("published_post_nos") or []),
                "body_hashes": set(data.get("published_body_hashes") or []),
                "authors": data.get("named_authors") or {},
            }
    except Exception:
        pass
    return default


def split_long_paragraphs(body: str) -> str:
    """If any paragraph exceeds MAX_PARA_WORDS, split it at sentence boundaries near
    the middle so the rendered article has visible breaks. Keeps existing paragraph
    breaks intact."""
    if not body:
        return body
    paras = re.split(r"\n\n+", body)
    out = []
    for p in paras:
        words = p.split()
        if len(words) <= MAX_PARA_WORDS:
            out.append(p)
            continue
        # Split into sentences and regroup into chunks of ~30-50 words each
        sentences = SENTENCE_SPLIT_RE.split(p)
        if len(sentences) <= 1:
            out.append(p)
            continue
        chunks = []
        current = []
        current_words = 0
        target = max(30, len(words) // ((len(words) // 45) + 1))
        for s in sentences:
            sw = len(s.split())
            if current and current_words + sw > target:
                chunks.append(" ".join(current))
                current = [s]
                current_words = sw
            else:
                current.append(s)
                current_words += sw
        if current:
            chunks.append(" ".join(current))
        out.append("\n\n".join(chunks))
    return "\n\n".join(out)


def extract_json(s: str) -> dict:
    s = s.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        start = s.find("{")
        end = s.rfind("}")
        if start >= 0 and end > start:
            return json.loads(s[start:end + 1])
        raise


# ---------- composers ----------

def compose_thread(thread: dict) -> dict | None:
    """One AI call for a single thread. Returns the article dict, or None on failure.
    Retries once on transient JSON validation errors from the model."""
    section = thread["assignment"]
    target = thread["target_words"]
    payload = {
        "section": section,
        "target_words": target,
        "thread": {
            "op": thread["op"],
            "replies": thread["replies"],
        },
    }
    user_msg = (
        f"Section: {section}\n"
        f"Target length: ~{target} words (acceptable: {int(target*0.8)}-{int(target*1.2)}).\n\n"
        f"Thread (one OP, then replies, each with name and time):\n"
        f"{json.dumps(payload['thread'], ensure_ascii=False, indent=2)}\n\n"
        f"Write one {section} article on this thread, following the voice and rules in the system message."
    )
    sys_msg = SHARED_VOICE + "\n" + THREAD_INSTR

    obj = None
    last_err = None
    for attempt in range(2):
        try:
            raw_resp = call_ai(sys_msg, user_msg, max_tokens=1400)
            obj = extract_json(raw_resp)
            break
        except Exception as e:
            last_err = e
            if attempt == 0:
                print(f"  [{section} | thread {thread['id']}] retry after: {str(e)[:80]}")
                time.sleep(2)
    if obj is None:
        print(f"  [{section} | thread {thread['id']}] FAILED after retry: {last_err}")
        return None

    title = scrub_text(obj.get("title") or "")
    body = split_long_paragraphs(scrub_text(obj.get("body") or ""))
    if not title or not body:
        return None

    return {
        "section": section,
        "title": title,
        "body": body,
        "flow": (section == "Leading"),
        "source_thread_id": thread["id"],
    }


def compose_observations(briefs: list) -> dict | None:
    """One AI call summarising the day's tenor across all covered threads."""
    user_msg = (
        "Today's covered threads (each with section, topic line, and approximate weight):\n"
        f"{json.dumps(briefs, ensure_ascii=False, indent=2)}\n\n"
        "Write today's Observations column following the voice and rules in the system message."
    )
    sys_msg = SHARED_VOICE + "\n" + OBS_INSTR

    try:
        raw_resp = call_ai(sys_msg, user_msg, max_tokens=600)
        obj = extract_json(raw_resp)
    except Exception as e:
        print(f"  [Observations] FAILED: {e}")
        return None

    body = split_long_paragraphs(scrub_text(obj.get("body") or ""))
    if not body:
        return None

    # Title is fixed for Observations
    return {
        "section": "Observations",
        "title": "Weather for Indus today",
        "body": body,
        "flow": False,
        "source_thread_id": None,
    }


def brief_of(thread: dict, article_title: str) -> dict:
    """A compact summary card for the Observations call."""
    op_line = (thread["op"].get("body") or "")[:200]
    return {
        "section": thread["assignment"],
        "title": article_title,
        "opener": op_line,
        "weight": thread.get("reply_count"),
    }


def main() -> int:
    raw_path = latest_raw()
    print(f"Reading {raw_path.name}")
    raw = json.loads(raw_path.read_text(encoding="utf-8"))

    issue_n = next_issue_number()
    issue_no_str = f"No. {issue_n:03d}"
    date_str = datetime.now(timezone.utc).strftime("%#d %B %Y") if os.name == "nt" else datetime.now(timezone.utc).strftime("%-d %B %Y")

    model_label = {
        "groq": GROQ_MODEL,
        "anthropic": ANTHROPIC_MODEL,
        "openai": OPENAI_MODEL,
    }.get(PROVIDER, "?")

    threads = assign_threads(raw)
    active = [t for t in threads if t["assignment"] != "skip"]
    print(f"Composing {issue_no_str} via {PROVIDER} ({model_label}).")
    print(f"  {len(active)} threads to cover + 1 Observations column.")

    articles = []
    briefs = []

    for i, t in enumerate(active, 1):
        print(f"  [{i}/{len(active)}] {t['assignment']} — thread {t['id']} (target ~{t['target_words']}w)...")
        art = compose_thread(t)
        if art:
            articles.append(art)
            briefs.append(brief_of(t, art["title"]))
        if i < len(active):
            time.sleep(SLEEP_BETWEEN_CALLS_S)

    # Observations call (Weather column)
    time.sleep(SLEEP_BETWEEN_CALLS_S)
    print(f"  [obs] Observations column...")
    obs = compose_observations(briefs)

    # Quote of the Day: heuristic prefilter + one AI pick
    time.sleep(SLEEP_BETWEEN_CALLS_S)
    quote_candidates = collect_quote_candidates(raw.get("threads") or [])
    print(f"  [quote] {len(quote_candidates)} candidates after heuristic filter")
    quote = compose_quote(quote_candidates)
    if quote:
        print(f"  [quote] picked No. {quote['post_no']} ({quote['name']}): {quote['text'][:60]}...")

    # Letters from /b/: OP submissions with !eastofindus marker
    published_post_nos = fetch_published_guests()
    guest_letters = collect_guest_letters(raw.get("threads") or [], published_post_nos)
    print(f"  [letters] {len(guest_letters)} new guest submission(s) this issue")
    for l in guest_letters:
        print(f"    No. {l['post_no']} ({l['name']}): {l['title'][:50]}")

    # Assemble: Observations first, Leading, Discourse, Notices
    final = []
    if obs:
        final.append(obs)
    order = {"Leading": 0, "Discourse": 1, "Notices": 2}
    articles.sort(key=lambda a: order.get(a["section"], 99))
    final.extend(articles)

    issue = {
        "issue_no": issue_no_str,
        "date": date_str,
        "metrics": raw.get("metrics") or {},
        "quote_of_day": quote,
        "guest_letters": guest_letters,
        "articles": final,
        "composed_at": datetime.now(timezone.utc).isoformat(),
        "source_raw": raw_path.name,
        "provider": PROVIDER,
        "model": model_label,
        "mode": "multi-pass",
    }

    out_path = DATA_DIR / f"issue_{issue_n:03d}.json"
    out_path.write_text(json.dumps(issue, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote {out_path}")
    print(f"  {len(final)} articles total.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
