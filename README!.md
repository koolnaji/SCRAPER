\# News Scraper



A general-purpose article scraper that works against most news sites out of

the box, with an optional per-site tuning layer for sites you scrape a lot

(currently RÚV and BBC/BBC Cymru Fyw, evidence-backed — see \*\*Site overrides\*\*

below). Two modes: auto-discover articles from a listing page, or scrape

specific article URLs directly. Saved articles are automatically sorted into

per-language subfolders.



\## Install



```bash

pip install playwright trafilatura lingua-language-detector tqdm

playwright install chromium

```



Optional, only if you want lemmatized output:



```bash

pip install stanza

```



\## Usage



\*\*No arguments — interactive menu:\*\*



```bash

python icelandic\_text\_extractor.py

```



Walks you through: mode (auto-discover vs. direct URLs) → paste URLs

(comma-separated or one per line) → lemmatize y/n (and language code, if

yes) → output directory (Enter for default) → then runs the scrape with a

live progress bar.



\*\*With arguments — same as before, for scripting/automation\*\* (skips the

menu entirely):



\*\*Mode 1 — auto:\*\* point it at a listing/section page (homepage, `/world`,

`/sport`, etc.) and it discovers and scrapes article links on its own.



```bash

python icelandic\_text\_extractor.py auto https://example.com/news https://example.com/sport

```



\*\*Mode 2 — url:\*\* scrape specific article URLs directly, no discovery step.



```bash

python icelandic\_text\_extractor.py url https://example.com/news/some-headline-slug

```



\## Options (CLI mode)



| Flag | Default | Description |

|---|---|---|

| `--output-dir DIR` | `./news\_corpus` | Where `.txt` files and the manifest are saved |

| `--lemmatize LANG` | off | Stanza language code (e.g. `en`, `cy`, `is`) — lemmatizes text before saving |

| `--delay SECONDS` | `1.5` | Pause between requests (+ random jitter), for politeness / rate-limit avoidance |



Both flags apply to either mode. The interactive menu currently always uses

the default delay (1.5s); pass `--delay` via the CLI form if you need to

change it.



\## Output



Each saved article is written as `domain\_\_slug\_\_hash.txt`, inside a

\*\*per-language subfolder\*\* of the output directory, further split into

\*\*`raw/`\*\* and \*\*`lemmatized/`\*\* — e.g.

`news\_corpus/is/raw/ruv.is\_\_some-slug\_\_a1b2c3d4.txt`.



The raw extracted text is \*\*always\*\* saved to `raw/`. If `--lemmatize` is

given, a lemmatized copy is \*\*additionally\*\* saved to `lemmatized/`, with

`\_\_lemma` appended to the filename before the extension (same

domain/slug/hash prefix as its raw counterpart, so the two are easy to spot

as a matched pair) — lemmatizing never replaces or discards the raw text.

This also means re-running the same URL later with a different `--lemmatize`

setting doesn't lose anything: both a raw-only scrape and a lemmatized scrape

of the same article can coexist, since dedup (below) triggers off a single

extraction either way rather than off which variant was saved.



Language is detected automatically from each article's own extracted text

(via `lingua` — see \*\*Language detection\*\* below), not guessed from the

source site, so a mixed-language listing page sorts itself correctly on its

own. Text that can't be confidently classified lands in `news\_corpus/unknown/`

rather than being dropped.



A `scraped\_urls.txt` manifest is kept at the \*\*top level\*\* of the output

directory (shared across all language subfolders, since dedup is

corpus-wide), one line per article:



```

https://example.com/news/some-slug	<content-hash>

```



This powers two dedup checks on every run:

\- \*\*URL dedup\*\* — same URL won't be re-scraped.

\- \*\*Content dedup\*\* — the same story republished under a \*different\* URL/slug

&#x20; is detected by hashing the extracted text, and skipped.



Old manifests from before content-hash dedup existed (URL-only, no second

column) still load fine.



\## How article discovery works (auto mode)



Links found on a listing page are kept as candidate articles if they:

\- stay on the same domain,

\- aren't an obvious non-article path (`/tag/`, `/video/`, `/author/`, etc.),

\- aren't a static asset (image, PDF, video, etc.), and

\- \*\*either\*\* match a site-specific `listing\_path\_prefixes` override (see

&#x20; below) \*\*or\*\*, generically, have a hyphen-rich slug

&#x20; (`biden-signs-new-bill`) or a long numeric ID in the path.



The generic heuristic covers most news CMSs, but it's not a guarantee — some

sites use opaque IDs it can't recognize at all (BBC's `/news/articles/<id>`

has neither hyphens nor a long digit run; without the override below, `auto`

mode would silently discover zero BBC articles from any listing page). If

