# News Scraper

A general-purpose article scraper that works against most news sites out of
the box, with an optional per-site tuning layer for sites you scrape a lot
(currently RÚV and BBC/BBC Cymru Fyw, evidence-backed — see **Site overrides**
below). Two modes: auto-discover articles from a listing page, or scrape
specific article URLs directly. Saved articles are automatically sorted into
per-language subfolders by a local, offline 4-judge language-ID panel.

## Install

```bash
pip install playwright trafilatura lingua-language-detector tqdm
playwright install chromium
```

Optional, only if you want lemmatized output:

```bash
pip install stanza
```

Optional, only if you use `--detect-boilerplate` or `--detect-language-llm`:

```bash
pip install google-genai
export GEMINI_API_KEY=...   # free key, no credit card required: https://aistudio.google.com/apikey
# or, to rotate across multiple free-tier keys when one hits its rate limit:
export GEMINI_API_KEYS="key1,key2,key3"
```

Optional, only for the local language-ID panel's non-`lingua` judges (the
scraper still runs fine without these — the panel just falls back to
whichever judges *are* available, see **Language detection** below):

```bash
pip install fasttext huggingface_hub gcld3
```

`fasttext` + `huggingface_hub` are needed for the GlotLID and OpenLID-v3
judges (each downloads a ~1–2GB model from HuggingFace the first time it's
used — needs internet access for that one-time download). `gcld3` is needed
for the CLD3 judge and needs a C++ compiler and the protobuf compiler
available at install time.

## Usage

**No arguments — interactive menu:**

```bash
python icelandic_text_extractor.py
```

Walks you through: output directory → mode (auto-discover, direct URLs,
review/clear/delete) → URLs → lemmatize y/n (and language code, if yes) →
LLM boilerplate detection y/n → Gemini language-ID tiebreak y/n → runs the
scrape with a live progress bar, then asks if you want to go again. Type `q`
at any step to go back one step (or to quit, at the very first step).

**With arguments — same as before, for scripting/automation** (skips the
menu entirely):

**Mode `auto`:** point it at a listing/section page (homepage, `/world`,
`/sport`, etc.) and it discovers and scrapes article links on its own.

```bash
python icelandic_text_extractor.py auto https://example.com/news https://example.com/sport
```

**Mode `url`:** scrape specific article URLs directly, no discovery step.

```bash
python icelandic_text_extractor.py url https://example.com/news/some-headline-slug
```

**Mode `review`:** interactively walk through `boilerplate_candidates.json`
(logged by `--detect-boilerplate`), see a suggested regex for each new
fragment (auto-generalized once there are 2+ examples), and approve/skip
each one — approved patterns are written straight into
`boilerplate_patterns.py`.

```bash
python icelandic_text_extractor.py review --output-dir ./news_corpus
```

**Mode `clear`:** permanently deletes every saved article `.txt` file (raw +
lemmatized, all language subfolders) under the given output dir and resets
`scraped_urls.txt`. Confirms first unless `--yes` is given.

```bash
python icelandic_text_extractor.py clear --output-dir ./news_corpus
```

**Mode `delete`:** permanently deletes `boilerplate_candidates.json` for the
given output dir (the LLM review-queue log — not `boilerplate_patterns.py`
itself). Confirms first unless `--yes` is given.

```bash
python icelandic_text_extractor.py delete --output-dir ./news_corpus
```

## Options (CLI mode)

| Flag | Default | Applies to | Description |
|---|---|---|---|
| `--output-dir DIR` | `./news_corpus` | all modes | Where `.txt` files and the manifest are saved |
| `--lemmatize LANG` | off | `auto`, `url` | Stanza language code (e.g. `en`, `cy`, `is`) — lemmatizes text before saving |
| `--delay SECONDS` | `1.5` | `auto`, `url` | Pause between requests (+ random jitter), for politeness / rate-limit avoidance |
| `--detect-boilerplate` | off | `auto`, `url` | Use Gemini's free tier to flag possible leftover boilerplate for review. Only suspicious-looking articles are checked, sent in batches — see **Boilerplate detection** below. Candidates are logged, never auto-applied. Requires `GEMINI_API_KEY`/`GEMINI_API_KEYS` |
| `--detect-language-llm` | off | `auto`, `url` | Let Gemini break ties when the local 4-judge language panel genuinely disagrees on an article. Only called for the disputed subset, not every article — see **Language detection** below |
| `--yes` | off | `clear`, `delete` | Skip the confirmation prompt |

The interactive menu currently always uses the default delay (1.5s); pass
`--delay` via the CLI form if you need to change it.

## Output

