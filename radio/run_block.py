"""
Inch Radio, one block: generate -> render -> publish. Headless, repo-relative,
service key + groq key from env (CI secrets). edge-tts + ffmpeg required.

Run from the eoi repo root:  python -m radio.run_block
Local convenience: reads eoi/.env via python-dotenv if present.
"""
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent          # eoi/radio
REPO = ROOT.parent                               # eoi/
load_dotenv(REPO / ".env")
sys.path.insert(0, str(REPO))                    # so `import simplechan` resolves
import simplechan
import groq_limits

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

WORK = ROOT / "work"
WORK.mkdir(exist_ok=True)

GROQ_MODEL = os.getenv("GROQ_MODEL") or "qwen/qwen3.8-27b"
BOARD = "b"
SLEEP = float(os.getenv("RADIO_SLEEP", "6"))
SUPABASE_URL = "https://nfpdtjqncwibgyrzvffr.supabase.co"
BUCKET = "radio"
SERVICE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SB_SERVICE_KEY") or ""

HOST_VOICE = ("en-AU-WilliamNeural", "-12%", "-12Hz")
GUEST_VOICE = ("en-GB-RyanNeural", "-6%", "-6Hz")
PLAYLISTS = ["PL8F6B0753B2CCA128", "PLJqaCrWsnLdDBWSIF6UVSCRkohB4KZTC0", "PL8XkUYLRCrx-bAuQmPfP1g9dpZAdQItii"]
PRONOUNCE = [("Indiachan", "India Chan")]
FFMPEG = shutil.which("ffmpeg") or r"C:\ffmpeg-8.0-essentials_build\bin\ffmpeg.exe"
FFPROBE = shutil.which("ffprobe") or r"C:\ffmpeg-8.0-essentials_build\bin\ffprobe.exe"


# ----------------------------------------------------------------- idents
IDENT_MAX_CHARS = 300
# One ident per line is what the prompt asks for. When the model ignores that and
# returns the whole batch as a single paragraph, splitlines() hands back one
# enormous "ident": 10 Sep 2026 shipped id_11.mp3, 153 seconds and 36 cues, twelve
# idents welded together, cycling "Good morning / Welcome back to Inch Radio /
# You're tuned into the New Lhasa station" over and over between songs.
_OPENER = r"(?:You\s?['’]?re listening to|You\s?['’]?re tuned into|Welcome back to|Good (?:morning|afternoon|evening))"
_SPLIT_AT_OPENER = re.compile(r"(?<=[.!?])\s+(?=" + _OPENER + ")")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def _split_idents(raw):
    """Lines -> idents, re-splitting any run-on batch and dropping what will not fit.

    Splits only at an opener that follows a sentence end, so "Good morning, you're
    tuned into Inch Radio." stays one ident rather than two fragments.
    """
    out = []
    for line in raw.splitlines():
        line = re.sub(r"^[\s\-•\d.)]+", "", line).strip()
        if not line:
            continue
        parts = _SPLIT_AT_OPENER.split(line) if len(line) > IDENT_MAX_CHARS else [line]
        for part in parts:
            part = part.strip()
            if not part:
                continue
            if len(part) > IDENT_MAX_CHARS:
                # Still oversized (no openers to cut on): keep the opening two
                # sentences, which is what an ident is supposed to be anyway.
                sents = _SENTENCE.split(part)
                part = " ".join(sents[:2]).strip()
                if len(part) > IDENT_MAX_CHARS:
                    print(f"  drop oversized ident ({len(part)} chars)", flush=True)
                    continue
            out.append(part)
    return out


# ----------------------------------------------------------------- groq
def call_groq(system, user, max_tokens=900, json_mode=True):
    from groq import Groq
    import groq_limits
    client = Groq(api_key=os.getenv("GROQ_API_KEY"))
    kwargs = dict(model=GROQ_MODEL,
                  messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                  temperature=0.85, max_tokens=max_tokens)
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    return groq_limits.chat(client, **kwargs).choices[0].message.content


# ----------------------------------------------------------------- time + weather
IST = timezone(timedelta(hours=5, minutes=30))
now = datetime.now(IST)
h = now.hour
tod = ("the dead of night" if h < 5 else "early morning" if h < 8 else "morning" if h < 12
       else "afternoon" if h < 17 else "evening" if h < 21 else "night")
if 5 <= h < 12:
    greeting = 'Greet with "Good morning".'
elif 12 <= h < 17:
    greeting = 'Greet with "Good afternoon".'
elif 17 <= h < 22:
    greeting = 'Greet with "Good evening".'
else:
    greeting = 'It is the middle of the night, after midnight. Greet like a late-night host, never good morning/afternoon/evening.'
m = now.month
season = ("deep winter" if m in (12, 1) else "the tail of winter" if m == 2
          else "spring heat building" if m in (3, 4) else "peak summer before the rains" if m in (5, 6)
          else "the monsoon" if m in (7, 8, 9) else "the cool after the rains")

WMO = {0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
       51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
       66: "freezing rain", 67: "freezing rain", 71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
       80: "rain showers", 81: "rain showers", 82: "violent rain showers", 85: "snow showers", 86: "heavy snow showers",
       95: "thunderstorm", 96: "thunderstorm with hail", 99: "severe thunderstorm"}


