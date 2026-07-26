"""
boilerplate_detector.py
========================
Optional LLM-assisted pass that flags leftover boilerplate/UI-chrome in
already-extracted, already-filtered article text -- the same kind of
leftover junk you've been finding by eye and adding to
boilerplate_patterns.py by hand (cookie banners, related-story widgets,
bylines, editorial footers, etc).

This does NOT touch what gets saved. It only LOGS candidates to
boilerplate_candidates.json (top level of the output dir, alongside
scraped_urls.txt) for you to review and, if they're real, promote into
BOILERPLATE_PATTERNS / END_OF_ARTICLE_MARKERS by hand -- same
evidence-backed-not-guessed discipline as everything else in
SITE_OVERRIDES. Nothing here auto-edits boilerplate_patterns.py.

Opt-in only (--detect-boilerplate / the interactive-menu prompt), off by
default.

Uses Google's Gemini API (genuine free tier, no credit card required) via
the official `google-genai` SDK, not the older `google-generativeai`
package. Requires:

  pip install google-genai
  export GEMINI_API_KEY=...   (free key: https://aistudio.google.com/apikey)

Two things worth knowing about the free tier before turning this on:
  1. It's rate-limited (requests-per-minute AND requests-per-day caps).
     Scraping a large batch with this on can burn through the daily quota
     partway through a run -- handled below by backing off on transient
     429s and giving up cleanly (not retrying forever) if the quota looks
     exhausted, so the rest of the scrape keeps running without this
     feature rather than getting stuck.
  2. Per Google's own billing docs, free-tier prompts/responses may be
     used to improve Google's products; that only stops once billing is
     enabled. Worth knowing since this sends scraped article text.
     Check https://ai.google.dev/gemini-api/docs/billing for the current
     wording before relying on this for anything sensitive.
"""

import os
import re
import json
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

# Free-tier model as of July 2026. Google ships new Flash generations
# every few months (this went 2.5 -> 3 -> 3.5 -> 3.6 across 2026 alone) --
# if this starts 404ing or a newer one's out, check
# https://ai.google.dev/gemini-api/docs/models for the current lineup and
# swap the string here. Considered pointing this at the "gemini-flash-
# latest" alias instead, which auto-follows whatever's newest -- didn't,
# because Google's own docs note that alias hot-swaps model behavior
# without warning (only a 2-week notice by email), and a silent behavior
# change on a classifier you're trusting to log accurate candidates is
# worse than an occasional manual bump. Pin a real version, update by
# hand when needed.
DETECTION_MODEL = "gemini-3.6-flash"

# Boilerplate can show up anywhere, but article text can run long and
# this is a per-article API call -- cap what gets sent to keep token
# usage (and free-tier RPD burn) down. Boilerplate in practice clusters
# at the start (bylines, consent banners) or end (footers, related-
# content, promo blocks) of the text, so a head+tail slice covers the
# cases actually seen so far; revisit if a real leak turns out to be
# buried in the middle of a long article.
MAX_HEAD_CHARS = 3000
MAX_TAIL_CHARS = 2000

# Free-tier RPM limits are tight (single digits to low teens depending on
# model/date) -- a transient 429 is expected under normal use, not a
# sign anything's broken. Back off and retry a couple of times; if it's
# still failing after that, it's more likely the daily quota is actually
# exhausted, so stop trying for the rest of this run (see
# _quota_exhausted below) instead of retrying every single article.
MAX_RETRIES = 3
INITIAL_BACKOFF_SECONDS = 5

_DETECTION_SYSTEM_PROMPT = """You audit scraped news-article text for leftover site chrome that a text extractor failed to strip out: cookie-consent notices, related-article link lists, bylines/credits, editorial-policy footers, social-share prompts, promotional blurbs, navigation labels, or any other fragment that is not part of the article's actual prose.

Only flag text that is clearly NOT part of the article. Do not flag real sentences just because they're short. Do not flag anything you are not reasonably confident is boilerplate. If the text looks like clean article prose throughout, return an empty array.

Respond with ONLY a JSON array. Each element must be an object with exactly two keys:
  "fragment": the exact substring from the input text (copied verbatim, so it can be located by exact match)
  "reason": one short phrase describing what kind of boilerplate it is

Example response: [{"fragment": "Follow us on social media", "reason": "social-share prompt"}]
If nothing is flagged, respond with: []"""

_gemini_client = None
_quota_exhausted = False  # tripped for the rest of a RUN once free-tier
                          # quota looks genuinely exhausted, not just a
                          # transient rate-limit blip -- reset at the start
                          # of each top-level run_auto/run_url_mode call,
                          # same pattern as cysill_client.py's
                          # reset_cysill_circuit_breaker() on the Welsh
                          # project. Without a reset, one run that trips
                          # this (which can happen from a burst of RPM-
                          # limit 429s, not just genuine daily-quota
                          # exhaustion) would silently disable boilerplate
                          # detection for every subsequent run in the same
                          # interactive-menu session, with no way back on
                          # short of restarting the script.


