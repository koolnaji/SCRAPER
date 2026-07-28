"""
language_voices.py
===================
Gemini-backed language identification -- ONE call this module exposes,
detect_language_llm(), used by language_judges.py's judge_language()
ensemble strictly as a TIEBREAKER, not a coequal second voice the way it
used to be.

Old design (superseded): every article got a lingua call AND, if the
flag was on, a Gemini call, and the two were reconciled directly in the
main script. That meant Gemini's call volume scaled with total articles
scraped, which is exactly what ran into the Gemini API's per-minute
request ceiling (RPM binds far sooner than TPM does for a call this
small -- a couple hundred tokens per sample against a 5 RPM free-tier
cap, well before the 250K TPM cap is anywhere close).

Current design: language_judges.py runs a panel of four LOCAL, offline
judges (GlotLID, OpenLID-v3, CLD3, lingua) first. Gemini, via this
module, is only ever invoked for the subset of articles that panel
couldn't already resolve on its own -- see judge_language() and
reconcile_gemini_tiebreak() in language_judges.py. That's what actually
fixes the RPM pressure, not just spreads the same per-article call rate
out with retries/backoff.

Opt-in only (--detect-language-llm / the interactive-menu prompt), off
by default -- same reasoning as boilerplate_detector.py: worth paying
for specifically when the local panel is genuinely split on what you're
scraping, not something to burn API budget on by default.

Reuses the SAME cached Gemini client as boilerplate_detector.py
(get_gemini_client() there is a module-level singleton, so this doesn't
spin up a second client) and the same pinned model/retry constants, so
there's one place to bump the model string when Google ships a new Flash
generation, not two. Keeps its OWN quota-exhausted flag, though -- a
language-ID call and a boilerplate call are different requests, and
tripping one voice's rate limit shouldn't be assumed to silently disable
the other (if the underlying daily quota really is exhausted, both will
independently trip anyway within a call or two of each other).
"""

import re
import json
import asyncio

from gemini_retry import GeminiRetryCaller
from boilerplate_detector import (
    get_gemini_client,
    DETECTION_MODEL,
    MAX_RETRIES,
    INITIAL_BACKOFF_SECONDS,
    rotate_gemini_key,
    mark_gemini_key_exhausted,
    gemini_key_count,
    reset_gemini_key_pool,
)

# Same slice detect_language() (the local lingua voice, in the main
# script) uses for its own sample. Keeping both voices looking at
# exactly the same stretch of text is what makes a disagreement between
# them meaningful -- if the API voice saw more of the article than the
# local one, a "disagreement" could just be that, not an actual split
# opinion on the same evidence.
MAX_SAMPLE_CHARS = 2000

_LANGUAGE_SYSTEM_PROMPT = """You identify the language of a piece of news-article text.

Respond with ONLY a JSON object with exactly two keys:
  "language_code": the two-letter ISO 639-1 code for the DOMINANT language of the text (lowercase, e.g. "is", "en", "cy")
  "confidence": a number from 0.0 to 1.0 for how confident you are that this code is correct. Use a LOW number (below 0.5) if the text is short, mixed-language, boilerplate-heavy, or otherwise ambiguous -- do not default to a high number out of habit.

If the text is genuinely mixed-language with no single dominant language, pick whichever language makes up more of the text and lower your confidence accordingly. If you cannot identify the language at all, use "unknown" as the language_code and 0.0 as the confidence.

Example response: {"language_code": "is", "confidence": 0.9}"""

# Gemini isn't a calibrated LID specialist the way the local judges are
# (see language_judges.py's own comment on why its base weight sits
# between CLD3 and the two fastText models), and its self-reported
# confidence field above is a best-effort ask, not a guaranteed-accurate
# probability. This floor is what a genuinely unremarkable, "yeah
# probably" call should land at -- used whenever the model's own
# confidence value is missing, malformed, or (as LLM self-reported
# confidence tends to run) implausibly pinned at the extremes. Previously
# this path just hardcoded 1.0 for every tiebreak vote regardless of how
# sure the model actually sounded, which meant Gemini's real contribution
# to a tiebreak (weight x confidence) was always its full 0.9 weight --
# quietly outweighing local judges who report their genuine, usually-
# sub-1.0 confidence on the same vote.
GEMINI_CONFIDENCE_FALLBACK = 0.6