def falkland_weather():
    import httpx
    try:
        r = httpx.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": -51.7, "longitude": -57.85,
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m,wind_gusts_10m,weather_code,is_day",
            "wind_speed_unit": "kmh", "timezone": "auto"}, timeout=20)
        c = r.json().get("current", {})
        return {"conditions": WMO.get(c.get("weather_code"), "unsettled"), "temp_c": c.get("temperature_2m"),
                "feels_like_c": c.get("apparent_temperature"), "wind_kmh": c.get("wind_speed_10m"),
                "gusts_kmh": c.get("wind_gusts_10m"), "humidity_pct": c.get("relative_humidity_2m"),
                "daylight": "day" if c.get("is_day") else "dark"}
    except Exception as e:
        return {"error": str(e), "conditions": "cold and blowing", "temp_c": "?", "wind_kmh": "?"}


# ----------------------------------------------------------------- prompts
WORLD = f"""You write spoken radio for INCH RADIO, the New Lhasa station. The station broadcasts the life of an anonymous Indian imageboard, Indiachan /b/, as if it were the news of New Lhasa, a far-off cold island outpost.
The station is ALWAYS called Inch Radio. Never call the station Indiachan Radio. "Indiachan" is only the name of the board you cover, never the station.
New Lhasa is FELT, never announced. Never welcome anyone to an island or explain the setting. Let it leak only through cold, sea, distance, and the weather. At most ONE short line of that per segment. It is seasoning, never the subject.
You do not report the board flatly. The threads are what's coming in tonight; you translate them into the station's world, inventing the frame while keeping the real human substance the posters brought. Never use the word "wire".
Voice: spoken, reads aloud cleanly, always contract (it's, you're, don't, there's, that's, he's) and never the written-out forms, no headlines, no stage directions, no sound-effect notes. Write the way people talk on air, not the way they write: short clauses, breath, the occasional ellipsis or comma for a natural pause. The host is unnamed: "the desk", "your host". Never use em dashes. Do not moralise. The host is UNSHOCKABLE: nothing on the board fazes, disturbs, or surprises him, he has heard far worse a thousand times. He relays the ugliest, most violent, strangest things flatly and casually, like old news, never recoiling, never tut-tutting, no "can you believe it", no concern, no judgement, no "ouch" or "tough one". It is all perfectly ordinary to him. Report what was said as it was said; you can be blunt and uncensored. Every sentence complete.
NAMES: never speak a private person's real name, employer, salary, phone number, handle, or any other detail that would identify them off the board, even when a post states it plainly and even when you are told to be concrete. Say "this anon", "one of the voices", "a poster" instead, and describe what they said rather than who they are. Public figures, politicians, actors, cricketers, and the like are fine to name.
TIME: it is {tod} in India ({season}). {greeting} Say the time of day only as morning, afternoon, evening, or night. NEVER speak a clock time or a number for the hour.
OPENING: open with warm radio phrasing, never a blunt label. Ease in like a real host with connective lines such as "you're listening to Inch Radio", "you're tuned into the New Lhasa station", "welcome back to Inch Radio, here's the news", "alright, time for the weather". NEVER a flat title-drop like "Inch Radio news from the desk" or "a bulletin from the desk". Then deliver.
Stay on THIS segment's material only. Do not list the board's other topics.
LENGTH IS A HARD CEILING, not a target. Count as you go and stop at or under the number this segment gives you. Running over gets the end of the segment cut off mid-word on air. Land the ending early rather than late.
Return ONLY the spoken words the host says, as plain text. No JSON, no labels, no list, no quotation marks wrapped around the whole thing. Just what goes out on air."""

FORMAT = {
    "sign_on": "Format: SIGN-ON. The station coming on air at this hour. Identify the station. Three to four lines. Atmosphere and the hour only. Name no board topics. Casual and offhand, a host easing into the night, a little informal, natural pauses.",
    "news": "Format: NEWS. You are the news anchor, live on air. Open with the part of day and the proper greeting, then a line like 'here's what's going on'. This is RADIO news, NOT a written summary and NOT a list of items. Talk ABOUT the day as a host would: flowing on-air commentary that sweeps across the stories with reactions, asides, and transitions between them ('over in another corner of the board', 'meanwhile', 'elsewhere'). You can have a take and a tone. Move between the threads as one connected stretch of talk, not separate bullet points. Keep each thread's real subject recognizable; quote a poster as 'a caller' or 'one of the voices tonight' where it lands. Composed and clean, an anchor who knows the board, warm not stiff, keep filler light. This is the longest segment. 22 to 30 sentences.",
    "host_talk": (
        "Format: HOST TALK. The host has the board open in front of him and is giving you his read on this corner of it.\n\n"
        "OPEN by looking at the board and saying what he noticed, in his own words. Something in the shape of \"so, taking a look at the board\", "
        "\"right, so I've been scrolling through this lot\", \"alright, what have we got tonight\". Never a bulletin, never 'here's the news', never a flat title drop.\n\n"
        "Then WALK THE THREADS. For each one, NAME the actual subject plainly and QUOTE or closely paraphrase a real line from a real poster, "
        "attributed as \"this anon\", \"one anon said\", \"another post\", \"someone further down\". BE CONCRETE. If a poster is on about a caste fight, "
        "a slur, his sister, a job rejection, a specific game or show, SAY exactly that. BANNED phrasings: \"a particular topic\", \"certain activities\", "
        "\"a recent event\", \"strong opinions\", \"social issues\", \"their thoughts on something\", \"mixed reactions\", \"an interesting discussion\".\n\n"
        "The opinion comes from REACTING to the posts, not from the host's inner life. He is allowed to take a side, to find it stupid, to be amused, "
        "to say he doesn't get what the issue is. He is NOT allowed to talk about himself, his room, his tea, his loneliness, or his feelings. He has no "
        "biography. If he says \"I\", it must be \"I don't get it\", \"I've seen this one before\", \"I'd say\", never \"I remember\" or \"I feel\".\n\n"
        "NO ATMOSPHERE IN THIS SEGMENT. Do not mention the sea, the cold, the harbor, the rain, the wind, the island, or the weather. Not one line, not even "
        "at the end. This overrides the general instruction about letting the setting leak in.\n\n"
        "HE DOES NOT DIAGNOSE THE BOARD. He is a man reading posts out loud and reacting, not a critic explaining a place to you. BANNED sentence shapes: "
        "\"it's the board's ...\", \"it's just ...\", \"there's no actual ... happening\", \"everyone is just ...\", \"the collective ...\", \"it's all very ...\", "
        "and any sentence that sums up what the board IS or why people are the way they are. React to one specific post at a time. If you want to make a point, "
        "quote another post instead.\n\n"
        "Every claim he makes must be traceable to a post below. If he cannot source it, he does not say it.\n\n"
        "END on a specific post, or on him not getting it, or on the thing still going. Never on a summary of what it all means. Good endings: \"and I really "
        "don't get what the issue is here\", \"that's where it's sitting as of now\", \"nobody's answered him yet\". Bad endings: any sentence that explains the "
        "board to the listener.\n\n"
        "14 to 20 sentences."),
    "talk": "Format: TALK HOUR. A real two-person interview drawn from this ONE thread. Write it as a back-and-forth where EACH LINE begins with 'HOST:' or 'GUEST:' (these labels are markers only, NEVER spoken aloud). The host welcomes the person on and introduces them NATURALLY, by who they are or what they're into, and NEVER uses the word 'guest'. The other person's lines are invented but true to what the poster actually argued. Several exchanges deep, the host asks, follows up, reacts, pushes; loose and informal, the odd filler, small reactions, natural pauses. Return plain text, one labeled line per turn, nothing else. 22 to 32 turns.",
    "government": "Format: GOVERNMENT BULLETIN. Identify this as a bulletin from the New Lhasa state desk, then reframe this ONE thread's anxiety as calm official address, decree, or reassurance. The state always sounds composed. 9 to 13 sentences.",
    "weather": "Format: WEATHER. Identify the weather break, then report the REAL conditions below as New Lhasa's own weather: a cold frigid southern island. Genuine weather, vivid and short, not a metaphor for the board. Refer to time of day only, never a clock time. 5 to 8 sentences.",
}