def reset_quota_flag():
    """Call at the start of each top-level run (run_auto/run_url_mode)
    before any scraping begins, so a previous run's rate-limit trip
    doesn't silently carry over into a fresh one in the same process."""
    global _quota_exhausted
    _quota_exhausted = False


def get_gemini_client():
    """Lazily creates and caches the Gemini client, same lazy-init
    pattern as ensure_lemmatizer()'s Stanza pipeline -- only pay the
    import/setup cost if --detect-boilerplate is actually used.

    Fails fast with a clear message (missing package, missing API key)
    rather than a generic error buried inside the first article's
    scrape, matching the ensure_lemmatizer() fail-fast discipline.
    """
    global _gemini_client
    if _gemini_client is not None:
        return _gemini_client
    try:
        from google import genai
    except ImportError:
        raise RuntimeError(
            "--detect-boilerplate requires the 'google-genai' package "
            "(not the older 'google-generativeai'). "
            "Install it with: pip install google-genai"
        )
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "--detect-boilerplate requires a GEMINI_API_KEY environment "
            "variable to be set. Free key, no credit card required: "
            "https://aistudio.google.com/apikey"
        )
    _gemini_client = genai.Client(api_key=api_key)
    return _gemini_client


def _parse_candidates(raw_response_text):
    """Strips ```json fences if the model added any despite JSON mode
    being requested, then parses. Returns [] (not a raise) on anything
    malformed -- a bad LLM response should never take down a scrape
    that's otherwise working fine."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_response_text.strip())
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    candidates = []
    for item in data:
        if isinstance(item, dict) and item.get("fragment"):
            candidates.append({
                "fragment": str(item["fragment"]).strip(),
                "reason": str(item.get("reason", "")).strip(),
            })
    return candidates


def _call_gemini_with_retry(client, sample):
    global _quota_exhausted
    if _quota_exhausted:
        return None

    from google.genai import types
    config = types.GenerateContentConfig(
        system_instruction=_DETECTION_SYSTEM_PROMPT,
        max_output_tokens=1000,
        response_mime_type="application/json",  # JSON mode: skips needing
                                                  # to ask nicely for no
                                                  # markdown fences
    )
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
            if is_rate_limit:
                _quota_exhausted = True
                print("\n⚠️  Gemini API rate/quota limit hit repeatedly -- "
                      "pausing boilerplate detection for the rest of this "
                      "run (free-tier RPM/RPD caps are easy to hit on a "
                      "big batch). The rest of the scrape keeps going "
                      "normally, just without this check.")
            return None
    return None


def detect_boilerplate_candidates_sync(text):
    """Blocking call -- run via asyncio.to_thread() from the async scrape
    path so it doesn't stall the Playwright event loop. Returns a list of
    {"fragment", "reason"} dicts, possibly empty. Never raises for
    ordinary API hiccups -- returns [] and lets the caller decide whether
    to note the failure, since a detection failure is not a reason to
    fail the whole scrape."""
    client = get_gemini_client()
    if len(text) > MAX_HEAD_CHARS + MAX_TAIL_CHARS:
        sample = text[:MAX_HEAD_CHARS] + "\n\n[... article middle omitted ...]\n\n" + text[-MAX_TAIL_CHARS:]
    else:
        sample = text

    response = _call_gemini_with_retry(client, sample)
    if response is None:
        return []
    raw = getattr(response, "text", None)
    if not raw:
        return []
    return _parse_candidates(raw)


async def detect_boilerplate_candidates(text):
    import asyncio
    return await asyncio.to_thread(detect_boilerplate_candidates_sync, text)


def log_boilerplate_candidates(output_dir, url, candidates):
    """Appends new candidates to boilerplate_candidates.json, deduped by
    (domain, fragment) so the same site-wide footer showing up on every
    article from that domain doesn't get logged fifty times over.

    Returns how many NEW entries were actually added (0 if everything was
    already logged, or candidates was empty), so the caller can decide
    whether to print anything.
    """
    if not candidates:
        return 0
    domain = urlparse(url).netloc.replace("www.", "")
    path = os.path.join(output_dir, "boilerplate_candidates.json")

    existing = []
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing = json.load(f)
            if not isinstance(existing, list):
                existing = []
        except (json.JSONDecodeError, OSError):
            existing = []  # corrupt/unreadable log shouldn't block new entries

    seen_keys = {(e.get("domain"), (e.get("fragment") or "").strip().lower())
                 for e in existing if isinstance(e, dict)}

    added = 0
    for c in candidates:
        fragment = c.get("fragment", "").strip()
        if not fragment:
            continue
        key = (domain, fragment.lower())
        if key in seen_keys:
            continue
        seen_keys.add(key)
        existing.append({
            "domain": domain,
            "url": url,
            "fragment": fragment,
            "reason": c.get("reason", ""),
            "detected_at": datetime.now(timezone.utc).isoformat(),
        })
        added += 1

    if added:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
    return added