_language_caller = GeminiRetryCaller(
    label="the LLM language voice",
    get_client=get_gemini_client,
    rotate_key=rotate_gemini_key,
    mark_exhausted=mark_gemini_key_exhausted,
    key_count=gemini_key_count,
    max_retries=MAX_RETRIES,
    initial_backoff_seconds=INITIAL_BACKOFF_SECONDS,
    fallback_note="Language detection keeps going on the local lingua "
                   "voice alone, just without a second opinion.",
)  # separate instance (own quota_exhausted flag) from
   # boilerplate_detector.py's caller by design -- see module docstring.
   # Both still route through the SAME underlying key pool via the
   # get_client/rotate_key/etc. callables above.


def reset_language_quota_flag():
    """Call at the start of each top-level run (run_auto/run_url_mode)
    before any scraping begins, mirroring boilerplate_detector.py's
    reset_quota_flag() -- so a previous run's rate-limit trip doesn't
    silently carry over into a fresh one in the same interactive-menu
    session. Also resets the SHARED key-exhaustion pool (via
    reset_gemini_key_pool(), not just this caller's own flag, since this
    flag and boilerplate's are intentionally independent -- see module
    docstring) so a previous run's rotated-through keys get another
    chance too."""
    _language_caller.reset()
    reset_gemini_key_pool()


def _parse_language_response(raw_response_text):
    """Strips ```json fences if the model added any despite JSON mode
    being requested, then parses and validates. Returns None (not a
    raise) on anything malformed or on the model's own "unknown" --
    either way the API voice has nothing usable to contribute, and the
    caller falls back to the local voice alone.

    Returns (code, confidence) on success. confidence falls back to
    GEMINI_CONFIDENCE_FALLBACK if the field is missing, non-numeric, or
    outside [0.0, 1.0] -- a malformed confidence field shouldn't sink an
    otherwise-valid language code, but it also shouldn't be silently
    trusted as 1.0 just because it round-tripped through JSON."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_response_text.strip())
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    code = str(data.get("language_code", "")).strip().lower()
    if not re.fullmatch(r"[a-z]{2}", code):
        return None  # covers "unknown" (3+ letters) and anything malformed

    raw_confidence = data.get("confidence")
    try:
        confidence = float(raw_confidence)
        if not (0.0 <= confidence <= 1.0):
            confidence = GEMINI_CONFIDENCE_FALLBACK
    except (TypeError, ValueError):
        confidence = GEMINI_CONFIDENCE_FALLBACK

    return code, confidence


def _call_gemini_with_retry(sample):
    return _language_caller.call(
        DETECTION_MODEL, sample, _LANGUAGE_SYSTEM_PROMPT, max_output_tokens=50,
    )


def detect_language_llm_sync(text):
    """Blocking call -- run via asyncio.to_thread() from the async scrape
    path so it doesn't stall the Playwright event loop. Returns a
    (lowercase 2-letter ISO 639-1 code, confidence) tuple, or None if the
    API voice couldn't produce one (quota exhausted, transient failure,
    or the model itself said "unknown") -- None means "no second opinion
    available", not "the article has no language", so callers should
    fall back to the local voice rather than treating None as a real
    answer."""
    get_gemini_client()  # fail fast if package/key(s) missing
    sample = text[:MAX_SAMPLE_CHARS]
    response = _call_gemini_with_retry(sample)
    if response is None:
        return None
    raw = getattr(response, "text", None)
    if not raw:
        return None
    return _parse_language_response(raw)


async def detect_language_llm(text):
    """Async wrapper. Returns a (code, confidence) tuple or None -- see
    detect_language_llm_sync(). Swallows get_gemini_client()'s RuntimeError
    (missing google-genai package or GEMINI_API_KEY) instead of letting it
    propagate -- this is only ever called as a tiebreaker for the subset
    of articles language_judges.py's local panel couldn't already
    resolve, so a missing/misconfigured Gemini client should just mean
    "the dispute stays a dispute, filed under DISPUTED_LANG_FOLDER" here,
    not fail the article scrape entirely."""
    try:
        return await asyncio.to_thread(detect_language_llm_sync, text)
    except Exception:
        return None