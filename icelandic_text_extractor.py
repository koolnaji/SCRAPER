"""
Generic News Article Scraper
=============================
Works against (almost) any news site, not just RUV.

Two modes:

  Mode 1 - "auto":  give it one or more LISTING/SECTION pages
                     (e.g. a homepage or a "/world" section page).
                     It crawls the page, discovers likely article
                     links automatically, and scrapes all new ones.

      python news_scraper.py auto https://example.com/news https://example.com/sport

  Mode 2 - "url":   give it one or more specific ARTICLE URLs directly.
                     It scrapes exactly those, no discovery step.

      python news_scraper.py url https://example.com/news/some-headline-slug

Common options:
  --output-dir DIR       where .txt files + manifest go (default ./news_corpus).
                          Each saved article is auto-sorted into a per-language
                          subfolder under this dir (e.g. news_corpus/is/,
                          news_corpus/en/), detected from the article's own
                          text -- not from which site/URL it came from, so a
                          mixed-language listing page sorts itself correctly.
                          Undetectable/ambiguous text lands in news_corpus/unknown/.
                          The scraped_urls.txt manifest itself stays at the
                          top level, shared across all languages, since dedup
                          is corpus-wide.
  --lemmatize LANG_CODE  optional Stanza lemmatization, e.g. --lemmatize en
                          (leave off to just save raw extracted text).
                          Language detection for folder routing always runs
                          on the RAW text first, regardless of this flag.
  --delay SECONDS         pause between requests, default 1.5s (+jitter)

Requires:
  pip install playwright trafilatura lingua-language-detector tqdm
  playwright install chromium
  # only if you use --lemmatize:
  pip install stanza
"""

import os
import re
import sys
import hashlib
import random
import asyncio
import argparse
from urllib.parse import urlparse

from playwright.async_api import async_playwright
from tqdm import tqdm
import trafilatura

# Language detection for per-language output folders. Uses `lingua`
# rather than the more commonly-reached-for `langdetect`: tested against
# this project's actual RUV samples, langdetect consistently misread
# Icelandic as Norwegian/Swedish (confirmed on multiple real articles,
# not a one-off) -- it leans on character n-grams that overlap heavily
# with Icelandic's Nordic neighbors and evidently doesn't have enough
# signal for a smaller language like Icelandic to separate them
# reliably. lingua correctly identified all of them, including short
# ones, in the same test. Worth keeping in mind if this list ever needs
# to grow -- re-test any newly-added closely-related-language pair
# before trusting it, same caution either detector deserves.
try:
    from lingua import Language, LanguageDetectorBuilder
    _LINGUA_LANGUAGES = [
        Language.ICELANDIC, Language.ENGLISH, Language.WELSH,
        Language.BOKMAL, Language.NYNORSK, Language.SWEDISH, Language.DANISH,
        Language.GERMAN, Language.FRENCH, Language.SPANISH,
    ]
    _lingua_detector = LanguageDetectorBuilder.from_languages(*_LINGUA_LANGUAGES).build()
    LANGDETECT_AVAILABLE = True
except ImportError:
    LANGDETECT_AVAILABLE = False
    _lingua_detector = None
    print("⚠️  lingua not installed -- per-language output folders disabled, "
          "everything will land in 'unknown/' (pip install lingua-language-detector).")

DEFAULT_OUTPUT = os.path.join(os.getcwd(), "news_corpus")
MANIFEST_NAME = "scraped_urls.txt"
UNKNOWN_LANG_FOLDER = "unknown"

# Path fragments that almost never indicate an actual article, across
# most news CMSs (WordPress, custom, Drupal, etc.)
NON_ARTICLE_HINTS = [
    "/tag/", "/tags/", "/topic/", "/topics/", "/category/", "/categories/",
    "/author/", "/authors/", "/video/", "/videos/", "/photo/", "/photos/",
    "/gallery/", "/live/", "/subscribe", "/newsletter", "/about", "/contact",
    "/privacy", "/terms", "/search", "/rss", "/feed", "/login", "/signup",
    "/account", "/cart", "/advert",
]

STATIC_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".pdf",
    ".mp4", ".mp3", ".css", ".js", ".zip", ".ico",
)

