# News Scraper

A generic article scraper that works against most news sites — no site-specific
selectors or junk-phrase lists needed. Two modes: auto-discover articles from a
listing page, or scrape specific article URLs directly.

## Install

```bash
pip install playwright trafilatura
playwright install chromium
```

Optional, only if you want lemmatized output:

```bash
pip install stanza
```

## Usage

**Mode 1 — auto:** point it at a listing/section page (homepage, `/world`, `/sport`,
etc.) and it discovers and scrapes article links on its own.

```bash
python news_scraper.py auto https://example.com/news https://example.com/sport
```

**Mode 2 — url:** scrape specific article URLs directly, no discovery step.

```bash
python news_scraper.py url https://example.com/news/some-headline-slug
```

## Options

| Flag | Default | Description |
|---|---|---|
| `--output-dir DIR` | `./news_corpus` | Where `.txt` files and the manifest are saved |
| `--lemmatize LANG` | off | Stanza language code (e.g. `en`, `cy`, `is`) — lemmatizes text before saving |
| `--delay SECONDS` | `1.5` | Pause between requests (+ random jitter), for politeness / rate-limit avoidance |

Both flags apply to either mode.

## Output

Each saved article is written as `domain__slug__hash.txt` in the output directory,
containing the extracted (and optionally lemmatized) article text.

A `scraped_urls.txt` manifest is also kept there, one line per article:

```
https://example.com/news/some-slug	<content-hash>
```

This powers two dedup checks on every run:
- **URL dedup** — same URL won't be re-scraped.
- **Content dedup** — the same story republished under a *different* URL/slug
  is detected by hashing the extracted text, and skipped.

Old manifests from before content-hash dedup existed (URL-only, no second
column) still load fine.

## How article discovery works (auto mode)

There's no per-site config. Links found on a listing page are kept as
candidate articles if they:
- stay on the same domain,
- aren't an obvious non-article path (`/tag/`, `/video/`, `/author/`, etc.),
- aren't a static asset (image, PDF, video, etc.), and
- have either a hyphen-rich slug (`biden-signs-new-bill`) or a long numeric ID
  in the path — both common across most news CMSs.

This is a heuristic, not a guarantee. If it misses articles or grabs junk on a
particular site, fall back to **Mode 2** with direct article URLs.

## How text extraction works

`trafilatura` pulls the main article body out of the page HTML, automatically
discarding nav bars, ads, cookie banners, and related-story widgets — no
manual per-site rules required. If it can't parse a page, the script falls
back to concatenating `<p>` tags directly.

## Notes

- Pages are rendered with headless Chromium (via Playwright) before
  extraction, so JS-heavy sites work too.
- Failed URLs get one automatic retry pass at the end of each run.
- `--lemmatize` loads its model once up front — a bad language code fails
  immediately with a clear error instead of failing silently on every article.
