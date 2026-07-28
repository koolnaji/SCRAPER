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

Two-track redesign to relieve free-tier RPM pressure (a per-article call
on every scraped article was the actual bottleneck, same shape of
problem language ID had before language_judges.py -- see is_suspicious()
and queue_boilerplate_check() below):

  Track 1 -- suspicion filter: most well-formed, normal-length articles
  never generate an LLM call at all now. is_suspicious() decides on cheap
  local signals (word count, short-paragraph ratio) whether an article's
  extracted text is even worth spending a call on. Deliberately biased
  the other way for SHORT text specifically: a suspiciously short
  extraction is exactly the shape of a genuine extraction failure (see
  DR.dk's cookie-banner-as-full-article bug), so short articles are MORE
  likely to get checked, not less -- the filter cuts volume on the
  articles that are probably fine, not on the ones most likely to
  actually need review.

  Track 2 -- batching: articles that DO pass the suspicion filter aren't
  sent one at a time either. They queue up (queue_boilerplate_check) and
  get sent as ONE combined request per BATCH_SIZE articles
  (flush_boilerplate_queue), since the free tier's real constraint is
  requests-per-minute, not tokens-per-minute -- a batch of a dozen-plus
  short article samples is still a small fraction of the TPM cap, so
  batching turns N requests into 1 for a proportional RPM saving without
  meaningfully touching the OTHER limit.

Uses Google's Gemini API (genuine free tier, no credit card required) via
the official `google-genai` SDK, not the older `google-generativeai`
package. Requires:

  pip install google-genai
  export GEMINI_API_KEY=...   (free key: https://aistudio.google.com/apikey)

  Optional: export GEMINI_API_KEYS="key1,key2,key3" instead, to rotate
  across multiple free-tier keys when one runs into its rate/quota limit
  (see the _GeminiKeyPool class below for details and a terms-of-service
  caveat worth reading first). NOTE: per Google's own rate-limit docs,
  quota is enforced per PROJECT, not per key -- rotation here only helps
  if these keys are actually on separate Google Cloud projects; same-
  project keys share one pool and rotating between them buys nothing.

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
import asyncio
from datetime import datetime, timezone
from urllib.parse import urlparse

from tqdm import tqdm

from gemini_retry import GeminiRetryCaller

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

# --------------------------------------------------------------------------
# Track 1: suspicion filter -- decides whether an article is even worth
# an LLM pass. See module docstring for the "short = MORE suspicious,
# not less" reasoning.
# --------------------------------------------------------------------------
MIN_WORDS_FOR_TRUST = 80  # below this word count, always check -- this is
                           # roughly the size of a short blurb; DR.dk's
                           # cookie-banner-as-full-article bug produced
                           # well under this many words, and a genuinely
                           # short-but-legitimate article costs one wasted
                           # batch slot, which is cheap insurance against
                           # silently filing a broken extraction

MIN_PARAGRAPHS_FOR_LINE_SHAPE_CHECK = 3   # below this many paragraphs,
                                           # the ratio check below is too
                                           # noisy on such a small sample
                                           # to mean anything (skip it,
                                           # the word-count check above
                                           # already covers very short
                                           # articles anyway)
SHORT_PARAGRAPH_WORD_LIMIT = 6            # a paragraph this short reads
                                           # more like a nav link or list
                                           # item than a sentence
SHORT_PARAGRAPH_RATIO_THRESHOLD = 0.4     # if >=40% of an article's
                                           # paragraphs are that short,
                                           # the overall SHAPE looks like
                                           # a leaked link list / related-
                                           # content block rather than
                                           # normal prose, regardless of
                                           # total word count


def is_suspicious(text):
    """True = worth an LLM boilerplate pass (queue it). False = looks
    like normal, well-formed prose -- skip the LLM pass entirely. This
    is where most of the call-volume reduction actually comes from,
    since most scraped articles ARE normal prose and never reach Gemini
    at all under this gate; see the module docstring's Track 1.

    Two DIFFERENT failure shapes get checked here, deliberately kept as
    separate signals rather than one combined score:

      - Edge check: a single short/boilerplate-looking line at the very
        start or end of the article, however long the article otherwise
        is. Boilerplate concentrates at the edges in practice (bylines/
        consent banners up top, footers/related-content/promo blocks at
        the bottom -- see MAX_HEAD_CHARS/MAX_TAIL_CHARS's own comment on
        this same pattern). One boilerplate sentence tacked onto 30 real
        paragraphs is a ~3% short-paragraph ratio -- nowhere near the
        whole-document threshold below -- but it's exactly the shape a
        real footer leak takes, so it needs its own check rather than
        being averaged away by a document-wide ratio.
      - Whole-document ratio: catches the OPPOSITE shape, an article
        that's mostly link-list/nav-shaped rather than prose throughout
        (a related-content widget, a nav dump) -- too diffuse for the
        edge check above to catch, since there's no single "the"
        boilerplate line, the whole thing reads that way.
    """
    words = text.split()
    if len(words) < MIN_WORDS_FOR_TRUST:
        return True

    paragraphs = [p for p in text.split("\n") if p.strip()]
    if not paragraphs:
        return False

    edge_paragraphs = [paragraphs[0]]
    if len(paragraphs) > 1:
        edge_paragraphs.append(paragraphs[-1])
    if any(len(p.split()) <= SHORT_PARAGRAPH_WORD_LIMIT for p in edge_paragraphs):
        return True

    if len(paragraphs) >= MIN_PARAGRAPHS_FOR_LINE_SHAPE_CHECK:
        short_count = sum(1 for p in paragraphs if len(p.split()) <= SHORT_PARAGRAPH_WORD_LIMIT)
        if short_count / len(paragraphs) >= SHORT_PARAGRAPH_RATIO_THRESHOLD:
            return True

    return False


# --------------------------------------------------------------------------
# Track 2: batching -- see module docstring. BATCH_SIZE is a request-count
# lever, not a token-budget one (TPM headroom is not the binding
# constraint here); kept modest anyway to limit one failed batch call's
# blast radius and keep the model's structured multi-item output
# reliable.
# --------------------------------------------------------------------------
BATCH_SIZE = 15

_BATCH_DETECTION_SYSTEM_PROMPT = """You audit MULTIPLE scraped news articles at once for leftover site chrome that a text extractor failed to strip out: cookie-consent notices, related-article link lists, bylines/credits, editorial-policy footers, social-share prompts, promotional blurbs, navigation labels, or any other fragment that is not part of an article's actual prose.

You will receive a JSON array of objects, each with "id" (an integer) and "text" (one article's sample text). Treat each article independently -- do not let one article's content influence what counts as boilerplate in another.

Only flag text that is clearly NOT part of the article it came from. Do not flag real sentences just because they're short. Do not flag anything you are not reasonably confident is boilerplate.

Respond with ONLY a JSON array, one object per input article, IN THE SAME ORDER as the input, each with exactly two keys:
  "id": the matching input id
  "candidates": an array of {"fragment": ..., "reason": ...} objects (fragment copied verbatim from that article's text), or an empty array if nothing was flagged

Example response: [{"id": 0, "candidates": []}, {"id": 1, "candidates": [{"fragment": "Follow us on social media", "reason": "social-share prompt"}]}]"""

_boilerplate_caller = None  # constructed lazily, right after
                            # get_gemini_client/rotate_gemini_key/
                            # mark_gemini_key_exhausted/gemini_key_count
                            # are defined below -- see
                            # _get_boilerplate_caller()

_pending_batch = []  # list of {"output_dir", "url", "text"} dicts queued
                     # by queue_boilerplate_check(), flushed either when
                     # it hits BATCH_SIZE or by an explicit
                     # flush_boilerplate_queue() call at the end of a run.


def reset_quota_flag():
    """Call at the start of each top-level run (run_auto/run_url_mode)
    before any scraping begins, so a previous run's rate-limit trip
    doesn't silently carry over into a fresh one in the same process."""
    global _pending_batch
    _get_boilerplate_caller().reset()
    _pending_batch = []  # a previous (possibly interrupted) run's
                         # not-yet-flushed items shouldn't bleed into a
                         # fresh run's batch
    _key_pool.reset()


def reset_gemini_key_pool():
    """Standalone reset for callers that need to clear key-exhaustion
    tracking without touching THIS module's own _quota_exhausted flag --
    language_voices.py's reset_language_quota_flag() calls this directly
    rather than reset_quota_flag(), since the two flags are intentionally
    independent (see language_voices.py's module docstring) even though
    they share this one underlying key pool."""
    _key_pool.reset()


class _GeminiKeyPool:
    """Manages one or more Gemini API keys, rotating to the next one when
    the current key's free-tier quota runs out mid-run instead of just
    giving up. Reads GEMINI_API_KEYS (comma- or newline-separated) if
    set, falling back to the single-key GEMINI_API_KEY for anyone not
    using multiple keys -- so rotation is opt-in by way of which env var
    you set, no separate flag needed.

    NOTE ON TERMS OF SERVICE: rotating multiple free-tier keys/projects
    specifically to route around Google's rate limit is generally
    understood to run against the free tier's terms of service -- this
    exists because it was explicitly requested with that tradeoff in
    mind, not because it's risk-free. Worth reading
    https://ai.google.dev/gemini-api/docs/billing yourself before relying
    on this for anything you can't afford to have a key suspended over.

    One client per key, created lazily and cached (same reasoning as the
    single-client version this replaced -- don't pay setup cost for keys
    that never end up needed). Keys marked "exhausted" stay marked for
    the rest of the run (cleared by reset()), so a key that's already
    known to be out of quota isn't retried every single call.
    """

    def __init__(self):
        self._keys = None
        self._clients = {}
        self._exhausted = set()
        self._active_idx = 0

    def _load_keys(self):
        if self._keys is not None:
            return
        raw = os.environ.get("GEMINI_API_KEYS") or os.environ.get("GEMINI_API_KEY")
        if not raw:
            raise RuntimeError(
                "--detect-boilerplate / --detect-language-llm require a "
                "GEMINI_API_KEY (single key) or GEMINI_API_KEYS (comma- or "
                "newline-separated, for rotation across multiple keys) "
                "environment variable. Free key, no credit card required: "
                "https://aistudio.google.com/apikey"
            )
        seen = set()
        keys = []
        for k in re.split(r"[,\n]", raw):
            k = k.strip()
            if k and k not in seen:
                seen.add(k)
                keys.append(k)
        self._keys = keys

    def get_client(self):
        self._load_keys()
        from google import genai
        key = self._keys[self._active_idx]
        if key not in self._clients:
            self._clients[key] = genai.Client(api_key=key)
        return self._clients[key]

    def key_count(self):
        self._load_keys()
        return len(self._keys)

    def mark_current_exhausted(self):
        self._load_keys()
        self._exhausted.add(self._keys[self._active_idx])

    def rotate(self):
        """Advances to the next key that hasn't already been marked
        exhausted this run. Returns True if a fresh key is now active,
        False if every key in the pool is exhausted -- nothing left to
        rotate to, caller should give up for the rest of the run."""
        self._load_keys()
        for _ in range(len(self._keys)):
            self._active_idx = (self._active_idx + 1) % len(self._keys)
            if self._keys[self._active_idx] not in self._exhausted:
                return True
        return False

    def reset(self):
        """Clears exhausted-key tracking for a fresh run -- same
        reasoning as reset_quota_flag() generally: a previous run's
        exhaustion shouldn't silently carry over into a new one in the
        same interactive-menu session. Deliberately keeps whichever key
        is currently active rather than resetting to key 0, so repeated
        runs in one session spread load across the pool instead of
        hammering the first key every time."""
        self._exhausted.clear()


_key_pool = _GeminiKeyPool()


def get_gemini_client():
    """Returns the client for whichever key is CURRENTLY ACTIVE in the
    pool -- see rotate_gemini_key() for how that changes mid-run when a
    key's quota runs out. Fails fast with a clear message (missing
    package, missing key(s)) rather than a generic error buried inside
    the first article's scrape, matching the ensure_lemmatizer()
    fail-fast discipline.
    """
    try:
        from google import genai  # noqa: F401 -- import check only, for
                                    # the friendly error message below;
                                    # the pool does its own import too
    except ImportError:
        raise RuntimeError(
            "--detect-boilerplate / --detect-language-llm require the "
            "'google-genai' package (not the older 'google-generativeai'). "
            "Install it with: pip install google-genai"
        )
    return _key_pool.get_client()


def rotate_gemini_key():
    """Advances the shared key pool to its next non-exhausted key.
    Returns True if a fresh key is now active, False if every key has
    been exhausted this run. Exposed at module level (rather than just
    on _key_pool) so language_voices.py's independent retry loop can
    trigger rotation on the SAME pool -- a key exhausted by a boilerplate
    call is exhausted for a language call too, same account either way."""
    return _key_pool.rotate()


def mark_gemini_key_exhausted():
    """Marks the pool's currently active key as exhausted for the rest of
    this run. See rotate_gemini_key() for why this is shared with
    language_voices.py rather than each module tracking its own."""
    _key_pool.mark_current_exhausted()


def gemini_key_count():
    """How many keys are configured (1 unless GEMINI_API_KEYS has more).
    Used to decide whether "rotate" is even a meaningful option, and to
    phrase the exhaustion message correctly (singular vs plural)."""
    return _key_pool.key_count()


def _get_boilerplate_caller():
    """Lazily builds this module's GeminiRetryCaller -- deferred (rather
    than built at module import time) purely so it's defined after the
    key-pool functions it closes over, without needing to reorder this
    file around a forward reference. Cached after first call, same
    lazy-singleton shape as get_gemini_client()'s own client cache."""
    global _boilerplate_caller
    if _boilerplate_caller is None:
        _boilerplate_caller = GeminiRetryCaller(
            label="boilerplate detection",
            get_client=get_gemini_client,
            rotate_key=rotate_gemini_key,
            mark_exhausted=mark_gemini_key_exhausted,
            key_count=gemini_key_count,
            max_retries=MAX_RETRIES,
            initial_backoff_seconds=INITIAL_BACKOFF_SECONDS,
            fallback_note="The rest of the scrape keeps going normally, "
                           "just without this check.",
        )
    return _boilerplate_caller


def _call_gemini_with_retry(sample, system_prompt=_DETECTION_SYSTEM_PROMPT, max_output_tokens=1000):
    return _get_boilerplate_caller().call(DETECTION_MODEL, sample, system_prompt, max_output_tokens)


def _sample_for_detection(text):
    """Shared head+tail slicing used by both a single article's slot in a
    batch payload -- boilerplate in practice clusters at the start
    (bylines, consent banners) or end (footers, related-content, promo
    blocks) of the text, so this covers the cases actually seen so far;
    revisit if a real leak turns out to be buried in the middle of a
    long article."""
    if len(text) > MAX_HEAD_CHARS + MAX_TAIL_CHARS:
        return text[:MAX_HEAD_CHARS] + "\n\n[... article middle omitted ...]\n\n" + text[-MAX_TAIL_CHARS:]
    return text


def _parse_batch_response(raw_response_text, expected_count):
    """Returns a list of candidate-lists, length == expected_count,
    positionally aligned by the "id" each input article was given.
    Anything malformed -- unparseable JSON, a missing/out-of-range id,
    a non-list "candidates" field -- defaults that SLOT to [] rather
    than raising or (worse) misaligning one article's candidates onto
    another's log entry. A misattributed candidate would get logged
    under the wrong url/domain in boilerplate_candidates.json, which is
    worse than a silently missed one, since review works off exactly
    that domain/url pairing."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_response_text.strip())
    results = [[] for _ in range(expected_count)]
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return results
    if not isinstance(data, list):
        return results

    for item in data:
        if not isinstance(item, dict):
            continue
        idx = item.get("id")
        if not isinstance(idx, int) or not (0 <= idx < expected_count):
            continue
        candidates = item.get("candidates")
        if not isinstance(candidates, list):
            continue
        parsed = [
            {"fragment": str(c["fragment"]).strip(), "reason": str(c.get("reason", "")).strip()}
            for c in candidates if isinstance(c, dict) and c.get("fragment")
        ]
        results[idx] = parsed
    return results


def _flush_batch_sync(batch_items):
    """Blocking call -- run via asyncio.to_thread(). Sends ONE Gemini
    request covering every queued article at once; this is the actual
    RPM relief described in the module docstring's Track 2. Returns a
    list of (output_dir, url, candidates) tuples, same order as
    batch_items -- a failed call (quota exhausted, malformed response)
    fails safe to "nothing flagged" for every article in the batch
    rather than raising, same discipline the old single-article path
    used."""
    get_gemini_client()  # fail fast if package/key(s) missing

    payload = json.dumps(
        [{"id": i, "text": _sample_for_detection(item["text"])} for i, item in enumerate(batch_items)],
        ensure_ascii=False,
    )
    # Not a token-budget lever (see BATCH_SIZE's comment) -- just enough
    # headroom that a batch of flagged fragments across many articles
    # doesn't get cut off mid-response.
    max_tokens = min(8000, 200 + 300 * len(batch_items))

    response = _call_gemini_with_retry(payload, system_prompt=_BATCH_DETECTION_SYSTEM_PROMPT,
                                        max_output_tokens=max_tokens)
    if response is None:
        return [(item["output_dir"], item["url"], []) for item in batch_items]

    raw = getattr(response, "text", None)
    if not raw:
        return [(item["output_dir"], item["url"], []) for item in batch_items]

    parsed = _parse_batch_response(raw, len(batch_items))
    return [(batch_items[i]["output_dir"], batch_items[i]["url"], parsed[i]) for i in range(len(batch_items))]


async def queue_boilerplate_check(output_dir, url, text):
    """Call this once per saved article instead of hitting Gemini
    directly. Decides for itself (is_suspicious()) whether the article
    is even worth an LLM pass -- a no-op, returning immediately, for
    articles that look like normal well-formed prose. That's most
    scraped articles, which is where most of the call-volume reduction
    actually comes from (see the module docstring's Track 1).

    Suspicious articles queue up instead of triggering an immediate
    call, and this function auto-flushes (one batched Gemini request
    covering everything queued so far) once BATCH_SIZE has piled up, so
    a long auto-discover run doesn't hold an ever-growing queue in
    memory or delay ALL its candidate logging to the very end.

    The caller (run_auto / run_url_mode) still needs one final
    flush_boilerplate_queue() call after its scraping loop ends, to
    catch whatever's left in the queue below BATCH_SIZE -- see that
    function's docstring."""
    if not is_suspicious(text):
        return
    _pending_batch.append({"output_dir": output_dir, "url": url, "text": text})
    if len(_pending_batch) >= BATCH_SIZE:
        await flush_boilerplate_queue()


async def flush_boilerplate_queue():
    """Sends whatever's currently queued as one batched Gemini request,
    logs each article's candidates via log_boilerplate_candidates(), and
    prints a summary line for any article that got something flagged.

    Safe to call with an empty queue (no-op) -- callers can
    unconditionally call this at the end of a run without checking queue
    length themselves first. This MUST be called once at the end of
    run_auto/run_url_mode's scraping loop (when --detect-boilerplate is
    on) -- otherwise a run whose suspicious-article count doesn't
    happen to be an exact multiple of BATCH_SIZE will silently lose
    whatever's left sitting in the queue when the process exits."""
    global _pending_batch
    if not _pending_batch:
        return
    batch_items = _pending_batch
    _pending_batch = []

    results = await asyncio.to_thread(_flush_batch_sync, batch_items)
    for out_dir, url, candidates in results:
        added = log_boilerplate_candidates(out_dir, url, candidates)
        if added:
            tqdm.write(f"   🔍 LLM flagged {added} possible boilerplate fragment(s) "
                       f"({url}) → boilerplate_candidates.json")


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