# Ceilings sit under Groq's 1,000 output-tokens-per-minute cap (see groq_limits.OTPM);
# a request asking for more than that is refused outright, not throttled.
CAPS = {"news": 900, "talk": 900, "host_talk": 900, "government": 800, "weather": 600, "sign_on": 400}

# Host talk used to be handed ONE thread with an order to riff from the host's own
# head, which produced segments about his loneliness and the sea and nothing about
# the board. Now a first pass reads several threads and finds what actually relates,
# and the segment is written from that cluster with real posts to quote.
READ_SYS = """You are the host of a radio station that covers an anonymous Indian imageboard, Indiachan /b/. You have just opened the board and you are scanning what is on it right now, the way a person actually does: not thread by thread in order, but noticing that a few of them are circling the same nerve.

Group the threads below into readings. A reading is threads that genuinely share something: the same argument, the same grievance, the same obsession, the same kind of person posting, or two threads that flatly contradict each other. Contradiction counts, and is often the best reading.

HARD RULE: every reading must list EXACTLY 3 or 4 thread ids. A reading with 1 or 2 ids is invalid and gets thrown away. If the third thread is only a loose fit, include it anyway and let the looseness show.

For each reading give:
  "noticed": one plain spoken sentence, at most 20 words, said the way you would say it to someone sitting in the room. Name the actual subject. "The caste fight is back and this time it's the jatts" is good. "Users are discussing identity" is worthless. Do not write an essay sentence.
  "ids": exactly 3 or 4 thread ids.
  "reaction": one short spoken sentence of the host reacting out loud, the way a person does when something is stupid, funny, tedious, or confusing. Plain and a little dumb. "I really don't get what the issue is here" is good. "It reveals the underlying anxiety of the board" is banned. Do not explain anything. Just react.

Never use the words "theme", "discourse", "narrative", "community", "conversation", "dynamic", "phenomenon", or "vulnerability".

Return JSON: {"readings": [{"noticed": "...", "ids": [1,2,3], "reaction": "..."}]}."""

REPAIR_SYS = """You are grouping imageboard threads for a radio host. You will be given a reading (a few threads that go together) and a list of threads that have not been placed yet. Add the 1 or 2 unplaced threads that fit the reading best, so the reading ends up with 3 or 4 threads total. Pick the least bad fit if nothing fits well. Return JSON: {"add": [id, ...]} and nothing else."""


def gen(fmt_key, payload):
    cap = CAPS.get(fmt_key, 1000)
    raw = call_groq(WORLD + "\n\n" + FORMAT[fmt_key], payload, cap, json_mode=False).strip()
    sents = [p.strip() for p in re.split(r"(?<=[.!?])\s+", raw) if p.strip()]
    if sents and not sents[-1].rstrip().endswith((".", "!", "?", '."', '!"', '?"', ".”", "!”", "?”")):
        print(f"  [gen] {fmt_key}: dropped truncated tail", flush=True)
        sents.pop()
    return sents


def gen_turns(payload):
    raw = call_groq(WORLD + "\n\n" + FORMAT["talk"], payload, CAPS["talk"], json_mode=False).strip()
    turns = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        mt = re.match(r"^(HOST|GUEST)\s*[:\-]\s*(.*)$", line, re.I)
        if mt:
            turns.append({"speaker": mt.group(1).lower(), "text": mt.group(2).strip()})
        elif turns:
            turns[-1]["text"] += " " + line
    turns = [t for t in turns if t["text"]]
    if turns and not turns[-1]["text"].rstrip().endswith((".", "!", "?", '."', '!"', '?"', ".”", "!”", "?”")):
        print("  [gen_turns] dropped truncated final turn", flush=True)
        turns.pop()
    return turns