Each saved article is written as `domain__slug__hash.txt`, inside a
**per-language subfolder** of the output directory, further split into
**`raw/`** and **`lemmatized/`** — e.g.
`news_corpus/is/raw/ruv.is__some-slug__a1b2c3d4.txt`.

The raw extracted text is **always** saved to `raw/`. If `--lemmatize` is
given, a lemmatized copy is **additionally** saved to `lemmatized/`, with
`__lemma` appended to the filename before the extension (same
domain/slug/hash prefix as its raw counterpart, so the two are easy to spot
as a matched pair) — lemmatizing never replaces or discards the raw text.
This also means re-running the same URL later with a different `--lemmatize`
setting doesn't lose anything: both a raw-only scrape and a lemmatized scrape
of the same article can coexist, since dedup (below) triggers off a single
extraction either way rather than off which variant was saved.

Language is detected automatically from each article's own extracted text
(via a local 4-judge panel — see **Language detection** below), not guessed
from the source site, so a mixed-language listing page sorts itself
correctly on its own. Text lands in one of three places:
- a real language folder (`is/`, `en/`, etc.) when the panel reaches consensus,
- `unknown/` when no judge had anything usable to say at all,
- `disputed/` when judges genuinely disagreed (or only one could vote) even
  after an optional Gemini tiebreak — see `language_disputes.json`.

A `scraped_urls.txt` manifest is kept at the **top level** of the output
directory (shared across all language subfolders, since dedup is
corpus-wide), one line per article:

```
https://example.com/news/some-slug	<content-hash>
```

This powers two dedup checks on every run:
- **URL dedup** — same URL won't be re-scraped.
- **Content dedup** — the same story republished under a *different* URL/slug
  is detected by hashing the extracted text, and skipped.

Old manifests from before content-hash dedup existed (URL-only, no second
column) still load fine. The manifest is also reconciled against what's
actually on disk at the start of every run — if you delete a saved article
file by hand, its manifest entry is pruned automatically rather than
silently blocking a re-scrape forever.

## How article discovery works (auto mode)

Links found on a listing page are kept as candidate articles if they:
- stay on the same domain,
- aren't an obvious non-article path (`/tag/`, `/video/`, `/author/`, etc.),
- aren't a static asset (image, PDF, video, etc.), and
- **either** match a site-specific `listing_path_prefixes` override (see
  below) **or**, generically, have a hyphen-rich slug
  (`biden-signs-new-bill`) or a long numeric ID in the path.

The generic heuristic covers most news CMSs, but it's not a guarantee — some
sites use opaque IDs it can't recognize at all (BBC's `/news/articles/<id>`
has neither hyphens nor a long digit run; without the override below, `auto`
mode would silently discover zero BBC articles from any listing page). If
discovery misses articles or grabs junk on a new site, either add an override
(see below) or fall back to **Mode `url`** with direct article URLs.

## How text extraction works

For a domain with a **site override** (below), a confirmed CSS selector is
tried first. Otherwise, `trafilatura` pulls the main article body out of the
page HTML automatically, discarding nav bars, ads, and related-story widgets
with no manual per-site rules required. If that returns nothing usable, the
script falls back to scanning known article-container elements
(`article`, `main`, `.article-body`, etc.) directly, and finally to raw `<p>`
tag concatenation as a last resort.

At every stage, extracted paragraphs are run through a boilerplate filter
before being saved — this catches things that render as ordinary `<p>` tags
*inside* the article body but aren't actually article prose: footer contact
blocks (phone numbers, addresses), copyright lines, cookie-consent
placeholders shown in place of a blocked embed, and "read more" teaser
duplicates. This list grows as new patterns turn up — see
`boilerplate_patterns.py` (`BOILERPLATE_PATTERNS`, `END_OF_ARTICLE_MARKERS`,
`READ_MORE_SUFFIXES`).

### Site overrides