# --------------------------------------------------------------------------
# Per-site overrides
# --------------------------------------------------------------------------
# Keyed by domain (www.-stripped). Each entry can supply:
#   article_selector      - CSS selector for the real article body, tried
#                            BEFORE trafilatura/generic-container guessing
#                            on this domain. Falls through to the generic
#                            path if this selector matches nothing.
#   exclude_selectors      - selectors for junk to strip OUT of whatever
#                            article_selector matched, before extracting
#                            paragraph text (e.g. share widgets, related-
#                            content boxes living inside the container).
#   listing_path_prefixes  - if set, discover_article_links() treats any
#                            same-domain link whose PATH starts with one
#                            of these as an article, instead of running
#                            the generic hyphen-count/digit-ID heuristic.
#                            Needed for sites using opaque IDs in the URL
#                            (BBC's /news/articles/<id> has neither
#                            hyphens nor a long digit run, so the generic
#                            heuristic would silently discover zero BBC
#                            articles from a listing page without this).
#
# Every entry below is evidence-backed from a real inspect_selectors.py
# run against a live article on that domain, not guessed -- see each
# comment for what was actually observed. If a site starts silently
# under- or mis-extracting, re-run the inspector against a fresh article
# on it before assuming the override itself has gone stale (templates do
# change; scoping to <article> plateaus at "identical to <main article>"
# rather than "guaranteed forever").
SITE_OVERRIDES = {
    "ruv.is": {
        # UPDATED after a second inspect_selectors.py run, this time
        # against an ordinary bylined news article (the first run was a
        # sports live-blog, which turned out to be a different template
        # -- see below):
        #
        #   'article'        -> 9 paragraphs, 1806 chars, paragraph #1 is
        #                        the reporter's byline ("Anna Lilja
        #                        Þórisdóttir") sitting as its own <p>.
        #   '.article-body'  -> 8 paragraphs, 1784 chars -- exactly the
        #                        same content MINUS that byline paragraph
        #                        (1806-1784=22 chars, and
        #                        len("Anna Lilja Þórisdóttir")==22 exactly,
        #                        so this is confirmed to be precisely the
        #                        byline dropped, nothing else lost).
        #
        # A byline name is a proper noun, not article prose -- worth
        # excluding structurally for a linguistic corpus rather than
        # leaving it in and filtering after the fact. '.article-body'
        # did NOT appear at all as a candidate on the earlier live-blog
        # URL (that template doesn't use the class), so this only
        # activates on the bylined-article template it was confirmed
        # against; live-blog pages fall through to the generic
        # trafilatura/'article'/'main' path exactly as before, since
        # extract_via_override() returns None when the selector matches
        # nothing rather than forcing it.
        "article_selector": ".article-body",
        "exclude_selectors": [],
        "listing_path_prefixes": None,
    },
    "bbc.com": {
        # inspect_selectors.py on a BBC News article: 'article' and
        # 'main article' both returned identical text (39 paragraphs);
        # bare 'main' added 5 extra paragraphs 'article' lacked (same
        # recirc-widget-inside-main pattern as RUV above). Nothing
        # suspicious flagged inside 'article' itself.
        #
        # inspect_selectors.py on a BBC Cymru Fyw article (also served
        # under bbc.com, distinguished only by path): 'article', 'main
        # article', and bare 'main' were all IDENTICAL (553 chars) --
        # no widget contamination on this one at all. 'article' still
        # covers it correctly, so one selector serves both sections.
        # Caveat: that Cymru Fyw sample was a short quiz-format page,
        # not a typical long-form story -- worth re-checking against an
        # ordinary Cymru Fyw article before fully trusting this on that
        # section specifically.
        "article_selector": "article",
        "exclude_selectors": [],
        # BBC's opaque IDs (/news/articles/c80ny93xlrvo,
        # /cymrufyw/erthyglau/c77g8jdmdkyo) have neither hyphens nor a
        # long digit run, so the generic looks_like_article() heuristic
        # would find zero BBC articles from any listing page. This is
        # the fix for that -- path PREFIX match instead.
        "listing_path_prefixes": ["/news/articles/", "/cymrufyw/erthyglau/"],
    },
}


def get_site_override(url):
    domain = urlparse(url).netloc.replace("www.", "").lower()
    return SITE_OVERRIDES.get(domain)


_stanza_pipeline = None  # lazily initialized, only if --lemmatize is used