def thread_payload(t):
    return {"subject": t.get("subject", ""), "op": (t.get("body", "") or "")[:600],
            "replies": [(r.get("body", "") or "")[:200] for r in (t.get("replies", []) or [])[:12]]}


def brief(t, n_replies=5, op_chars=280, rep_chars=150):
    """A cheap thread sketch, for the passes that scan many threads at once."""
    return {"id": t.get("no"), "subject": (t.get("subject") or "").strip(),
            "op": (t.get("body") or "")[:op_chars],
            "replies": [(r.get("body") or "")[:rep_chars] for r in (t.get("replies") or [])[:n_replies]]}


def read_board(pool, n_readings):
    """Scan a pool of threads and return readings: what the host noticed, and which
    threads it spans. The model reliably under-fills a reading, so short ones get
    topped up from the threads it left unplaced (its pick, not ours, where it will make one)."""
    if len(pool) < 3:
        return []
    by_id = {t.get("no"): t for t in pool}
    try:
        raw = call_groq(READ_SYS, "The board right now:\n" + json.dumps([brief(t) for t in pool], ensure_ascii=False)
                        + "\n\nGive %d readings." % n_readings, 900, json_mode=True)
        readings = json.loads(raw).get("readings", [])
    except Exception as e:
        print("  read_board failed:", e, flush=True)
        return []
    time.sleep(SLEEP)
    readings = [r for r in readings if [x for x in (r.get("ids") or []) if x in by_id]][:n_readings]
    placed = {x for r in readings for x in (r.get("ids") or [])}
    for r in readings:
        ids = [x for x in (r.get("ids") or []) if x in by_id]
        if len(ids) >= 3:
            r["ids"] = ids[:4]
            continue
        spare = [t for t in pool if t.get("no") not in placed]
        want = 3 - len(ids)
        add = []
        if spare:
            ask = ("The reading: " + str(r.get("noticed")) +
                   "\nThreads already in it:\n" + json.dumps([brief(by_id[x]) for x in ids], ensure_ascii=False) +
                   "\n\nUnplaced threads:\n" + json.dumps([brief(t) for t in spare], ensure_ascii=False) +
                   "\n\nAdd exactly %d." % want)
            try:
                add = json.loads(call_groq(REPAIR_SYS, ask, 200, json_mode=True)).get("add", [])
                time.sleep(SLEEP)
            except Exception as e:
                print("  repair failed:", e, flush=True)
            add = [x for x in add if x in by_id and x not in placed][:want]
            if len(add) < want:      # declined, or hallucinated ids: fall back to the biggest spares
                add += [t["no"] for t in spare if t["no"] not in add and t["no"] not in placed][:want - len(add)]
        r["ids"] = ids + add
        placed.update(r["ids"])
    return [r for r in readings if len(r.get("ids") or []) >= 2]


# ----------------------------------------------------------------- generation
RECENT_FILE = "recent_threads.json"
IDENTS_FILE = "recent_idents.json"
MIN_IDENTS = 8          # below this the block loses most of its songs too


def load_cached_idents():
    """Last block's idents, for when Groq will not give us new ones."""
    try:
        url = f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{IDENTS_FILE}"
        with urllib.request.urlopen(url, timeout=15) as r:
            v = json.loads(r.read().decode("utf-8"))
            return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []
    except Exception:
        return []


def save_cached_idents(idents):
    try:
        sb("POST", f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{IDENTS_FILE}",
           data=json.dumps(idents[:24], ensure_ascii=False).encode("utf-8"),
           ctype="application/json", upsert=True)
    except Exception as e:
        print("save_cached_idents:", e)



def load_recent():
    """Thread ids aired in the last few blocks (newest first), from the bucket."""
    try:
        url = f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{RECENT_FILE}"
        with urllib.request.urlopen(url, timeout=15) as r:
            v = json.loads(r.read().decode("utf-8"))
            return v if isinstance(v, list) else []
    except Exception:
        return []


def save_recent(used_ids, prev):
    """Remember the threads this block used so the next blocks can skip them."""
    try:
        merged = list(used_ids) + [x for x in prev if x not in used_ids]
        sb("POST", f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{RECENT_FILE}",
           data=json.dumps(merged[:28]).encode("utf-8"), ctype="application/json", upsert=True)
    except Exception as e:
        print("save_recent:", e)


