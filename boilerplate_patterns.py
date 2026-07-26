"""
Boilerplate / junk-text tables for the news scraper.
=====================================================
Pure data, no functions -- mirrors the mutation_tables.py split on the
Welsh project (data lives here, matching/filtering logic stays in the
main script, which imports these). Kept separate specifically because
this list is expected to keep growing as new sites get scraped; adding a
new pattern should never require touching extraction logic.

Three tables, three different jobs:

  BOILERPLATE_PATTERNS   -- fixed junk text to drop wherever it appears
                             (cookie banners, consent placeholders, promo
                             blocks, editorial-policy footers, phone/
                             address patterns, etc). Each entry is a
                             regex tried against a single paragraph.

  END_OF_ARTICLE_MARKERS -- stronger than a boilerplate pattern: once one
                             of these matches a paragraph, EVERYTHING
                             from that paragraph to the end of the
                             extracted text is dropped, not just the
                             matching paragraph itself. Needed for
                             footers that are followed by dynamic
                             related-content widgets (different headlines
                             every time, so there's no fixed string for
                             BOILERPLATE_PATTERNS to catch) -- see
                             SVT.se's "Så arbetar vi" footer.

  READ_MORE_SUFFIXES     -- "read more" / expand-widget trailing words.
                             Some CMS teaser widgets keep both the
                             truncated teaser (ending in one of these)
                             and the full text in the DOM at once; used
                             by dedupe_consecutive_paragraphs() in the
                             main script to collapse the pair into just
                             the full version.

Every entry below is evidence-backed from a real scraped sample, not
guessed -- see the comment above each one for what was actually observed
and which file it came from.
"""

# --------------------------------------------------------------------------
# BOILERPLATE_PATTERNS -- drop the matching paragraph, wherever it appears
# --------------------------------------------------------------------------
BOILERPLATE_PATTERNS = [
    # --- Icelandic (RUV) ---
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

    # --- Dutch (NOS.nl) ---
    # Promo block that sits at the end of almost every article (confirmed
    # across many real scrapes). Appears as one or two <p> tags inside/
    # after the body, so container-scoping alone does not remove it.
    r"maak nos jouw voorkeursbron in google",
    r"met een nieuwe functie van google bepaal je voortaan zelf",
    r"^meer bekijken\??$",
    # "Opens in new window" accessibility label attached to inline links --
    # extracted as its own paragraph, which splits the sentence it was
    # embedded in right down the middle (confirmed: "...criminaliteit /
    # (opent in nieuw venster) / onder Palestijnse Israëliërs..." was one
    # continuous sentence in the source, broken into three paragraphs by
    # this leaking through). Not real content in any case.
    r"^\(opent in nieuw venster\)$",

    # --- Danish (DR.dk) ---
    # Cookie-consent banner. Confirmed on a real scrape where this was the
    # ENTIRE extracted "article" (no actual story text at all) -- not just
    # noise near real content, a full extraction failure on its own. This
    # doesn't fix WHY DR is returning the banner/homepage river instead of
    # the article (that needs a SITE_OVERRIDES entry, still pending
    # inspect_selectors.py output) -- it just stops the banner text from
    # being saved as if it were prose.
    r"^dr passer p[åa] dine data$",
    r"dr indsamler oplysninger om dine bes[øo]g ved hj[æa]lp af cookies",
    r"dr bruger egne cookies og cookies fra tredjepart",

    # --- Swedish (SVT.se) ---
    # Editorial-policy footer ("This is how we work"), confirmed verbatim
    # and identical across every single SVT.se sample scraped so far -- a
    # fixed site-wide footer, not article content. Belt-and-suspenders
    # pattern on the body sentence itself; the header line ("Så arbetar
    # vi") is also handled as an END_OF_ARTICLE marker below, since on
    # some articles a dynamic "related stories" widget follows right
    # after this footer and needs to be dropped too, not just this one
    # paragraph.
    r"svt:s nyheter ska st[åa] f[öo]r saklighet och opartiskhet",
]

# --------------------------------------------------------------------------
# END_OF_ARTICLE_MARKERS -- drop this paragraph AND everything after it
# --------------------------------------------------------------------------
# Confirmed on SVT.se: the "Så arbetar vi" editorial footer is sometimes
# followed by a "related stories" widget -- a topic tag plus bullet lines
# that glue a "Just nu" breaking-news badge directly onto the next
# headline with no space ("Just nuTiotusentals flyr...") -- clearly UI
# chrome, but each headline is different text every time, so there's no
# fixed string for BOILERPLATE_PATTERNS to catch. Truncating at the
# marker instead of pattern-matching the noise handles this regardless of
# what the related-content widget happens to say.
END_OF_ARTICLE_MARKERS = [
    r"^så arbetar vi$",  # SVT.se
]

# --------------------------------------------------------------------------
# READ_MORE_SUFFIXES -- "read more" widget trailing words, by language
# --------------------------------------------------------------------------
# Some CMS teaser widgets (confirmed on DR.dk, using "vis mere") keep BOTH
# the truncated teaser (ending in this word) and the full text in the DOM
# at once -- CSS toggles which one shows, but both get scraped, producing
# the same paragraph twice in a row, once truncated once full.
READ_MORE_SUFFIXES = [
    "vis mere",   # Danish (DR.dk, confirmed)
    "vis mer",    # Norwegian
    "visa mer",   # Swedish
    "lees meer",  # Dutch
]