# --------------------------------------------------------------------------
# URL / filename helpers
# --------------------------------------------------------------------------

def normalize_url(url):
    """Strip query string / fragment / trailing slash so the same article
    reached via different tracking params still dedupes to one entry."""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def safe_filename(url, max_len=80):
    parsed = urlparse(url)
    domain = parsed.netloc.replace("www.", "")
    slug = parsed.path.rstrip("/").split("/")[-1] or "index"
    slug = re.sub(r"[^a-zA-Z0-9\-_]", "_", slug)[:max_len]
    url_hash = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    return f"{domain}__{slug}__{url_hash}.txt"


def looks_like_article(url, base_domain):
    """Generic heuristic for 'is this link probably a news article,
    as opposed to a nav/category/tag/video page' - domain agnostic.

    Checked FIRST against SITE_OVERRIDES' listing_path_prefixes, if the
    domain has one -- exact path-prefix matching is strictly more
    reliable than the hyphen/digit-ID guesswork below for any site whose
    real article URLs don't happen to fit that shape (see BBC's opaque
    IDs, documented in SITE_OVERRIDES above)."""
    parsed = urlparse(url)
    if parsed.netloc.replace("www.", "") != base_domain.replace("www.", ""):
        return False  # stay on the same site
    path_lower = parsed.path.lower()
    if path_lower.endswith(STATIC_EXTENSIONS):
        return False
    if any(hint in path_lower for hint in NON_ARTICLE_HINTS):
        return False

    override = get_site_override(url)
    if override and override.get("listing_path_prefixes"):
        return any(path_lower.startswith(prefix)
                   for prefix in override["listing_path_prefixes"])

    path = parsed.path.rstrip("/")
    segments = [s for s in path.split("/") if s]
    if not segments:
        return False
    slug = segments[-1]

    hyphen_rich = slug.count("-") >= 3          # "biden-signs-new-bill-today"
    has_long_id = bool(re.search(r"\d{4,}", path))  # numeric article IDs
    deep_enough = len(segments) >= 2

    return deep_enough and (hyphen_rich or has_long_id)


# --------------------------------------------------------------------------
# Manifest (dedup across runs)
# --------------------------------------------------------------------------

def manifest_path(output_dir):
    return os.path.join(output_dir, MANIFEST_NAME)


def content_hash(text):
    """Hash of the normalized article body, so the same story reached via
    two different URLs (e.g. a retitled slug) is still recognized as a
    duplicate instead of being saved twice."""
    normalized = re.sub(r"\s+", " ", text.strip().lower())
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


def load_manifest(output_dir):
    """Returns (seen_urls, seen_hashes). Tolerates the old one-column
    (URL-only) manifest format from before content-hash dedup existed."""
    path = manifest_path(output_dir)
    seen_urls, seen_hashes = set(), set()
    if not os.path.exists(path):
        return seen_urls, seen_hashes
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            seen_urls.add(parts[0])
            if len(parts) > 1 and parts[1]:
                seen_hashes.add(parts[1])
    return seen_urls, seen_hashes


def append_to_manifest(output_dir, url, hash_value):
    with open(manifest_path(output_dir), "a", encoding="utf-8") as f:
        f.write(f"{url}\t{hash_value}\n")


# --------------------------------------------------------------------------
# Lemmatization (optional, lazy)
# --------------------------------------------------------------------------

def ensure_lemmatizer(lang_code):
    """Initialize the Stanza pipeline once, up front, before any scraping
    happens. Previously this was lazy-loaded inside the per-article loop,
    so a bad language code or a download failure would surface as a
    generic 'Error' on article 1, get silently retried on every
    subsequent article, and never give a clear top-level failure."""
    global _stanza_pipeline
    if _stanza_pipeline is not None:
        return
    import stanza
    print(f"Loading Stanza lemmatizer for '{lang_code}'...")
    try:
        _stanza_pipeline = stanza.Pipeline(lang_code, processors="tokenize,lemma")
    except Exception:
        print(f"Model for '{lang_code}' not found locally, downloading...")
        stanza.download(lang_code)
        _stanza_pipeline = stanza.Pipeline(lang_code, processors="tokenize,lemma")
    print("Lemmatizer ready!\n")