def generate():
    wx = falkland_weather()
    print("weather:", json.dumps(wx))
    cat = simplechan.fetch_catalog(BOARD)
    pool = [t for t in cat if not t.get("pinned")]
    pool.sort(key=lambda t: t.get("reply_count", 0), reverse=True)
    recent_list = load_recent()
    recent = set(recent_list)                            # threads aired in the last couple of blocks
    cands = pool[:40]                                    # wider candidate pool than the strict top 14
    fresh = [t for t in cands if t.get("no") not in recent]
    chosen = fresh + [t for t in cands if t.get("no") in recent]   # prefer threads we haven't aired lately; fall back to recent only if the board's thin
    top = chosen[:14]
    random.shuffle(top)                                  # vary which thread becomes which segment, block to block
    threads = []
    for t in top:
        try:
            full = simplechan.fetch_thread(BOARD, t["no"])
            full["_n"] = t.get("reply_count", 0)
            threads.append(full)
        except Exception as e:
            print("  skip", t.get("no"), e)
        time.sleep(1)

    briefs = [{"id": t.get("no"), "subject": t.get("subject", ""), "op": (t.get("body", "") or "")[:300],
               "reply_count": t.get("_n", 0),
               "sample": [(r.get("body", "") or "")[:120] for r in (t.get("replies", []) or [])[:4]]} for t in threads]
    triage_sys = ('You are the running order desk of a radio station that turns imageboard threads into segments. '
                  'Pick each thread\'s best format: "news", "talk", "government", or "skip". '
                  'Return JSON: {"assignments": [{"id": <id>, "format": "news|talk|government|skip"}]}.')
    try:
        assigns = json.loads(call_groq(triage_sys, json.dumps(briefs, ensure_ascii=False), 400)).get("assignments", [])
    except Exception as e:
        assigns = []
        print("  triage failed", e)
    fmt_of = {a["id"]: a["format"] for a in assigns if "id" in a}
    time.sleep(SLEEP)

    used = set()

    def take(n, prefer=None):
        picked = []
        for want_match in (True, False):
            for t in threads:
                if len(picked) >= n:
                    break
                tid = t.get("no")
                if tid in used:
                    continue
                if want_match and prefer and fmt_of.get(tid) != prefer:
                    continue
                picked.append(t)
                used.add(tid)
        return picked

    news1 = take(3, "news")
    talk_sel, gov_sel = take(1, "talk"), take(1, "government")
    news2 = take(2, "news")
    # Whatever news/talk/government didn't claim goes to host talk, which reads it as
    # a group rather than one thread at a time. Two segments of 3-4 threads, not four of one.
    host_pool = [t for t in threads if t.get("no") not in used]
    by_id = {t.get("no"): t for t in threads}
    print("read_board over %d spare threads" % len(host_pool), flush=True)
    try:
        readings = read_board(host_pool, 2)
    except Exception as e:
        print(f"  SKIP read_board: {type(e).__name__}: {e}", flush=True)
        readings = []
    for r in readings:
        print("  noticed:", r.get("noticed"), r.get("ids"), flush=True)
        used.update(r.get("ids") or [])

    def ids(ts):
        return ", ".join("#" + str(t.get("no")) for t in ts)

    def ids2(r):
        return ", ".join("#" + str(x) for x in r.get("ids") or [])

    def pl(ts):
        return json.dumps([thread_payload(t) for t in ts], ensure_ascii=False)

    def hpl(r):
        """Host talk payload: what the host noticed, how he feels, and the real posts to quote."""
        mat = [brief(by_id[x], n_replies=10, op_chars=600, rep_chars=220) for x in r["ids"] if x in by_id]
        return ("What you noticed when you opened the board: " + str(r.get("noticed")) +
                "\nHow you feel about it, in your own words: " + str(r.get("reaction")) +
                "\n\nThe threads, with real posts to quote:\n" + json.dumps(mat, ensure_ascii=False))

    segs = []

    def add(label, fmt_key, payload):
        # A segment that cannot be generated is skipped, never fatal. A block that
        # is short a weather break still goes on air; a block that raises leaves
        # the previous one playing for hours (see 10 Sep 2026).
        print("gen", label)
        try:
            segs.append({"label": label, "sentences": gen(fmt_key, payload)})
        except Exception as e:
            print(f"  SKIP {label}: {type(e).__name__}: {e}", flush=True)
            return
        time.sleep(SLEEP)

    add("SIGN-ON", "sign_on", "Coming on air.")
    if news1:
        add(f"NEWS 1 ({ids(news1)})", "news", pl(news1))
    if len(readings) > 0:
        add(f"HOST TALK 1 ({ids2(readings[0])})", "host_talk", hpl(readings[0]))
    if talk_sel:
        print("gen TALK HOUR (two voices)")
        try:
            tt = gen_turns(pl(talk_sel))
            segs.append({"label": f"TALK HOUR ({ids(talk_sel)})", "turns": tt, "sentences": [t["text"] for t in tt]})
            time.sleep(SLEEP)
        except Exception as e:
            print(f"  SKIP TALK HOUR: {type(e).__name__}: {e}", flush=True)
    add("WEATHER 1", "weather", json.dumps(wx, ensure_ascii=False))
    if news2:
        add(f"NEWS 2 ({ids(news2)})", "news", pl(news2))
    if len(readings) > 1:
        add(f"HOST TALK 2 ({ids2(readings[1])})", "host_talk", hpl(readings[1]))
    if gov_sel:
        add(f"GOVERNMENT ({ids(gov_sel)})", "government", pl(gov_sel))
    add("WEATHER 2", "weather", json.dumps(wx, ensure_ascii=False))   # a second weather break, placed elsewhere in the block

    print("gen IDENTS (batch)")
    ident_sys = WORLD + ('\n\nFormat: IDENTS. Short station idents for between songs. Each ident is TWO to THREE sentences: it places '
                         'the listener with connective phrasing ("you\'re listening to Inch Radio", "you\'re tuned into the New Lhasa '
                         'station", "welcome back to Inch Radio"), then folds in a dry, unhurried glance at ONE specific thing on the '
                         "board right now. BE CONCRETE: name the actual subject and quote or closely paraphrase a real line from the "
                         'thread. NEVER vague-out. BANNED phrasings: "a particular topic", "a particular individual", "certain '
                         'activities", "a recent event", "a situation that didn\'t quite work out", "strong opinions", "social issues", '
                         '"their preferences", "their thoughts on something", "mixed reactions". If a poster is on about gay sex, a caste '
                         "fight, a slur, a breakup, a specific game or show, SAY exactly that, plainly. The host is unshockable and never "
                         "euphemises or softens, he relays the real subject flatly. Each ident is about a DIFFERENT thread than the one "
                         "before it. Vary the opener every time. Write each ident as ONE single line, idents separated by a newline. Plain text, nothing else.")
    # Two calls of twelve rather than one of twenty-four: 24 idents is ~950 output
    # tokens, over the per-request output ceiling, and a clamped single call just
    # returns half of them silently.
    idents = []
    for half in (threads[:len(threads) // 2], threads[len(threads) // 2:]):
        if not half:
            continue
        act = json.dumps([{"subject": (t.get("subject") or "").strip(),
                           "op": (t.get("body", "") or "")[:260],
                           "replies": [(r.get("body", "") or "")[:160] for r in (t.get("replies", []) or [])[:5]]}
                          for t in half], ensure_ascii=False)
        try:
            raw = call_groq(ident_sys, "The live threads right now (cover as many different ones as you can, naming each real subject):\n" + act + "\n\nWrite about 12 idents, each grounded in a specific thread above. Do not invent topics that aren't there.", 900, json_mode=False)
            idents += _split_idents(raw)
        except Exception as e:
            print("  idents failed", e)
        time.sleep(SLEEP)
    # Idents are the cheapest thing in the block and the most load-bearing: each one
    # drags a song in behind it, so losing them collapses the block length (10 Sep
    # 2026: 13 idents lost = 13 songs lost = 135 min down to 38). Reuse the last
    # good set rather than going out thin. They are two-sentence station tags, so
    # they age far better than the thread segments do.
    if len(idents) >= MIN_IDENTS:
        save_cached_idents(idents)
    else:
        cached = [c for c in load_cached_idents() if c not in idents]
        if cached:
            print(f"  only {len(idents)} fresh idents, topping up with {len(cached)} cached", flush=True)
            idents += cached
        else:
            print(f"  only {len(idents)} idents and no cache to fall back on", flush=True)
    random.shuffle(idents)
    save_recent(list(used), recent_list)                # so the next blocks rotate to different threads
    return segs, idents, wx


# ----------------------------------------------------------------- render
def parse_vtt(p):
    def t2s(ts):
        ts = ts.strip().replace(".", ",")
        hh, mm, rest = ts.split(":")
        ss, ms = rest.split(",")
        return int(hh) * 3600 + int(mm) * 60 + int(ss) + int(ms) / 1000.0
    cues = []
    for blk in p.read_text(encoding="utf-8").replace("\r", "").strip().split("\n\n"):
        lines = [l for l in blk.split("\n") if l.strip()]
        tl = next((l for l in lines if "-->" in l), None)
        if not tl:
            continue
        a, b = tl.split("-->")
        txt = " ".join(l for l in lines if "-->" not in l and not l.strip().isdigit()).strip()
        if txt:
            cues.append([round(t2s(a), 3), round(t2s(b), 3), txt])
    return cues


def render(text, stem, voice, rate, pitch):
    audio = text
    for a, b in PRONOUNCE:
        audio = audio.replace(a, b)
    if not audio.strip():
        raise ValueError("empty text")
    body, mp3, vtt = WORK / f"{stem}.body.txt", WORK / f"{stem}.mp3", WORK / f"{stem}.vtt"
    body.write_text(audio, encoding="utf-8")
    # edge-tts intermittently returns "no audio received" (exit 1). Retry a few
    # times and verify a non-empty mp3 landed, so one flaky call can't kill a block.
    for attempt in range(3):
        try:
            subprocess.run([sys.executable, "-m", "edge_tts", "--voice", voice, "--rate=" + rate, "--pitch=" + pitch,
                            "--file", str(body), "--write-media", str(mp3), "--write-subtitles", str(vtt)],
                           check=True, capture_output=True)
            if mp3.exists() and mp3.stat().st_size > 0:
                break
        except subprocess.CalledProcessError:
            pass
        time.sleep(2 + attempt * 2)
    else:
        raise RuntimeError(f"edge_tts failed for {stem} after retries")
    cues = parse_vtt(vtt)
    for c in cues:
        for a, b in PRONOUNCE:
            c[2] = c[2].replace(b, a)
    return cues


def dur_of(p):
    out = subprocess.run([FFPROBE, "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
                         capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except Exception:
        return 0.0


def concat(parts, out):
    lst = WORK / "_concat.txt"
    lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
    subprocess.run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                    "-c:a", "libmp3lame", "-q:a", "4", str(out)], check=True, capture_output=True)


def render_all(segments, idents):
    seg_items = []
    for i, seg in enumerate(segments):
        if seg.get("turns"):
            cum, comb, parts = 0.0, [], []
            for k, turn in enumerate(seg["turns"]):
                ttext = (turn.get("text") or "").strip()
                if not ttext:
                    continue
                v = GUEST_VOICE if turn.get("speaker") == "guest" else HOST_VOICE
                try:
                    cues = render(ttext, f"seg_{i}_t{k}", *v)
                except Exception as e:
                    print(f"  skip turn {i}.{k}: {e}")
                    continue
                mp3 = WORK / f"seg_{i}_t{k}.mp3"
                for c in cues:
                    comb.append([round(c[0] + cum, 3), round(c[1] + cum, 3), c[2]])
                cum += dur_of(mp3)
                parts.append(mp3)
            if not parts:
                print(f"  skip talk seg_{i}: no usable turns")
                continue
            concat(parts, WORK / f"seg_{i}.mp3")
            print(f"  talk seg_{i}.mp3 ({len(parts)} turns, {cum:.0f}s)")
            seg_items.append({"type": "segment", "kind": "talk", "label": seg.get("label", ""),
                              "audio": f"seg_{i}.mp3", "cues": comb, "duration": round(cum, 3)})
            continue
        sents = [s.strip() for s in seg.get("sentences", []) if s.strip()]
        if not sents:
            continue
        try:
            cues = render(" ".join(sents), f"seg_{i}", *HOST_VOICE)
        except Exception as e:
            print(f"  skip seg {i} ({seg.get('label','')}): {e}")
            continue
        print(f"  seg_{i}.mp3 ({seg.get('label','')})")
        seg_items.append({"type": "segment", "kind": "segment", "label": seg.get("label", ""),
                          "audio": f"seg_{i}.mp3", "cues": cues, "duration": cues[-1][1] if cues else 0})
    ident_items = []
    for j, idt in enumerate(idents):
        txt = (idt or "").strip()
        if not txt:
            continue
        try:
            cues = render(txt, f"id_{j}", *HOST_VOICE)
        except Exception as e:
            print(f"  skip ident {j}: {e}")
            continue
        ident_items.append({"type": "segment", "kind": "ident", "label": "IDENT",
                            "audio": f"id_{j}.mp3", "cues": cues, "duration": cues[-1][1] if cues else 0})
    return seg_items, ident_items


# ----------------------------------------------------------------- running order
def iso_dur(s):
    mt = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not mt:
        return 0
    return int(mt.group(1) or 0) * 3600 + int(mt.group(2) or 0) * 60 + int(mt.group(3) or 0)


def load_playlists():
    """Playlists of the active music bucket from config.json, else the default set."""
    try:
        url = f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/config.json"
        with urllib.request.urlopen(url, timeout=20) as r:
            cfg = json.loads(r.read().decode("utf-8"))
        active = cfg.get("activeBucket")
        for b in (cfg.get("musicBuckets") or []):
            if b.get("id") == active:
                pls = [p for p in (b.get("playlists") or []) if p]
                if pls:
                    print(f"music bucket: {b.get('name', '?')} ({len(pls)} playlists)")
                    return pls
    except Exception as e:
        print("playlist config fallback:", e)
    return PLAYLISTS


def fetch_song_pool(playlists=None):
    import httpx
    playlists = playlists or PLAYLISTS
    key = os.getenv("YOUTUBE_API_KEY")
    if not key:
        print("no YOUTUBE_API_KEY; music not globally scheduled (client shuffle fallback)")
        return []
    pool = []
    for pid in playlists:
        ids, token = [], ""
        try:
            for _ in range(2):
                r = httpx.get("https://www.googleapis.com/youtube/v3/playlistItems",
                              params={"part": "contentDetails", "playlistId": pid, "maxResults": 50, "key": key, "pageToken": token}, timeout=30)
                d = r.json()
                for it in d.get("items", []):
                    ids.append(it["contentDetails"]["videoId"])
                token = d.get("nextPageToken", "")
                if not token:
                    break
            for i in range(0, len(ids), 50):
                r = httpx.get("https://www.googleapis.com/youtube/v3/videos",
                              params={"part": "contentDetails,status", "id": ",".join(ids[i:i+50]), "key": key}, timeout=30)
                for it in r.json().get("items", []):
                    st = it.get("status", {})
                    if not st.get("embeddable") or st.get("privacyStatus") != "public":
                        continue   # only songs that can actually play in an embed
                    du = iso_dur(it.get("contentDetails", {}).get("duration"))
                    if 30 < du < 900:
                        pool.append({"videoId": it["id"], "duration": du})
        except Exception as e:
            print("yt pool fetch failed for", pid, e)
    random.shuffle(pool)
    return pool


# The cron puts a block on air every 3 hours, so a block shorter than that leaves
# the station to run dry or loop. Music, not speech, is what fills it.
TARGET_BLOCK_SEC = float(os.getenv("TARGET_BLOCK_SEC", "10800"))
MAX_SONGS_PER_GAP = int(os.getenv("MAX_SONGS_PER_GAP", "5"))


def build_order(seg_items, ident_items, song_pool, playlists=None):
    def has(s, kw):
        return kw in s["label"].upper()
    signon = [s for s in seg_items if has(s, "SIGN-ON")]
    news = [s for s in seg_items if has(s, "NEWS")]
    hosts = [s for s in seg_items if has(s, "HOST")]
    talk = [s for s in seg_items if has(s, "TALK HOUR")]
    weather = [s for s in seg_items if has(s, "WEATHER")]
    govt = [s for s in seg_items if has(s, "GOVERNMENT")]
    order, idents = [], list(ident_items)

    fallback_pls = playlists or PLAYLISTS      # honour the configured bucket, not just the defaults
    plan, gap_i = [], [0]                      # songs per gap, filled in once speech is counted

    def music():
        n = plan[gap_i[0]] if gap_i[0] < len(plan) else 1
        gap_i[0] += 1
        for _ in range(n):
            if song_pool:
                s = song_pool.pop(0)
                order.append({"type": "song", "videoId": s["videoId"], "duration": s["duration"]})
            else:
                order.append({"type": "music", "playlist": random.choice(fallback_pls), "songs": 1})

    # Uniform chattiness: do NOT front-load the big talk. Spread every big segment
    # evenly across the whole block, woven into the ident stream, then put one song
    # after each talk item. So the first hour and the last hour are equally chatty
    # and equally colourful end to end.
    def spread(groups):   # interleave the segment TYPES so the same kind never clusters (host, news, weather, talk, ...)
        tagged = []
        for g in groups:
            n = len(g) or 1
            for i, it in enumerate(g):
                tagged.append(((i + 0.5) / n, it))   # even fractional position within each type
        tagged.sort(key=lambda t: t[0])
        return [it for _, it in tagged]
    bigs = spread([news, hosts, talk, weather, govt])    # varied lineup; sign-on opens separately
    nb, ni = len(bigs), len(idents)
    talk_stream, bi = [], 0
    for k, idt in enumerate(idents):
        target = round((k / ni) * nb) if ni else nb       # how many bigs should be placed by now to stay even
        while bi < target and bi < nb:
            talk_stream.append(bigs[bi]); bi += 1
        talk_stream.append(idt)
    while bi < nb:                                         # leftover bigs (few-idents case)
        talk_stream.append(bigs[bi]); bi += 1

    # How many songs go in each gap is a function of how much speech we ended up
    # with. A full block needs one per gap; a block that lost half its segments to
    # rate limits needs several, or it comes out a third of the length it should be.
    spoken = sum((it.get("duration") or 0) for it in ([signon[0]] if signon else []) + talk_stream)
    gaps = len(talk_stream) + (1 if signon else 0)
    avg_song = (sum(x["duration"] for x in song_pool) / len(song_pool)) if song_pool else 240.0
    if gaps:
        need = max(0.0, TARGET_BLOCK_SEC - spoken)
        # Spread the shortfall across the gaps rather than rounding each one up,
        # which overshot the target by half an hour on a healthy block.
        total_songs = int(round(need / avg_song))
        total_songs = max(gaps, min(total_songs, gaps * MAX_SONGS_PER_GAP))
        base, extra = divmod(total_songs, gaps)
        plan[:] = [base + (1 if i < extra else 0) for i in range(gaps)]
    print(f"  block plan: {spoken / 60:.0f} min spoken over {gaps} gaps, "
          f"{sum(plan)} songs, target {TARGET_BLOCK_SEC / 60:.0f} min", flush=True)

    if signon:
        order.append(signon[0]); music()                  # open the hour, then a song
    for t in talk_stream:
        order.append(t); music()                          # talk, song, talk, song ... uniformly, all block long
    return {"station": "Inch Radio", "items": order}


# ----------------------------------------------------------------- publish
def sb(method, url, data=None, ctype=None, upsert=False, timeout=600):
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Authorization", "Bearer " + SERVICE_KEY)
    r.add_header("apikey", SERVICE_KEY)
    if ctype:
        r.add_header("Content-Type", ctype)
    if upsert:
        r.add_header("x-upsert", "true")
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def publish(manifest):
    if not SERVICE_KEY:
        raise SystemExit("SUPABASE_SERVICE_ROLE_KEY not set")
    st, _ = sb("POST", f"{SUPABASE_URL}/storage/v1/bucket",
               data=json.dumps({"id": BUCKET, "name": BUCKET, "public": True}).encode(),
               ctype="application/json", timeout=60)
    print("bucket:", st)
    (WORK / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    audios = sorted({it["audio"] for it in manifest["items"] if it.get("type") == "segment" and it.get("audio")})

    def up(path, dest, ctype):
        st, body = sb("POST", f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{dest}",
                      data=path.read_bytes(), ctype=ctype, upsert=True)
        return st if st < 300 else f"FAIL {st} {body[:120]}"
    print("manifest.json:", up(WORK / "manifest.json", "manifest.json", "application/json"))
    for a in audios:
        print(f"  {a}:", up(WORK / a, a, "audio/mpeg"))
    bg = WORK / "bg.mp3"
    if bg.exists():
        print("  bg.mp3:", up(bg, "bg.mp3", "audio/mpeg"))
    print("public base:", f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/")


MIN_SEGMENTS = 3   # below this the block is not worth putting over a working one
RENDER_RESERVE_SEC = float(os.getenv("RENDER_RESERVE_SEC", "600"))   # TTS + ffmpeg + upload
MIN_GEN_SEC = float(os.getenv("MIN_GEN_SEC", "240"))                 # not worth starting below this


def main():
    print(f"=== INCH RADIO block · {tod} · {season} ===")
    # Generation gets a hard budget; render, stitch and upload get the rest of the
    # job. Anything that would sleep past it raises instead, and the segment is
    # skipped. The whole point is that a thin block beats yesterday's block.
    # RUN_DEADLINE is stamped by the workflow before anything installs, so a slow
    # apt mirror eats into the budget instead of being invisible to it. Render,
    # stitch and upload need the reserve after generation stops.
    deadline = time.time() + float(os.getenv("GEN_BUDGET_SEC", "900"))
    if os.getenv("RUN_DEADLINE"):
        try:
            deadline = min(deadline, float(os.environ["RUN_DEADLINE"]) - RENDER_RESERVE_SEC)
        except ValueError:
            pass
    left = deadline - time.time()
    if left < MIN_GEN_SEC:
        print(f"ABORT: {left / 60:.1f} min left for generation, need {MIN_GEN_SEC / 60:.0f}. "
              f"Setup ate the job. Leaving the previous block on air.", flush=True)
        sys.exit(1)
    groq_limits.set_deadline(deadline)
    print(f"generation budget: {left / 60:.1f} min", flush=True)
    segs, idents, wx = generate()
    groq_limits.set_deadline(None)          # rendering and upload are not rate limited
    seg_items, ident_items = render_all(segs, idents)
    playlists = load_playlists()
    song_pool = fetch_song_pool(playlists)
    print(f"song pool: {len(song_pool)} tracks with durations")
    manifest = build_order(seg_items, ident_items, song_pool, playlists)
    print(f"manifest: {len(manifest['items'])} items, {len(seg_items)} segments, {len(ident_items)} idents")
    if len(seg_items) < MIN_SEGMENTS:
        print(f"ABORT: only {len(seg_items)} segments rendered (need {MIN_SEGMENTS}). "
              f"Leaving the previous block on air rather than publishing dead air.", flush=True)
        sys.exit(1)
    publish(manifest)
    print("DONE")


if __name__ == "__main__":
    main()
