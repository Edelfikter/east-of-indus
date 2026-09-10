"""
Shared Groq rate limiting for the paper and the radio.

Groq's free tier caps tokens-per-minute per model (8,000 across the current
roster). A full radio block is ~25,000 tokens across ~15 calls,
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
# 8,000 across every model on the free roster as of Sept 2026. It was 12,000
# on llama-3.3-70b-versatile, which Groq removed; leaving the old ceiling here
# means the throttle lets through half again as many tokens as we are allowed.
TPM = int(os.getenv("GROQ_TPM", "8000"))
# Groq added a separate, much tighter cap on OUTPUT tokens per minute (1,000 on
# the free tier as of Sept 2026). It is enforced per request against max_tokens,
# so a call asking for 1,900 is refused outright no matter how quiet the last
# minute was, and retrying it unchanged just burns all five attempts. That was
# killing roughly half the radio blocks. We clamp max_tokens to the ceiling and
# meter output separately from total tokens.
OTPM = int(os.getenv("GROQ_OTPM", "1000"))
# Leave headroom rather than riding the ceiling; our estimate is approximate.
SAFETY = 0.9
BUDGET = TPM * SAFETY
OUT_BUDGET = int(OTPM * SAFETY)
WINDOW = 60.0
MAX_ATTEMPTS = 5
# A retry-after longer than this is not a wait, it is an outage. Groq's daily
# window hands back 1,000+ second cooldowns, and sleeping one off inside a
# 30-minute CI job guarantees the job is cancelled with nothing published.
# 10 Sep 2026: five consecutive radio blocks died exactly this way (429 ->
# "retrying in 1009.0s" -> cancelled at 30m), so the 06:33 IST block stayed on
# air saying "good morning" until the evening.
MAX_RETRY_WAIT = float(os.getenv("GROQ_MAX_RETRY_WAIT", "120"))

class BudgetExceeded(RuntimeError):
    """Raised instead of sleeping past the caller's deadline, so the caller can
    degrade (publish a shorter block) rather than be killed mid-sleep."""


_deadline = None  # absolute unix time after which no call may sleep


def set_deadline(ts: Optional[float]) -> None:
    """Cap how long calls may block. None disables the cap."""
    global _deadline
    _deadline = ts


def _budget_left() -> Optional[float]:
    return None if _deadline is None else _deadline - time.time()


def _check_budget(wait: float, what: str) -> None:
    left = _budget_left()
    if left is not None and wait >= left:
        raise BudgetExceeded(
            f"{what} wants {wait:.0f}s, only {left:.0f}s of generation budget left")


_lock = threading.Lock()
_window: list = []  # (timestamp, tokens_used)
_out_window: list = []  # (timestamp, completion_tokens)


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


def _record(tokens: int, out_tokens: int = 0) -> None:
    with _lock:
        now = time.time()
        _window.append((now, tokens))
        if out_tokens:
            _out_window.append((now, out_tokens))


def _out_used(now: float) -> int:
    cutoff = now - WINDOW
    while _out_window and _out_window[0][0] < cutoff:
        _out_window.pop(0)
    return sum(tok for _, tok in _out_window)


def _wait_for_room(cost: int, out_cost: int = 0) -> None:
    while True:
        with _lock:
            now = time.time()
            used = _used(now)
            out = _out_used(now)
            # If a window is empty we go regardless, otherwise a single call
            # larger than the whole budget would block forever.
            total_ok = (not _window) or used + cost <= BUDGET
            out_ok = (not _out_window) or out + out_cost <= OUT_BUDGET
            if total_ok and out_ok:
                return
            oldest = min(w[0][0] for w in (_window, _out_window) if w)
        sleep = max(0.5, oldest + WINDOW - time.time() + 0.25)
        _check_budget(sleep, "throttle")
        print(f"  [groq] throttle: {used} tok / {out} out-tok used in last 60s, "
              f"next call ~{cost} ({out_cost} out), waiting {sleep:.1f}s", flush=True)
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


def _is_output_cap(exc: Exception) -> bool:
    msg = str(getattr(exc, "message", "") or exc).lower()
    return "output tokens per minute" in msg or "otpm" in msg


def chat(client, **kwargs):
    """Drop-in for client.chat.completions.create(**kwargs), throttled + retried."""
    if kwargs.get("max_tokens") and kwargs["max_tokens"] > OUT_BUDGET:
        print(f"  [groq] clamping max_tokens {kwargs['max_tokens']} -> {OUT_BUDGET} (OTPM cap)", flush=True)
        kwargs["max_tokens"] = OUT_BUDGET
    cost = _estimate(kwargs)
    delay = 2.0
    last_exc = None

    for attempt in range(MAX_ATTEMPTS):
        out_cost = int(kwargs.get("max_tokens") or 0)
        _wait_for_room(cost, out_cost)
        try:
            resp = client.chat.completions.create(**kwargs)
        except Exception as exc:
            if not _is_rate_limit(exc):
                raise
            last_exc = exc
            # The refused request still counted against us, so book the estimate.
            _record(cost, out_cost)
            # An output-cap refusal is deterministic: retrying the same max_tokens
            # fails identically every time, so come down before trying again.
            if _is_output_cap(exc) and kwargs.get("max_tokens"):
                kwargs["max_tokens"] = max(300, int(kwargs["max_tokens"] * 0.75))
                cost = _estimate(kwargs)
                print(f"  [groq] output cap hit, dropping max_tokens to {kwargs['max_tokens']}", flush=True)
            wait = _retry_after(exc) or delay
            if wait > MAX_RETRY_WAIT:
                # Waiting this out and retrying just burns the remaining attempts
                # against a window that has not moved. Surface it instead.
                raise BudgetExceeded(
                    f"Groq asked for a {wait:.0f}s cooldown (cap {MAX_RETRY_WAIT:.0f}s); "
                    f"abandoning this call so the rest of the block can still go out") from exc
            _check_budget(wait, f"429 backoff on attempt {attempt + 1}")
            print(f"  [groq] 429 on attempt {attempt + 1}/{MAX_ATTEMPTS}, "
                  f"retrying in {wait:.1f}s", flush=True)
            time.sleep(wait)
            delay = min(delay * 2, 60.0)
            continue

        usage = getattr(resp, "usage", None)
        _record(getattr(usage, "total_tokens", None) or cost,
                getattr(usage, "completion_tokens", None) or out_cost)
        return resp

    raise last_exc