def lemmatize(text, lang_code):
    ensure_lemmatizer(lang_code)
    doc = _stanza_pipeline(text)
    lemmas = []
    for sentence in doc.sentences:
        for word in sentence.words:
            if word.lemma and word.lemma.strip():
                lemmas.append(word.lemma)
    return " ".join(lemmas)


# --------------------------------------------------------------------------
# Language detection (drives per-language output subfolders)
# --------------------------------------------------------------------------

def detect_language(text):
    """
    Best-effort ISO 639-1 code (e.g. 'is', 'en') for an article's RAW
    extracted text -- called before any --lemmatize step, since a
    lemmatized token stream is a worse detection input than natural
    text. Falls back to UNKNOWN_LANG_FOLDER if lingua isn't installed,
    or if the detector itself can't decide (very short or ambiguous
    text) -- the article is still saved either way, just parked in
    unknown/ instead of being silently dropped over a detection failure.

    _LINGUA_LANGUAGES above is a closed candidate list, not every
    language lingua knows -- if you start scraping a genuinely new
    language and it keeps landing in unknown/, add it to that list
    first (and spot-check a real sample or two -- see this function's
    surrounding comment on langdetect's Icelandic/Norwegian mix-up for
    why "add it and trust it blindly" isn't quite safe enough on its own).
    """
    if not LANGDETECT_AVAILABLE:
        return UNKNOWN_LANG_FOLDER
    sample = text[:2000]  # detection accuracy plateaus well before this; no need for the whole article
    result = _lingua_detector.detect_language_of(sample)
    if result is None:
        return UNKNOWN_LANG_FOLDER
    return result.iso_code_639_1.name.lower()


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------

# Selectors tried, in order, when trafilatura fails and we fall back to
# raw <p> scraping. Most news CMS templates wrap the actual story body in
# one of these; restricting to the first one found on the page means the
# fallback never even sees footer/header/nav paragraphs living outside
# it. Only if NONE of these match anything do we fall through to
# page-wide <p> (last resort, then filtered by looks_like_boilerplate).
ARTICLE_CONTAINER_SELECTORS = [
    "article", "main article", "[itemprop='articleBody']",
    ".article-body", ".article-content", "#article-body",
    "main", "[role='main']",
]

# Generic boilerplate signals for the page-wide <p> last-resort path.
# Deliberately broad/domain-agnostic (phone numbers, postal-style
# addresses, copyright lines, common contact-block labels in Icelandic
# and English) rather than RUV-specific strings, since the same fallback
# runs against whatever site auto/url mode is pointed at.
BOILERPLATE_PATTERNS = [
    r"s[ií]mi\s*[:.]?\s*\d",          # "Sími: 515-3000"
    r"\bnetfang\s*[:.]?",              # "Netfang:"
    r"\bkt\.?\s*\d{6}-?\d{4}\b",       # Icelandic kennitala
    r"\b\d{3}[-\s]\d{4}\b",            # bare phone number pattern
    r"^\S+\s+\d+,\s*\d{3}\s+\S+$",     # "Efstaleiti 1, 103 Reykjavík" -- street, postcode, place
    r"^©", r"\ball rights reserved\b",
    r"^\s*(privacy policy|terms of (use|service))\s*$",
    # Cookie-consent placeholder shown in place of a blocked third-party
    # embed (RUV, confirmed from a real scraped article: appeared 3x
    # verbatim in one piece, once per blocked embed -- since it's UI
    # chrome rendered as <p> tags INSIDE the article body container, it
    # survives container-scoping and needs a text-content check instead).
    r"innfellt efni frá annarri vefsíðu",
    r"vafrakök\w*",
    r"^viltu samt sjá\??$",   # standalone consent-box follow-up, confirmed
                              # appearing as its own <p> alongside the above
]
_BOILERPLATE_RE = re.compile("|".join(BOILERPLATE_PATTERNS), re.IGNORECASE)


def looks_like_boilerplate(paragraph_text):
    return bool(_BOILERPLATE_RE.search(paragraph_text.strip()))


