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

    # --- English (BBC) ---
    # Newsletter sign-up CTA, confirmed as the final paragraph across 3
    # real scraped articles (a wildfire-insurance piece, a J&J settlement
    # piece, a US-munitions InDepth piece) -- and NOT generalizable by
    # _generalize_group()'s prefix/suffix diffing (checked directly: it
    # correctly returns None on these 3, since they share no meaningful
    # common prefix or suffix -- "Sign up for our Tech Decoded
    # newsletter...", "Get our flagship newsletter...", and "BBC InDepth
    # is the home... Sign up for the newsletter here" don't even start or
    # end the same way). This is a real gap in that tool: it generalizes
    # "same wrapper, different fill" (the Bob Howard byline case), not
    # "same semantic shape, entirely different wording" -- worth a second
    # look if this shape keeps recurring on other CMSs. Hand-written
    # here instead as an unordered "contains all three" check (word order
    # differs across the 3 examples -- sometimes "sign up" precedes
    # "newsletter", sometimes it follows), which is looser than the
    # prefix/suffix style used elsewhere in this file and carries a real
    # (believed low) false-positive risk on any single paragraph that
    # happens to mention a newsletter, signing up, AND the word "here" --
    # e.g. an article that's actually ABOUT a newsletter lawsuit. Revisit
    # if that ever shows up in the boilerplate_candidates.json review.
    r"(?=.*\bnewsletter\b)(?=.*\bsign up\b)(?=.*\bhere\b)",
    # Regional cross-promo footer, confirmed on a real scraped BBC Isle
    # of Man article ("Read more stories from the Isle of Man on the
    # BBC, watch BBC North West Tonight on BBC iPlayer and follow BBC
    # Isle of Man on Facebook and X."). Single example so far -- kept
    # literal rather than generalized to a "Read more stories from X...
    # BBC iPlayer... Facebook and X" template, same single-example
    # discipline as everywhere else in this file. Generalize once a
    # second regional BBC franchise's footer is seen.
    r"^read more stories from the isle of man on the bbc, watch bbc north west tonight on bbc iplayer and follow bbc isle of man on facebook and x\.?$",
    # Mid-article podcast-promo insert, confirmed on a real scraped BBC
    # article about the D4vd murder trial -- unlike the other BBC
    # patterns above, this one was NOT at the end of the article, it was
    # spliced into the middle of the body text ("Listen to the BBC's Fame
    # Under Fire podcast which brings you the latest from inside the Los
    # Angeles courtroom at the D4vd preliminary hearing."). Single
    # example, kept literal for the same reason as the footer above --
    # generalize to "Listen to the BBC's X podcast which..." once a
    # second example turns up.
    r"^listen to the bbc's fame under fire podcast which brings you the latest from inside the los angeles courtroom at the d4vd preliminary hearing\.?$",
    # Auto-suggested from boilerplate_candidates.json review (2026-07-30) -- bbc.com, promotional blurb, 1 example(s).
    # e.g. https://www.bbc.com/news/articles/clye651yzdjo
    "^Watch\\ the\\ full\\ interview,\\ BBC\\ Panorama\\ 'Andy\\ Burnham:\\ the\\ Laura\\ Kuenssberg\\ Interview'\\ on\\ iPlayer\\ from\\ 06:00\\ on\\ Monday\\ and\\ BBC\\ One\\ on\\ Monday\\ at\\ 20:00\\ BST\\.$",
    # Auto-suggested from boilerplate_candidates.json review (2026-07-30) -- bbc.com, reporter credit, 1 example(s).
    # e.g. https://www.bbc.com/news/articles/c33y3151vydo
    '^Additional\\ reporting\\ by\\ Bob\\ Howard\\.$',
    # Auto-suggested from boilerplate_candidates.json review (2026-07-30) -- bbc.com, reader engagement prompt, 1 example(s).
    # e.g. https://www.bbc.com/news/articles/cm2rgkp6z7no
    '^Do\\ you\\ have\\ a\\ story\\ suggestion\\ for\\ Essex\\?\\ Contact\\ us\\ below\\.$',
    # Auto-suggested from boilerplate_candidates.json review (2026-07-30) -- bbc.com, social-follow prompt, 1 example(s).
    # e.g. https://www.bbc.com/news/articles/cm2rgkp6z7no
    '^Follow\\ Essex\\ news\\ on\\ BBC\\ Sounds,\\ Facebook,\\ Instagram\\ and\\ X\\.$',
    # Auto-suggested from boilerplate_candidates.json review (2026-07-30) -- bbc.com, content warning, 1 example(s).
    # e.g. https://www.bbc.com/news/articles/c78gjyx4q2yo
    '^Contains\\ upsetting\\ scenes\\.$',
    # Auto-suggested from boilerplate_candidates.json review (2026-07-30) -- bbc.com, omission placeholder, 1 example(s).
    # e.g. https://www.bbc.com/news/articles/c872nj1n4xyo
    '^\\[\\.\\.\\.\\ article\\ middle\\ omitted\\ \\.\\.\\.\\]$',
    # Auto-suggested from boilerplate_candidates.json review (2026-07-30) -- bbc.com, site chrome / video player UI text, 1 example(s).
    # e.g. https://www.bbc.com/news/articles/cwymnvkv2zlo
    '^Automatically\\ selects\\ the\\ best\\ quality\\ available$',
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