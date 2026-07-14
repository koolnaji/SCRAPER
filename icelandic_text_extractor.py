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
  --output-dir DIR       where .txt files + manifest go (default ./news_corpus)
  --lemmatize LANG_CODE  optional Stanza lemmatization, e.g. --lemmatize en
                          (leave off to just save raw extracted text)
  --delay SECONDS         pause between requests, default 1.5s (+jitter)

Requires:
  pip install playwright trafilatura
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
import trafilatura

DEFAULT_OUTPUT = os.path.join(os.getcwd(), "news_corpus")
MANIFEST_NAME = "scraped_urls.txt"

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
    as opposed to a nav/category/tag/video page' - domain agnostic."""
    parsed = urlparse(url)
    if parsed.netloc.replace("www.", "") != base_domain.replace("www.", ""):
        return False  # stay on the same site
    path_lower = parsed.path.lower()
    if path_lower.endswith(STATIC_EXTENSIONS):
        return False
    if any(hint in path_lower for hint in NON_ARTICLE_HINTS):
        return False

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
# Extraction
# --------------------------------------------------------------------------

async def fetch_html(page, url):
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    try:
        await page.wait_for_selector("p", state="attached", timeout=8000)
    except Exception:
        pass  # some pages render text without <p> tags; trafilatura may still work
    return await page.content()


async def extract_article_text(page, url):
    html = await fetch_html(page, url)

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
    if text and len(text.strip()) > 200:
        return text.strip()

    # Fallback: raw <p> tag concatenation, for the rare page trafilatura
    # can't parse (e.g. unusual JS-rendered structure).
    paragraphs = await page.query_selector_all("p")
    texts = []
    for p in paragraphs:
        t = (await p.inner_text()).strip()
        if len(t) >= 40:
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
        print(f"[{index}/{total}] {url}")
        text = await extract_article_text(page, url)

        if not text:
            print("   No usable text found")
            return False

        # Content-based dedup: catches the same story republished under a
        # different slug/URL, which URL-only dedup can't see.
        raw_hash = content_hash(text)
        if raw_hash in seen_hashes:
            print("   Duplicate content (already have this story under another URL) - skipping")
            append_to_manifest(output_dir, normalize_url(url), raw_hash)
            return True

        if lemmatize_lang:
            print("   Lemmatizing...")
            text = lemmatize(text, lemmatize_lang)

        filename = safe_filename(url)
        filepath = os.path.join(output_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(text)

        seen_hashes.add(raw_hash)
        append_to_manifest(output_dir, normalize_url(url), raw_hash)
        print(f"   Saved: {filename}")
        return True

    except Exception as e:
        print(f"   Error: {e}")
        return False


async def scrape_batch(page, urls, output_dir, lemmatize_lang, seen_hashes, delay):
    failed = []
    saved = 0
    for i, url in enumerate(urls):
        ok = await scrape_one(page, url, output_dir, lemmatize_lang, seen_hashes, i + 1, len(urls))
        if ok:
            saved += 1
        else:
            failed.append(url)
        if delay > 0 and i < len(urls) - 1:
            await asyncio.sleep(delay + random.uniform(0, delay * 0.5))

    if failed:
        print(f"\nRetrying {len(failed)} failed URLs...\n")
        still_failed = []
        for i, url in enumerate(failed):
            ok = await scrape_one(page, url, output_dir, lemmatize_lang, seen_hashes, i + 1, len(failed))
            if ok:
                saved += 1
            else:
                still_failed.append(url)
            if delay > 0 and i < len(failed) - 1:
                await asyncio.sleep(delay + random.uniform(0, delay * 0.5))
        if still_failed:
            print(f"\n{len(still_failed)} URLs could not be scraped after retry:")
            for u in still_failed:
                print(f"   {u}")

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
            print("Nothing new - come back later!")
            await browser.close()
            return

        saved = await scrape_batch(page, new_links, output_dir, lemmatize_lang, seen_hashes, delay)
        await browser.close()
        print(f"\nDone! Saved {saved} new articles to {output_dir}/")


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
        print(f"\nDone! Saved {saved} article(s) to {output_dir}/")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main():
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