async def extract_via_override(page, override):
    """
    Deterministic extraction for a domain with a SITE_OVERRIDES entry:
    pull <p> text from inside article_selector only, dropping any
    paragraph that falls inside one of exclude_selectors first. Runs
    BEFORE trafilatura for domains that have an override, since a
    selector already confirmed against a real page (via
    inspect_selectors.py) is more trustworthy than trafilatura's
    general-purpose heuristics for that specific site.

    Returns None (not raising) on no match, so callers can cleanly fall
    through to the generic trafilatura/container path -- an override
    matching nothing on a given page (a template change, or a page type
    the override wasn't built against, like a live-blog vs. a standard
    article) should degrade gracefully, not break the whole scrape.

    BUGFIX: confirmed on a real scraped RUV article -- a cookie-consent
    placeholder for a blocked embed ("Hér á að vera innfellt efni...")
    rendered as its own <p> INSIDE .article-body, once per blocked
    embed (3x in that one piece). Container-scoping alone can't catch
    this since it's not a widget living outside the container, it's UI
    text sitting directly among the real paragraphs -- needs the same
    looks_like_boilerplate() text-content check the generic fallback
    already used, applied here too now rather than only there.
    """
    selector = override.get("article_selector")
    if not selector:
        return None
    exclude_selectors = override.get("exclude_selectors") or []
    try:
        texts = await page.eval_on_selector_all(
            f"{selector} p",
            """(paragraphs, excludeSelectors) => {
                return paragraphs
                    .filter(p => !excludeSelectors.some(sel => p.closest(sel)))
                    .map(p => p.innerText.trim())
                    .filter(t => t.length >= 20);
            }""",
            exclude_selectors,
        )
    except Exception:
        return None
    if not texts:
        return None
    texts = [t for t in texts if not looks_like_boilerplate(t)]
    if not texts:
        return None
    return "\n\n".join(texts).strip()


async def fetch_html(page, url):
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    try:
        # BUGFIX: this used to wait for a single <p> to attach, then grab
        # the page immediately. On bylined-article templates (RUV and
        # presumably others), the header/byline/subhead block often
        # renders first and satisfies that check well before the real
        # body has hydrated in -- so the page got snapshotted mid-load,
        # and trafilatura was handed just the teaser+metadata block.
        # Waiting for a real paragraph COUNT (an actual body almost never
        # renders as a single <p>) is a much stronger signal the article
        # body itself is present, not just page chrome.
        await page.wait_for_function(
            "document.querySelectorAll('p').length >= 3", timeout=8000
        )
    except Exception:
        pass  # some pages render text without <p> tags, or genuinely have <3; trafilatura may still work
    try:
        # Best-effort extra grace period for anything still hydrating in
        # after the paragraph-count check passes (images, related-content
        # widgets, etc. finishing up) -- short timeout, never blocks long.
        await page.wait_for_load_state("networkidle", timeout=4000)
    except Exception:
        pass
    return await page.content()


