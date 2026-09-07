"""
Host talk test rig. Text only, no audio, no upload, nothing published.

Tests the redesign: instead of one thread handed to the host with an order to
riff from his own head, the AI first READS the board (a relation pass across
~14 live threads) and picks out what it noticed, then writes each host talk
from a cluster of 3-4 related threads with real posts to quote.

Run from eoi/ with the eoi venv:
    .venv/Scripts/python hosttalk_test.py
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
load_dotenv()

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import simplechan
import groq_limits

GROQ_MODEL = os.getenv("GROQ_MODEL") or "qwen/qwen3.8-27b"
BOARD = "b"


def call_groq(system, user, max_tokens=900, json_mode=False, temp=0.85):
    from groq import Groq
    client = Groq(api_key=os.getenv("GROQ_API_KEY"))
    kwargs = dict(model=GROQ_MODEL,
                  messages=[{"role": "system", "content": system},
                            {"role": "user", "content": user}],
                  temperature=temp, max_tokens=max_tokens)
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    return groq_limits.chat(client, **kwargs).choices[0].message.content


# ----------------------------------------------------------------- time
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


# ----------------------------------------------------------------- prompts
# WORLD is the live one from radio/run_block.py, with the atmosphere clause
# tightened: New Lhasa may still be felt, but it is not licence to fill airtime
# with sea and weather when the segment is meant to be about the board.
WORLD = f"""You write spoken radio for INCH RADIO, the New Lhasa station. The station broadcasts the life of an anonymous Indian imageboard, Indiachan /b/, as if it were the news of New Lhasa, a far-off cold island outpost.
The station is ALWAYS called Inch Radio. Never call the station Indiachan Radio. "Indiachan" is only the name of the board you cover, never the station.
New Lhasa is FELT, never announced. Never welcome anyone to an island or explain the setting. Let it leak only through cold, sea, distance, and the weather. At most ONE short line of that per segment. It is seasoning, never the subject.
Voice: spoken, reads aloud cleanly, always contract (it's, you're, don't, there's, that's, he's) and never the written-out forms, no headlines, no stage directions, no sound-effect notes. Write the way people talk on air, not the way they write: short clauses, breath, the occasional ellipsis or comma for a natural pause. The host is unnamed: "the desk", "your host". Never use em dashes. Do not moralise. The host is UNSHOCKABLE: nothing on the board fazes, disturbs, or surprises him, he has heard far worse a thousand times. He relays the ugliest, most violent, strangest things flatly and casually, like old news, never recoiling, never tut-tutting, no "can you believe it", no concern, no judgement, no "ouch" or "tough one". It is all perfectly ordinary to him. Report what was said as it was said; you can be blunt and uncensored. Every sentence complete.
TIME: it is {tod} in India. {greeting} Say the time of day only as morning, afternoon, evening, or night. NEVER speak a clock time or a number for the hour.
LENGTH IS A HARD CEILING, not a target. Count as you go and stop at or under the number this segment gives you. Running over gets the end of the segment cut off mid-word on air. Land the ending early rather than late.
Return ONLY the spoken words the host says, as plain text. No JSON, no labels, no list, no quotation marks wrapped around the whole thing. Just what goes out on air."""

READ_SYS = """You are the host of a radio station that covers an anonymous Indian imageboard, Indiachan /b/. You have just opened the board and you are scanning what is on it right now, the way a person actually does: not thread by thread in order, but noticing that a few of them are circling the same nerve.

Group the threads below into readings. Each reading is 3 or 4 threads that genuinely share something: the same argument, the same grievance, the same obsession, the same kind of person posting, or two threads that flatly contradict each other. Contradiction counts, and is often the best reading.

For each reading give:
  "noticed": one plain spoken sentence saying what you noticed, in the host's mouth. Concrete and specific, naming the actual subject. "The caste fight is back and this time it's the jatts" is good. "Users are discussing identity" is worthless.
  "ids": the thread ids in that reading.
  "angle": one sentence on the host's stance. He can be baffled, amused, dismissive, on one side, or genuinely unable to see what the issue is.

Do NOT force a grouping that isn't there. If threads only relate loosely, say what the loose thing is honestly, in the host's voice. Never use the words "theme", "discourse", "narrative", "community", or "conversation".

Return JSON: {"readings": [{"noticed": "...", "ids": [1,2,3], "angle": "..."}]}. Give 3 readings."""

HOST_FMT = """Format: HOST TALK. The host has the board open in front of him and is giving you his read on this corner of it.

OPEN by looking at the board and saying what he noticed, in his own words. Something in the shape of "so, taking a look at the board", "right, so I've been scrolling through this lot", "alright, what have we got tonight". Never a bulletin, never "here's the news", never a flat title drop.