discovery misses articles or grabs junk on a new site, either add an override

(see below) or fall back to \*\*Mode 2\*\* with direct article URLs.



\## How text extraction works



For a domain with a \*\*site override\*\* (below), a confirmed CSS selector is

tried first. Otherwise, `trafilatura` pulls the main article body out of the

page HTML automatically, discarding nav bars, ads, and related-story widgets

with no manual per-site rules required. If that returns nothing usable, the

script falls back to scanning known article-container elements

(`article`, `main`, `.article-body`, etc.) directly, and finally to raw `<p>`

tag concatenation as a last resort.



At every stage, extracted paragraphs are run through a boilerplate filter

before being saved — this catches things that render as ordinary `<p>` tags

\*inside\* the article body but aren't actually article prose: footer contact

blocks (phone numbers, addresses), copyright lines, and cookie-consent

placeholders shown in place of a blocked embed (confirmed on a real RÚV

article, where one such placeholder appeared three times verbatim in a

single piece). This list grows as new patterns turn up — see

`BOILERPLATE\_PATTERNS` in the script.



\### Site overrides



`SITE\_OVERRIDES` in the script holds evidence-backed extraction rules for

specific domains — built using the included `inspect\_selectors.py` tool (see

below), never guessed. Each entry can supply a proven `article\_selector`,

`exclude\_selectors` for known junk inside it, and/or `listing\_path\_prefixes`

to fix article discovery on sites the generic heuristic can't handle. An

override that matches nothing on a given page (a template change, or a page

type it wasn't built against — e.g. a live-blog vs. a standard article) falls

through cleanly to the generic path rather than breaking the scrape.



Currently covered: \*\*ruv.is\*\*, \*\*bbc.com\*\* (which also serves BBC Cymru Fyw

under `/cymrufyw/`). See the comments above each entry in the script for

exactly what was tested and observed.



\*\*Adding a new site:\*\* run `inspect\_selectors.py` against a real article URL

on that site — it prints every plausible container selector ranked by how

much real paragraph text it holds, plus anything suspicious found nested

inside the best match. Paste the output into a `SITE\_OVERRIDES` entry once

you've confirmed which selector is actually the article body.



```bash

python inspect\_selectors.py https://example.com/some/real/article

```



Worth re-running on a couple of different article types per site (a live

sports blog and an ordinary bylined story extract very differently on RÚV,

for instance) before fully trusting a new override.



\## Language detection



Each article's raw extracted text (before any `--lemmatize` step) is run

through `lingua` to pick its output subfolder. `lingua` was chosen over the

more commonly-reached-for `langdetect` after `langdetect` consistently

misidentified real Icelandic article text as Norwegian or Swedish in

testing — `lingua` got the same samples right, including short ones.



The candidate language list is a closed set (currently Icelandic, English,

Welsh, Norwegian (Bokmål/Nynorsk), Swedish, Danish, German, French, Spanish)

— if you start scraping a new language and articles keep landing in

`unknown/`, add it to `\_LINGUA\_LANGUAGES` in the script, and spot-check a

real sample afterward rather than trusting the addition blindly (see the

Icelandic/Norwegian mix-up above for why).



\## Notes



\- Pages are rendered with headless Chromium (via Playwright) before

&#x20; extraction, so JS-heavy sites work too. The script waits for a real

&#x20; paragraph \*count\* (not just one `<p>`) plus a short network-idle grace

&#x20; period before reading the page, to avoid snapshotting a bylined article

&#x20; template mid-hydration and capturing only its header/teaser block.

\- Failed URLs get one automatic retry pass at the end of each run.

\- `--lemmatize` loads its model once up front — a bad language code fails

&#x20; immediately with a clear error instead of failing silently on every

&#x20; article.

\- Progress is shown with a live `tqdm` bar (✅/💥 counts in the postfix);

&#x20; per-article status lines print above it without corrupting the bar.



\## Companion tool: `inspect\_selectors.py`



Standalone, not part of the main pipeline. Loads one real article URL and

prints every plausible article-container selector ranked by paragraph text

held, plus anything suspicious nested inside the best match (share widgets,

accessibility controls, etc.). Used to build evidence-backed

`SITE\_OVERRIDES` entries — see \*\*Site overrides\*\* above.



```bash

python inspect\_selectors.py <article\_url>

```