async def extract_article_text(page, url):
    html = await fetch_html(page, url)

    # If this domain has a proven selector (built from an actual
    # inspect_selectors.py run, see SITE_OVERRIDES), try it first --
    # more precise than trafilatura's general-purpose guessing for a
    # site we've already confirmed the structure of. Falls through to
    # the generic path below if it matches nothing (template changed,
    # or this is a page type the override wasn't built against).
    override = get_site_override(url)
    if override:
        override_text = await extract_via_override(page, override)
        if override_text and len(override_text) > 200:
            return override_text

    # Primary: trafilatura, which is built for exactly this - pulling clean
    # article text out of arbitrary news-site HTML while dropping nav/ads/
    # comments/boilerplate automatically, no site-specific rules needed.
    text = trafilatura.extract(
        html,
        url=url,
        include_comments=False,
        include_tables=False,
        favor_recall=True,
    )

    # BUGFIX (second line of defense against the same render-timing race
    # fetch_html's paragraph-count wait targets): a result that clears the
    # 200-char floor but is still suspiciously short is usually a
    # byline/teaser block extracted successfully, not a genuinely short
    # article -- give the page a bit longer and retry extraction once
    # before accepting it. Only fires when the first attempt actually
    # improves, so a genuinely short article (e.g. the weather bulletin
    # style one-paragraph pieces) isn't churned pointlessly.
    if text and 200 < len(text.strip()) < 500:
        try:
            await page.wait_for_timeout(1500)
            html_retry = await page.content()
            text_retry = trafilatura.extract(
                html_retry, url=url, include_comments=False,
                include_tables=False, favor_recall=True,
            )
            if text_retry and len(text_retry.strip()) > len(text.strip()):
                text = text_retry
        except Exception:
            pass

    if text and len(text.strip()) > 200:
        # BUGFIX: trafilatura's own boilerplate detection targets nav/
        # ads/comments, not this specific cookie-consent-for-blocked-
        # embed placeholder -- it survives trafilatura's cleanup as a
        # plain paragraph in the middle of otherwise-real article text
        # (confirmed on RUV; nothing about the pattern is RUV-specific,
        # so this runs for every site on this path, not just overridden
        # ones). Split on paragraph breaks, drop any matching paragraph,
        # rejoin -- same check extract_via_override() and the page-wide
        # fallback below both use, kept in sync across all three paths.
        cleaned_paragraphs = [p for p in text.strip().split("\n\n")
                              if not looks_like_boilerplate(p)]
        cleaned = "\n\n".join(cleaned_paragraphs).strip()
        if cleaned:
            return cleaned

    # Fallback: raw <p> tag concatenation, for the rare page trafilatura
    # can't parse (e.g. unusual JS-rendered structure).
    #
    # BUGFIX: this used to query <p> across the WHOLE page, which pulls in
    # footer/header/sidebar paragraphs the primary path never sees (e.g.
    # RUV's address/phone footer block ending up saved as article text).
    # Now tries known article-container selectors first -- if the page
    # has one, only <p>s inside it are ever considered, so boilerplate
    # outside the container is structurally excluded, not just filtered.
    # Only falls through to page-wide <p> if no container matches, and
    # even then, filters against looks_like_boilerplate() as a second
    # line of defense.
    paragraphs = []
    used_container = False
    for selector in ARTICLE_CONTAINER_SELECTORS:
        container = await page.query_selector(selector)
        if container:
            paragraphs = await container.query_selector_all("p")
            if paragraphs:
                used_container = True
                break

    if not paragraphs:
        paragraphs = await page.query_selector_all("p")

    texts = []
    for p in paragraphs:
        t = (await p.inner_text()).strip()
        if len(t) < 40:
            continue
        if not used_container and looks_like_boilerplate(t):
            continue
        texts.append(t)
    fallback_text = "\n\n".join(texts)
    return fallback_text.strip() if fallback_text.strip() else None


async def discover_article_links(page, listing_url):
    base_domain = urlparse(listing_url).netloc
    await page.goto(listing_url, wait_until="domcontentloaded", timeout=30000)
    try:
        await page.wait_for_selector("a[href]", state="attached", timeout=10000)
    except Exception:
        print(f"   Could not load links from {listing_url}, skipping")
        return []

    raw_links = await page.eval_on_selector_all(
        "a[href]", "elements => elements.map(el => el.href)"
    )

    candidates = []
    for link in raw_links:
        if looks_like_article(link, base_domain):
            candidates.append(normalize_url(link))

    return list(dict.fromkeys(candidates))  # dedupe, keep order


# --------------------------------------------------------------------------
# Scrape a single URL (used by both modes)
# --------------------------------------------------------------------------