`SITE_OVERRIDES` in the script holds evidence-backed extraction rules for
specific domains — built using the included `inspect_selectors.py` tool (see
below), never guessed. Each entry can supply a proven `article_selector`,
`exclude_selectors` for known junk inside it, and/or `listing_path_prefixes`
to fix article discovery on sites the generic heuristic can't handle. An
override that matches nothing on a given page (a template change, or a page
type it wasn't built against — e.g. a live-blog vs. a standard article) falls
through cleanly to the generic path rather than breaking the scrape.

Currently covered: **ruv.is**, **bbc.com** (which also serves BBC Cymru Fyw
under `/cymrufyw/`). See the comments above each entry in the script for
exactly what was tested and observed.

**Adding a new site:** run `inspect_selectors.py` against a real article URL
on that site — it prints every plausible container selector ranked by how
much real paragraph text it holds, plus anything suspicious found nested
inside the best match. Paste the output into a `SITE_OVERRIDES` entry once
you've confirmed which selector is actually the article body.

```bash
python inspect_selectors.py https://example.com/some/real/article
```

Worth re-running on a couple of different article types per site (a live
sports blog and an ordinary bylined story extract very differently on RÚV,
for instance) before fully trusting a new override.

## Boilerplate detection (optional, LLM-assisted)

`--detect-boilerplate` adds an LLM pass (Gemini, free tier) over already-
saved, already-filtered article text, looking for leftover site chrome that
`boilerplate_patterns.py` didn't catch — the same kind of thing you'd find by
eye and add to that file by hand. It never changes what gets saved; it only
logs candidates to `boilerplate_candidates.json` for review.

To keep this within the free tier's tight per-minute request cap:
- **Suspicion filter** — most normal-length, well-formed articles skip the
  LLM pass entirely. Short articles (under ~80 words), articles with a
  short/list-shaped first or last paragraph, or articles that are mostly
  short paragraphs throughout are the ones actually checked.
- **Batching** — suspicious articles queue up and are sent as one combined
  request per batch of 15, instead of one request per article.

Run `python icelandic_text_extractor.py review --output-dir ./news_corpus`
afterward to interactively approve or skip each logged candidate — approved
patterns are written straight into `boilerplate_patterns.py`.

## Language detection

Each article's raw extracted text (before any `--lemmatize` step) is run
through a panel of four **local, offline** judges — no API calls, no rate
limit — to decide its output folder:

- **GlotLID** — a fastText model covering 2000+ languages, including ones
  well outside a typical closed-list detector's range (e.g. Kalaallisut/
  Greenlandic).
- **OpenLID-v3** — a second fastText model, reported to have better
  precision/false-positive rate than GlotLID; meant to pair with it.
- **CLD3** — Google's neural language identifier, a genuinely different
  architecture from the two fastText models, so its errors tend not to
  correlate with theirs. Ships its own reliability flag, used as its abstain
  gate.
- **lingua** — the detector this project used to rely on alone. Solid within
  its closed candidate list (currently Icelandic, English, Welsh, Norwegian
  Bokmål/Nynorsk, Swedish, Danish, German, French, Spanish, Dutch, Irish) —
  it was chosen over `langdetect` after `langdetect` consistently
  misidentified real Icelandic article text as Norwegian or Swedish in
  testing.

Each judge's vote is weighted by (its own base reliability) × (its own
confidence on this specific text). A language "wins" only if it holds at
least 55% of the total weighted vote **and** at least 2 judges actually
voted for it — a confident-but-lone judge doesn't count as consensus. If no
judge has anything to say, the article goes to `unknown/`. If the panel
genuinely disagrees, the article goes to `disputed/` (see
`language_disputes.json` for the full per-judge breakdown), unless
`--detect-language-llm` is on, in which case Gemini gets one tiebreak vote
for that article only — not every article, which is what keeps this optional
feature within the free tier's rate limit.

If you start scraping a new language and articles keep landing in
`unknown/`, check whether it's in `_LINGUA_LANGUAGES` in the script (for the
`lingua` judge) — GlotLID and OpenLID-v3 already cover a much broader set on
their own. Spot-check a real sample afterward rather than trusting an
addition blindly (see the Icelandic/Norwegian mix-up above for why).

## Notes

- Pages are rendered with headless Chromium (via Playwright) before
  extraction, so JS-heavy sites work too. The script waits for a real
  paragraph *count* (not just one `<p>`) plus a short network-idle grace
  period before reading the page, to avoid snapshotting a bylined article
  template mid-hydration and capturing only its header/teaser block.
- Failed URLs get one automatic retry pass at the end of each run.
- `--lemmatize` loads its model once up front — a bad language code fails
  immediately with a clear error instead of failing silently on every
  article.
- Progress is shown with a live `tqdm` bar (✅/💥 counts in the postfix);
  per-article status lines print above it without corrupting the bar.
- `GEMINI_API_KEYS` (comma- or newline-separated) rotates across multiple
  free-tier keys when one hits its limit. Worth reading Google's terms of
  service on this before relying on it — quota is enforced per Google Cloud
  *project*, not per key, so this only helps if the keys are on genuinely
  separate projects, and using multiple free-tier keys/projects specifically
  to route around a rate limit is generally understood to be against the
  free tier's terms.

## Companion tool: `inspect_selectors.py`

Standalone, not part of the main pipeline. Loads one real article URL and
prints every plausible article-container selector ranked by paragraph text
held, plus anything suspicious nested inside the best match (share widgets,
accessibility controls, etc.). Used to build evidence-backed
`SITE_OVERRIDES` entries — see **Site overrides** above.

```bash
python inspect_selectors.py <article_url>
```