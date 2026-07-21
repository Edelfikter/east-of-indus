"""
Shared Groq rate limiting for the paper and the radio.

Groq's free tier caps tokens-per-minute per model (12,000 on
llama-3.3-70b-versatile). A full radio block is ~25,000 tokens across ~15 calls,
so firing it at full tilt trips the limit partway through. There is no daily
token cap, only a per-day *request* cap we are nowhere near, so pacing the calls
costs nothing: the block just takes ~3 minutes instead of ~1.

Two layers:

  1. A proactive throttle. Before each call we estimate its cost and, if the
     last 60 seconds of usage plus that estimate would breach the budget, we
     sleep until enough of the window has aged out. This means we normally never
     provoke a 429 at all, which matters because a refused request still burns
     one of the daily request allowance.

  2. Retry with backoff as a backstop, for anything the estimate misses or any
     transient error from Groq's side.

Every caller (compose.py, pulse.py, radio/run_block.py) routes its single
`client.chat.completions.create(...)` through `chat()` here.
"""
import os
import threading
import time
from typing import Optional

# Per-model tokens per minute. Override with GROQ_TPM if the model changes.
TPM = int(os.getenv("GROQ_TPM", "12000"))
# Leave headroom rather than riding the ceiling; our estimate is approximate.
SAFETY = 0.9
BUDGET = TPM * SAFETY
WINDOW = 60.0
MAX_ATTEMPTS = 5

_lock = threading.Lock()
_window: list = []  # (timestamp, tokens_used)


def _prune(now: float) -> None:
    cutoff = now - WINDOW
    while _window and _window[0][0] < cutoff:
        _window.pop(0)


def _used(now: float) -> int:
    _prune(now)
    return sum(tok for _, tok in _window)


def _estimate(kwargs: dict) -> int:
    """Rough cost of a call: prompt chars / 4, plus the full output allowance."""
    chars = sum(len(m.get("content") or "") for m in kwargs.get("messages", []))
    return chars // 4 + int(kwargs.get("max_tokens") or 1000)


def _record(tokens: int) -> None:
    with _lock:
        _window.append((time.time(), tokens))


def _wait_for_room(cost: int) -> None:
    while True:
        with _lock:
            now = time.time()
            used = _used(now)
            # If the window is empty we go regardless, otherwise a single call
            # larger than the whole budget would block forever.
            if not _window or used + cost <= BUDGET:
                return
            oldest = _window[0][0]
        sleep = max(0.5, oldest + WINDOW - time.time() + 0.25)
        print(f"  [groq] throttle: {used} tok used in last 60s, next call ~{cost}, "
              f"waiting {sleep:.1f}s", flush=True)
        time.sleep(sleep)


def _is_rate_limit(exc: Exception) -> bool:
    if type(exc).__name__ == "RateLimitError":
        return True
    return getattr(exc, "status_code", None) == 429


def _retry_after(exc: Exception) -> Optional[float]:
    resp = getattr(exc, "response", None)
    if resp is None:
        return None
    try:
        val = resp.headers.get("retry-after")
        return float(val) if val else None
    except Exception:
        return None


def chat(client, **kwargs):
    """Drop-in for client.chat.completions.create(**kwargs), throttled + retried."""
    cost = _estimate(kwargs)
    delay = 2.0
    last_exc = None

    for attempt in range(MAX_ATTEMPTS):
        _wait_for_room(cost)
        try:
            resp = client.chat.completions.create(**kwargs)
        except Exception as exc:
            if not _is_rate_limit(exc):
                raise
            last_exc = exc
            # The refused request still counted against us, so book the estimate.
            _record(cost)
            wait = _retry_after(exc) or delay
            print(f"  [groq] 429 on attempt {attempt + 1}/{MAX_ATTEMPTS}, "
                  f"retrying in {wait:.1f}s", flush=True)
            time.sleep(wait)
            delay = min(delay * 2, 60.0)
            continue

        usage = getattr(resp, "usage", None)
        _record(getattr(usage, "total_tokens", None) or cost)
        return resp

    raise last_exc