async def scrape_one(page, url, output_dir, lemmatize_lang, seen_hashes, index, total):
    try:
        tqdm.write(f"[{index}/{total}] {url}")
        text = await extract_article_text(page, url)

        if not text:
            tqdm.write("   💥 No usable text found")
            return False

        # Content-based dedup: catches the same story republished under a
        # different slug/URL, which URL-only dedup can't see.
        raw_hash = content_hash(text)
        if raw_hash in seen_hashes:
            tqdm.write("   ↩️  Duplicate content (already have this story under another URL) - skipping")
            append_to_manifest(output_dir, normalize_url(url), raw_hash)
            return True

        # Detected on the raw extracted text, BEFORE lemmatization -- a
        # lemmatized token stream (already reduced to dictionary forms,
        # function words flattened) is a worse detection input than
        # natural running text.
        lang_code = detect_language(text)

        if lemmatize_lang:
            tqdm.write("   Lemmatizing...")
            text = lemmatize(text, lemmatize_lang)

        lang_dir = os.path.join(output_dir, lang_code)
        os.makedirs(lang_dir, exist_ok=True)
        filename = safe_filename(url)
        filepath = os.path.join(lang_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(text)

        seen_hashes.add(raw_hash)
        append_to_manifest(output_dir, normalize_url(url), raw_hash)
        tqdm.write(f"   ✅ Saved: {lang_code}/{filename}")
        return True

    except Exception as e:
        tqdm.write(f"   💥 Error: {e}")
        return False


async def scrape_batch(page, urls, output_dir, lemmatize_lang, seen_hashes, delay):
    failed = []
    saved = 0
    with tqdm(total=len(urls), desc="Scraping", unit="article") as pbar:
        for i, url in enumerate(urls):
            ok = await scrape_one(page, url, output_dir, lemmatize_lang, seen_hashes, i + 1, len(urls))
            if ok:
                saved += 1
            else:
                failed.append(url)
            pbar.set_postfix({"✅": saved, "💥": len(failed)})
            pbar.update(1)
            if delay > 0 and i < len(urls) - 1:
                await asyncio.sleep(delay + random.uniform(0, delay * 0.5))

    if failed:
        tqdm.write(f"\n🔁 Retrying {len(failed)} failed URLs...\n")
        still_failed = []
        with tqdm(total=len(failed), desc="Retrying", unit="article") as pbar:
            for i, url in enumerate(failed):
                ok = await scrape_one(page, url, output_dir, lemmatize_lang, seen_hashes, i + 1, len(failed))
                if ok:
                    saved += 1
                else:
                    still_failed.append(url)
                pbar.set_postfix({"✅": saved, "💥": len(still_failed)})
                pbar.update(1)
                if delay > 0 and i < len(failed) - 1:
                    await asyncio.sleep(delay + random.uniform(0, delay * 0.5))
        if still_failed:
            tqdm.write(f"\n💥 {len(still_failed)} URLs could not be scraped after retry:")
            for u in still_failed:
                tqdm.write(f"   {u}")

    return saved


# --------------------------------------------------------------------------
# Modes
# --------------------------------------------------------------------------

async def run_auto(listing_urls, output_dir, lemmatize_lang, delay):
    os.makedirs(output_dir, exist_ok=True)
    already_urls, seen_hashes = load_manifest(output_dir)

    if lemmatize_lang:
        ensure_lemmatizer(lemmatize_lang)  # fail fast on a bad language code

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        page.set_default_timeout(30000)

        all_links = []
        for listing_url in listing_urls:
            print(f"Collecting links from: {listing_url}")
            links = await discover_article_links(page, listing_url)
            print(f"   Found {len(links)} candidate article links")
            all_links.extend(links)
            if delay > 0:
                await asyncio.sleep(delay)

        all_links = list(dict.fromkeys(all_links))
        new_links = [u for u in all_links if u not in already_urls]

        print(f"\n{len(new_links)} new articles to scrape ({len(already_urls)} already in corpus)")
        print(f"Saving corpus to: {output_dir}\n")

        if not new_links:
            print("💥 Nothing new - come back later!")
            await browser.close()
            return

        saved = await scrape_batch(page, new_links, output_dir, lemmatize_lang, seen_hashes, delay)
        await browser.close()
        print(f"\n🎉 Done! Saved {saved} new articles to {output_dir}/")


async def run_url_mode(article_urls, output_dir, lemmatize_lang, delay):
    os.makedirs(output_dir, exist_ok=True)
    article_urls = [normalize_url(u) for u in article_urls]
    article_urls = list(dict.fromkeys(article_urls))
    _, seen_hashes = load_manifest(output_dir)

    if lemmatize_lang:
        ensure_lemmatizer(lemmatize_lang)  # fail fast on a bad language code

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        page.set_default_timeout(30000)

        saved = await scrape_batch(page, article_urls, output_dir, lemmatize_lang, seen_hashes, delay)
        await browser.close()
        print(f"\n🎉 Done! Saved {saved} article(s) to {output_dir}/")


# --------------------------------------------------------------------------
# Interactive menu (launched with no CLI arguments -- run with args, e.g.
# `python icelandic_text_extractor.py auto <url>`, to skip straight to the
# argparse path below instead, for scripting/automation)
# --------------------------------------------------------------------------

def _print_banner():
    print("=" * 60)
    print("  📰  News Article Scraper")
    print("=" * 60)


def _prompt_choice(question, options):
    """options: list of (key, label) tuples. Returns the chosen key."""
    print(f"\n{question}")
    for key, label in options:
        print(f"  {key}) {label}")
    valid_keys = [k.lower() for k, _ in options]
    while True:
        choice = input("> ").strip().lower()
        if choice in valid_keys:
            return choice
        print(f"   Please enter one of: {', '.join(k for k, _ in options)}")


def _prompt_urls(prompt_text):
    print(f"\n{prompt_text}")
    print("(paste one or more URLs -- comma-separated, or one per line; blank line when done)")
    urls = []
    while True:
        line = input("> ").strip()
        if not line:
            if urls:
                break
            print("   Need at least one URL.")
            continue
        urls.extend(p.strip() for p in line.split(",") if p.strip())
    return urls


def _prompt_yes_no(question, default=False):
    suffix = "Y/n" if default else "y/N"
    while True:
        ans = input(f"{question} [{suffix}] ").strip().lower()
        if not ans:
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        print("   Please answer y or n.")


def interactive_menu():
    _print_banner()

    mode_key = _prompt_choice(
        "What would you like to do?",
        [("1", "Auto-discover articles from a listing/section page"),
         ("2", "Scrape specific article URL(s) directly")],
    )
    mode = "auto" if mode_key == "1" else "url"

    urls = _prompt_urls(
        "Enter the listing/section page URL(s):" if mode == "auto"
        else "Enter the article URL(s):"
    )

    do_lemmatize = _prompt_yes_no(
        "\nLemmatize the saved text (reduce every word to dictionary form)?",
        default=False,
    )
    lemmatize_lang = None
    if do_lemmatize:
        lemmatize_lang = input("   Stanza language code (e.g. is, en, cy): ").strip() or None

    output_dir = input(f"\nOutput directory [{DEFAULT_OUTPUT}]: ").strip() or DEFAULT_OUTPUT

    print(f"\n{'=' * 60}")
    print(f"  Mode:       {'Auto-discover' if mode == 'auto' else 'Direct URL(s)'}")
    print(f"  URLs:       {len(urls)} given")
    print(f"  Lemmatize:  {lemmatize_lang if lemmatize_lang else 'No'}")
    print(f"  Output dir: {output_dir}")
    print(f"{'=' * 60}\n")

    return mode, urls, output_dir, lemmatize_lang


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main():
    # No arguments at all -> interactive menu. Any arguments -> argparse
    # path below, unchanged, so scripted/automated calls keep working
    # exactly as before.
    if len(sys.argv) == 1:
        mode, urls, output_dir, lemmatize_lang = interactive_menu()
        delay = 1.5
        if mode == "auto":
            asyncio.run(run_auto(urls, output_dir, lemmatize_lang, delay))
        else:
            asyncio.run(run_url_mode(urls, output_dir, lemmatize_lang, delay))
        return

    parser = argparse.ArgumentParser(description="Generic news article scraper")
    sub = parser.add_subparsers(dest="mode", required=True)

    auto_p = sub.add_parser("auto", help="Crawl listing/section pages, auto-discover and scrape articles")
    auto_p.add_argument("urls", nargs="+", help="One or more listing/section page URLs")
    auto_p.add_argument("--output-dir", default=DEFAULT_OUTPUT)
    auto_p.add_argument("--lemmatize", default=None, help="Stanza language code, e.g. en, cy, is")
    auto_p.add_argument("--delay", type=float, default=1.5, help="Seconds to wait between requests (politeness/rate-limit avoidance, default 1.5)")

    url_p = sub.add_parser("url", help="Scrape one or more specific article URLs directly")
    url_p.add_argument("urls", nargs="+", help="One or more direct article URLs")
    url_p.add_argument("--output-dir", default=DEFAULT_OUTPUT)
    url_p.add_argument("--lemmatize", default=None, help="Stanza language code, e.g. en, cy, is")
    url_p.add_argument("--delay", type=float, default=1.5, help="Seconds to wait between requests (politeness/rate-limit avoidance, default 1.5)")

    args = parser.parse_args()

    if args.mode == "auto":
        asyncio.run(run_auto(args.urls, args.output_dir, args.lemmatize, args.delay))
    elif args.mode == "url":
        asyncio.run(run_url_mode(args.urls, args.output_dir, args.lemmatize, args.delay))


if __name__ == "__main__":
    main()