Then WALK THE THREADS. For each one, NAME the actual subject plainly and QUOTE or closely paraphrase a real line from a real poster, attributed as "this anon", "one anon said", "another post", "someone further down". BE CONCRETE. If a poster is on about a caste fight, a slur, his sister, a job rejection, a specific game or show, SAY exactly that. BANNED phrasings: "a particular topic", "certain activities", "a recent event", "strong opinions", "social issues", "their thoughts on something", "mixed reactions", "an interesting discussion".

The opinion comes from REACTING to the posts, not from the host's inner life. He is allowed to take a side, to find it stupid, to be amused, to say he doesn't get what the issue is. He is NOT allowed to talk about himself, his room, his tea, his loneliness, the sea, the cold, or his feelings. He has no biography. If he says "I", it must be "I don't get it", "I've seen this one before", "I'd say", never "I remember" or "I feel".

Every claim he makes must be traceable to a post below. If he cannot source it, he does not say it.

End on the noticing, not on a moral. No lesson, no wrap-up wisdom, no "at the end of the day".

14 to 20 sentences."""


def brief(t, n_replies=5, op_chars=280, rep_chars=150):
    return {"id": t.get("no"), "subject": (t.get("subject") or "").strip(),
            "op": (t.get("body") or "")[:op_chars],
            "replies": [(r.get("body") or "")[:rep_chars]
                        for r in (t.get("replies") or [])[:n_replies]]}


def main():
    print("fetching catalog...", flush=True)
    cat = simplechan.fetch_catalog(BOARD)
    pool = [t for t in cat if not t.get("pinned")]
    pool.sort(key=lambda t: t.get("reply_count", 0), reverse=True)
    top = pool[:14]

    threads = []
    for t in top:
        try:
            full = simplechan.fetch_thread(BOARD, t["no"])
            full["_n"] = t.get("reply_count", 0)
            threads.append(full)
            print("  #%s (%s replies) %s" % (t["no"], t.get("reply_count", 0),
                                             (t.get("subject") or "")[:50]), flush=True)
        except Exception as e:
            print("  skip", t.get("no"), e, flush=True)
        time.sleep(1)

    by_id = {t.get("no"): t for t in threads}

    print("\n--- PASS 1: reading the board ---", flush=True)
    payload = json.dumps([brief(t) for t in threads], ensure_ascii=False)
    raw = call_groq(READ_SYS, "The board right now:\n" + payload, 900, json_mode=True, temp=0.9)
    readings = json.loads(raw).get("readings", [])
    for r in readings:
        print("  NOTICED: %s" % r.get("noticed"))
        print("    ids: %s  angle: %s\n" % (r.get("ids"), r.get("angle")), flush=True)

    print("--- PASS 2: writing host talk ---\n", flush=True)
    out = []
    for i, r in enumerate(readings, 1):
        ids = [x for x in (r.get("ids") or []) if x in by_id]
        if not ids:
            print("  reading %d: no valid ids, skipped" % i, flush=True)
            continue
        mat = [brief(by_id[x], n_replies=10, op_chars=600, rep_chars=220) for x in ids]
        user = ("What you noticed when you opened the board: " + str(r.get("noticed")) +
                "\nYour angle on it: " + str(r.get("angle")) +
                "\n\nThe threads, with real posts to quote:\n" +
                json.dumps(mat, ensure_ascii=False))
        text = call_groq(WORLD + "\n\n" + HOST_FMT, user, 1200, temp=0.9).strip()
        sents = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text) if p.strip()]
        if sents and not sents[-1].rstrip().endswith((".", "!", "?", '."', '!"', '?"')):
            sents.pop()
        body = " ".join(sents)
        words = len(body.split())
        out.append((r, ids, body, len(sents), words))
        print("=" * 78)
        print("[ HOST TALK %d ]  threads %s" % (i, ", ".join("#" + str(x) for x in ids)))
        print("  noticed: %s" % r.get("noticed"))
        print("  %d sentences, %d words, ~%.0fs on air at William's pace\n" % (len(sents), words, words / 2.4))
        print(body)
        print(flush=True)

    with open("hosttalk_test_output.txt", "w", encoding="utf-8") as f:
        f.write("HOST TALK TEST  %s IST  model=%s\n\n" % (now.strftime("%Y-%m-%d %H:%M"), GROQ_MODEL))
        for r, ids, body, ns, words in out:
            f.write("=" * 78 + "\n")
            f.write("threads %s\n" % ", ".join("#" + str(x) for x in ids))
            f.write("noticed: %s\nangle: %s\n" % (r.get("noticed"), r.get("angle")))
            f.write("%d sentences, %d words, ~%.0fs\n\n%s\n\n" % (ns, words, words / 2.4, body))
    print("written to hosttalk_test_output.txt")


if __name__ == "__main__":
    main()
