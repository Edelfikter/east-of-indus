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

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

WORK = ROOT / "work"
WORK.mkdir(exist_ok=True)

GROQ_MODEL = os.getenv("GROQ_MODEL") or "llama-3.3-70b-versatile"
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
New Lhasa is FELT, never announced. Never welcome anyone to an island or explain the setting. Let it leak only through cold, sea, distance, and the weather.
You do not report the board flatly. The threads are what's coming in tonight; you translate them into the station's world, inventing the frame while keeping the real human substance the posters brought. Never use the word "wire".
Voice: spoken, reads aloud cleanly, contractions fine, no headlines, no stage directions, no sound-effect notes. Write the way people talk on air, not the way they write: short clauses, breath, the occasional ellipsis or comma for a natural pause. The host is unnamed: "the desk", "your host". Never use em dashes. Do not moralise. The host is UNSHOCKABLE: nothing on the board fazes, disturbs, or surprises him, he has heard far worse a thousand times. He relays the ugliest, most violent, strangest things flatly and casually, like old news, never recoiling, never tut-tutting, no "can you believe it", no concern, no judgement, no "ouch" or "tough one". It is all perfectly ordinary to him. Report what was said as it was said; you can be blunt and uncensored. Every sentence complete.
TIME: it is {tod} in India ({season}). {greeting} Say the time of day only as morning, afternoon, evening, or night. NEVER speak a clock time or a number for the hour.
OPENING: open with warm radio phrasing, never a blunt label. Ease in like a real host with connective lines such as "you're listening to Inch Radio", "you're tuned into the New Lhasa station", "welcome back to Inch Radio, here's the news", "alright, time for the weather". NEVER a flat title-drop like "Inch Radio news from the desk" or "a bulletin from the desk". Then deliver.
Stay on THIS segment's material only. Do not list the board's other topics.
Return ONLY the spoken words the host says, as plain text. No JSON, no labels, no list, no quotation marks wrapped around the whole thing. Just what goes out on air."""

FORMAT = {
    "sign_on": "Format: SIGN-ON. The station coming on air at this hour. Identify the station. Three to four lines. Atmosphere and the hour only. Name no board topics. Casual and offhand, a host easing into the night, a little informal, natural pauses.",
    "news": "Format: NEWS. You are the news anchor, live on air. Open with the part of day and the proper greeting, then a line like 'here's what's going on'. This is RADIO news, NOT a written summary and NOT a list of items. Talk ABOUT the day as a host would: flowing on-air commentary that sweeps across the stories with reactions, asides, and transitions between them ('over in another corner of the board', 'meanwhile', 'elsewhere'). You can have a take and a tone. Move between the threads as one connected stretch of talk, not separate bullet points. Keep each thread's real subject recognizable; quote a poster as 'a caller' or 'one of the voices tonight' where it lands. Composed and clean, an anchor who knows the board, warm not stiff, keep filler light. This is the longest segment. 22 to 30 sentences.",
    "host_talk": "Format: HOST TALK. The host ALONE, talking at the listener, giving his OWN opinions and tangents sparked by the topic below. This is NOT news and NOT a summary. Do NOT narrate the thread. NEVER say 'there's a thread', 'someone said', 'a poster', 'replies are coming in', 'one anon', 'over on the board'. The listener does not need to know what was posted. Instead take the TOPIC and run with it in first person: what HE thinks, his gripes, his theories, a small rant or a musing, tangents off the side of it. He can be biased, wrong, contrarian, can ramble. The board is the spark, not the subject. Loose and conversational, the odd filler (well, look, honestly, I mean), natural pauses, but write it with PROPER punctuation and fully written contractions (it's, you're, don't, them), complete sentences with full stops and commas, NEVER a run-on with missing apostrophes. Open casually as the host, do NOT announce a bulletin or 'the news'. 18 to 26 sentences.",
    "talk": "Format: TALK HOUR. A real two-person interview drawn from this ONE thread. Write it as a back-and-forth where EACH LINE begins with 'HOST:' or 'GUEST:' (these labels are markers only, NEVER spoken aloud). The host welcomes the person on and introduces them NATURALLY, by who they are or what they're into, and NEVER uses the word 'guest'. The other person's lines are invented but true to what the poster actually argued. Several exchanges deep, the host asks, follows up, reacts, pushes; loose and informal, the odd filler, small reactions, natural pauses. Return plain text, one labeled line per turn, nothing else. 22 to 32 turns.",
    "government": "Format: GOVERNMENT BULLETIN. Identify this as a bulletin from the New Lhasa state desk, then reframe this ONE thread's anxiety as calm official address, decree, or reassurance. The state always sounds composed. 9 to 13 sentences.",
    "weather": "Format: WEATHER. Identify the weather break, then report the REAL conditions below as New Lhasa's own weather: a cold frigid southern island. Genuine weather, vivid and short, not a metaphor for the board. Refer to time of day only, never a clock time. 5 to 8 sentences.",
}


CAPS = {"news": 1900, "talk": 1900, "host_talk": 1200, "government": 1100, "weather": 700, "sign_on": 400}


def gen(fmt_key, payload):
    cap = CAPS.get(fmt_key, 1000)
    raw = call_groq(WORLD + "\n\n" + FORMAT[fmt_key], payload, cap, json_mode=False).strip()
    return [p.strip() for p in re.split(r"(?<=[.!?])\s+", raw) if p.strip()]


def gen_turns(payload):
    raw = call_groq(WORLD + "\n\n" + FORMAT["talk"], payload, 2400, json_mode=False).strip()
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
    return [t for t in turns if t["text"]]


def thread_payload(t):
    return {"subject": t.get("subject", ""), "op": (t.get("body", "") or "")[:600],
            "replies": [(r.get("body", "") or "")[:200] for r in (t.get("replies", []) or [])[:12]]}


# ----------------------------------------------------------------- generation
RECENT_FILE = "recent_threads.json"


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

    news1, host1 = take(3, "news"), take(1)
    talk_sel, gov_sel = take(1, "talk"), take(1, "government")
    news2, host2 = take(2, "news"), take(1)
    host3, host4 = take(1), take(1)   # a couple more host riffs; types get interleaved in build_order

    def ids(ts):
        return ", ".join("#" + str(t.get("no")) for t in ts)

    def pl(ts):
        return json.dumps([thread_payload(t) for t in ts], ensure_ascii=False)

    def hpl(ts):
        return json.dumps([{"topic": (t.get("subject") or (t.get("body", "") or "")[:120]),
                            "gist": (t.get("body", "") or "")[:300]} for t in ts], ensure_ascii=False)

    segs = []

    def add(label, fmt_key, payload):
        print("gen", label)
        segs.append({"label": label, "sentences": gen(fmt_key, payload)})
        time.sleep(SLEEP)

    add("SIGN-ON", "sign_on", "Coming on air.")
    if news1:
        add(f"NEWS 1 ({ids(news1)})", "news", pl(news1))
    if host1:
        add(f"HOST TALK 1 ({ids(host1)})", "host_talk", hpl(host1))
    if talk_sel:
        print("gen TALK HOUR (two voices)")
        tt = gen_turns(pl(talk_sel))
        segs.append({"label": f"TALK HOUR ({ids(talk_sel)})", "turns": tt, "sentences": [t["text"] for t in tt]})
        time.sleep(SLEEP)
    add("WEATHER 1", "weather", json.dumps(wx, ensure_ascii=False))
    if news2:
        add(f"NEWS 2 ({ids(news2)})", "news", pl(news2))
    if host2:
        add(f"HOST TALK 2 ({ids(host2)})", "host_talk", hpl(host2))
    if gov_sel:
        add(f"GOVERNMENT ({ids(gov_sel)})", "government", pl(gov_sel))
    if host3:
        add(f"HOST TALK 3 ({ids(host3)})", "host_talk", hpl(host3))
    if host4:
        add(f"HOST TALK 4 ({ids(host4)})", "host_talk", hpl(host4))
    add("WEATHER 2", "weather", json.dumps(wx, ensure_ascii=False))   # a second weather break, placed elsewhere in the block

    print("gen IDENTS (batch)")
    activity = json.dumps([{"subject": (t.get("subject") or "").strip(),
                            "op": (t.get("body", "") or "")[:260],
                            "replies": [(r.get("body", "") or "")[:160] for r in (t.get("replies", []) or [])[:5]]}
                           for t in threads], ensure_ascii=False)
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
    try:
        raw = call_groq(ident_sys, "The live threads right now (cover as many different ones as you can, naming each real subject):\n" + activity + "\n\nWrite about 24 idents, each grounded in a specific thread above. Do not invent topics that aren't there.", 2800, json_mode=False)
        idents = [re.sub(r"^[\s\-•\d.)]+", "", l).strip() for l in raw.splitlines() if l.strip()]
    except Exception as e:
        idents = []
        print("  idents failed", e)
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


def build_order(seg_items, ident_items, song_pool):
    def has(s, kw):
        return kw in s["label"].upper()
    signon = [s for s in seg_items if has(s, "SIGN-ON")]
    news = [s for s in seg_items if has(s, "NEWS")]
    hosts = [s for s in seg_items if has(s, "HOST")]
    talk = [s for s in seg_items if has(s, "TALK HOUR")]
    weather = [s for s in seg_items if has(s, "WEATHER")]
    govt = [s for s in seg_items if has(s, "GOVERNMENT")]
    order, idents = [], list(ident_items)

    def music():
        if song_pool:
            s = song_pool.pop(0)
            order.append({"type": "song", "videoId": s["videoId"], "duration": s["duration"]})
        else:
            order.append({"type": "music", "playlist": random.choice(PLAYLISTS), "songs": 1})

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


def main():
    print(f"=== INCH RADIO block · {tod} · {season} ===")
    segs, idents, wx = generate()
    seg_items, ident_items = render_all(segs, idents)
    song_pool = fetch_song_pool(load_playlists())
    print(f"song pool: {len(song_pool)} tracks with durations")
    manifest = build_order(seg_items, ident_items, song_pool)
    print(f"manifest: {len(manifest['items'])} items, {len(seg_items)} segments, {len(ident_items)} idents")
    publish(manifest)
    print("DONE")


if __name__ == "__main__":
    main()
