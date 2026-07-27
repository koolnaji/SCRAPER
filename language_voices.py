"""
language_voices.py
===================
Two-voiced language identification for the news scraper.

Voice 1 ("local"): the existing lingua-based detect_language() already
in the main script -- fast, free, offline, but limited to the closed
_LINGUA_LANGUAGES candidate list and (per that function's own comment)
occasionally wrong on closely-related languages it wasn't specifically
tested against.

Voice 2 ("api"), added here: Gemini, asked the same question over the
API -- broader effective language coverage since it isn't limited to a
hardcoded candidate list, but it's a network call on the same
rate-limited free tier as boilerplate_detector.py.

This module only speaks for voice 2. Reconciling the two voices (what to
do when they agree, when one has nothing to say, or when they genuinely
disagree) is the main script's job, in scrape_one() -- kept there rather
than here since the reconciliation policy needs UNKNOWN_LANG_FOLDER and
other main-script context, and duplicating that here would risk the two
copies drifting apart.

Opt-in only (--detect-language-llm / the interactive-menu prompt), off
by default, same reasoning as boilerplate_detector.py: an extra
per-article API call against the same free-tier caps, worth paying for
specifically when you don't trust lingua's candidate list on what you're
scraping, not on by default for everything.

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
import os
import json
import time
import asyncio
from datetime import datetime, timezone
from urllib.parse import urlparse

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

Respond with ONLY a JSON object with exactly one key:
  "language_code": the two-letter ISO 639-1 code for the DOMINANT language of the text (lowercase, e.g. "is", "en", "cy")

If the text is genuinely mixed-language with no single dominant language, pick whichever language makes up more of the text. If you cannot identify the language at all, use "unknown" as the value.

Example response: {"language_code": "is"}"""

_lang_quota_exhausted = False  # separate from boilerplate_detector's own
                                # flag by design -- see module docstring.


def reset_language_quota_flag():
    """Call at the start of each top-level run (run_auto/run_url_mode)
    before any scraping begins, mirroring boilerplate_detector.py's
    reset_quota_flag() -- so a previous run's rate-limit trip doesn't
    silently carry over into a fresh one in the same interactive-menu
    session. Also resets the SHARED key-exhaustion pool (via
    reset_gemini_key_pool(), not reset_quota_flag() itself, since this
    flag and boilerplate's are intentionally independent -- see module
    docstring) so a previous run's rotated-through keys get another
    chance too."""
    global _lang_quota_exhausted
    _lang_quota_exhausted = False
    reset_gemini_key_pool()


def _parse_language_response(raw_response_text):
    """Strips ```json fences if the model added any despite JSON mode
    being requested, then parses and validates. Returns None (not a
    raise) on anything malformed or on the model's own "unknown" --
    either way the API voice has nothing usable to contribute, and the
    caller falls back to the local voice alone."""
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
    return code


def _call_gemini_with_retry(sample):
    global _lang_quota_exhausted
    if _lang_quota_exhausted:
        return None

    from google.genai import types
    config = types.GenerateContentConfig(
        system_instruction=_LANGUAGE_SYSTEM_PROMPT,
        max_output_tokens=50,  # a JSON object holding a 2-letter code
                                 # needs almost nothing -- keep the free-
                                 # tier token burn negligible per call
        response_mime_type="application/json",
    )
    while True:
        client = get_gemini_client()  # current active key -- may have
                                        # changed since the last call if a
                                        # rotation happened partway
                                        # through this run (possibly
                                        # triggered by the OTHER voice,
                                        # boilerplate detection -- they
                                        # share one pool)
        for attempt in range(MAX_RETRIES):
            try:
                return client.models.generate_content(
                    model=DETECTION_MODEL,
                    contents=sample,
                    config=config,
                )
            except Exception as e:
                msg = str(e)
                is_rate_limit = "429" in msg or "RESOURCE_EXHAUSTED" in msg
                if is_rate_limit and attempt < MAX_RETRIES - 1:
                    time.sleep(INITIAL_BACKOFF_SECONDS * (2 ** attempt))
                    continue
                if not is_rate_limit:
                    return None  # not a rate-limit issue -- rotating keys wouldn't help
                break  # persistent 429s on THIS key after MAX_RETRIES

        mark_gemini_key_exhausted()
        if gemini_key_count() > 1 and rotate_gemini_key():
            print("\n🔁 Gemini key exhausted -- rotating to the next key in "
                  "GEMINI_API_KEYS and retrying (language voice)...")
            continue

        _lang_quota_exhausted = True
        which = "the only configured key" if gemini_key_count() == 1 else "every configured key"
        print(f"\n⚠️  Gemini API rate/quota limit hit repeatedly on {which} -- "
              "pausing the LLM language voice for the rest of this run. "
              "Language detection keeps going on the local lingua voice "
              "alone, just without a second opinion.")
        return None


def detect_language_llm_sync(text):
    """Blocking call -- run via asyncio.to_thread() from the async scrape
    path so it doesn't stall the Playwright event loop. Returns a
    lowercase 2-letter ISO 639-1 code, or None if the API voice couldn't
    produce one (quota exhausted, transient failure, or the model itself
    said "unknown") -- None means "no second opinion available", not
    "the article has no language", so callers should fall back to the
    local voice rather than treating None as a real answer."""
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
    """Async wrapper. Unlike boilerplate_detector's equivalent, this
    swallows get_gemini_client()'s RuntimeError (missing google-genai
    package or GEMINI_API_KEY) instead of letting it propagate -- the
    whole point of a two-voiced system is that losing one voice doesn't
    take down the other, so a missing/misconfigured API voice should
    just mean "local voice only" here, not fail the article scrape the
    way it would for the (single-voiced) boilerplate pass."""
    try:
        return await asyncio.to_thread(detect_language_llm_sync, text)
    except Exception:
        return None


def log_language_disagreement(output_dir, url, local_code, api_code):
    """Appends a record to language_disagreements.json (top level of the
    output dir, alongside scraped_urls.txt and boilerplate_candidates.json)
    whenever the two voices genuinely disagree on an article that both
    had an opinion about. Deduped by URL -- a retried URL shouldn't log
    the same split decision twice.

    This is a review log, same role as boilerplate_candidates.json: if a
    particular language code keeps showing up here, it's worth checking
    whether _LINGUA_LANGUAGES (in the main script) is missing that
    language, or misreading it as a close relative -- see the
    Icelandic/Norwegian mix-up documented on detect_language() itself for
    exactly this failure mode with the local voice alone.

    Returns True if a new entry was added, False if this URL was already
    logged (so the caller can decide whether to print anything)."""
    path = os.path.join(output_dir, "language_disagreements.json")

    existing = []
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing = json.load(f)
            if not isinstance(existing, list):
                existing = []
        except (json.JSONDecodeError, OSError):
            existing = []  # corrupt/unreadable log shouldn't block new entries

    if any(isinstance(e, dict) and e.get("url") == url for e in existing):
        return False

    existing.append({
        "url": url,
        "domain": urlparse(url).netloc.replace("www.", ""),
        "local_lingua_code": local_code,
        "api_gemini_code": api_code,
        "logged_at": datetime.now(timezone.utc).isoformat(),
    })
    with open(path, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    return True