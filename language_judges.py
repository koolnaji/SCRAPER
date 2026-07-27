"""
language_judges.py
===================
Replaces the old two-voice (lingua local / Gemini API) language ID system
with a weighted panel of LOCAL, offline judges -- no API calls, no RPM
ceiling, no key rotation to think about. Gemini is kept, but demoted to
a rare tiebreaker: it's only ever called for articles the local panel
itself can't resolve, which is the actual fix for the RPM-pressure
problem raised earlier, not just a workaround for it.

Why four judges instead of one
-------------------------------
GlotLID (cis-lmu/glotlid on HuggingFace) is the anchor: a fastText model
covering 2000+ ISO 639-3 languages, including everything on this
project's radar (Icelandic, Dutch, Danish, Swedish, Norwegian, Welsh,
Irish, AND Kalaallisut/Greenlandic -- none of which lingua's closed
candidate list could represent, and one of which, Greenlandic, lingua's
underlying library doesn't support at ANY size list). It's the reason
this module exists at all.

But one judge, however broad its coverage, is still one opinion. The
other three each cover a real, different failure mode GlotLID alone
doesn't:

  - OpenLID-v3 (HPLT/OpenLID-v3) -- GlotLID's own paper reports it wins
    on recall but OpenLID wins on PRECISION and false-positive rate, and
    recommends ensembling the two. Where GlotLID casts the widest net,
    OpenLID is the one likelier to catch a GlotLID false positive.
  - CLD3 (Google, via gcld3) -- a genuinely different model architecture
    (neural net, not fastText n-grams) from the other two, so its errors
    are less likely to be CORRELATED with GlotLID/OpenLID's. Verified by
    hand: correctly identifies nl/da/sv/cy/ga with high confidence, and
    -- crucially -- on Greenlandic text it doesn't just get the answer
    wrong, it flags is_reliable=False on its (wrong) guess. That
    reliability flag is used directly as this judge's abstain gate below,
    not just an FYI field.
  - lingua -- already in this codebase, already proven (see
    icelandic_text_extractor.py's own comment on the langdetect
    Icelandic/Norwegian mix-up) to be solid WITHIN its closed candidate
    list. Kept as a fourth, cheap opinion; extended here to include
    Dutch and Irish (both are in lingua's underlying 75-language set,
    just weren't in this project's 10-language build list before).

None of the four is trusted alone. See judge_language()'s docstring for
how the panel's votes turn into a decision vs. a dispute.
"""

import os
import re
import json
from collections import namedtuple
from datetime import datetime, timezone
from urllib.parse import urlparse

# --------------------------------------------------------------------------
# Judge weights and abstain thresholds
# --------------------------------------------------------------------------
# Base weight reflects how much this judge's vote should count RELATIVE
# TO THE OTHERS, not an absolute confidence -- the actual per-article
# vote strength is base_weight * this_judge's_own_confidence_on_THIS_text,
# so a normally-strong judge that's unsure on a given article still
# contributes less than a normally-weaker judge that's very sure.
JUDGE_WEIGHTS = {
    "glotlid": 1.0,   # broadest coverage, best recall per its own paper
    "openlid": 1.0,   # best precision/FPR -- meant to pair with glotlid
    "cld3":    0.8,   # different architecture = independent error profile,
                       # but narrower (~107 language) coverage than the
                       # two fastText models above
    "lingua":  0.6,   # cheapest/oldest signal here, closed candidate
                       # list, documented history of confusing close
                       # Nordic-language pairs -- kept for corroboration,
                       # not as a primary voice
    "gemini":  0.9,   # tiebreak-only (see reconcile_gemini_tiebreak
                       # below) -- a strong general model but not a
                       # calibrated LID specialist, so it sits between
                       # cld3 and the two dedicated fastText judges
}

# fastText's own `threshold` predict() argument -- below this, predict()
# returns nothing at all for that call, which this module treats as an
# abstain rather than forcing a low-confidence guess into the vote.
# OpenLID's own model card usage example already threshold=0.5s its
# calls; GlotLID gets a slightly lower floor since its 2000+-language
# coverage means peak confidence is naturally more spread out even on
# genuinely correct calls (fastText fully wrong-language guesses do
# still tend to sit well below either floor in the fastText paper's own
# reporting, so this isn't just "less thorough", it's calibrated to how
# each model's confidence distribution actually looks).
GLOTLID_THRESHOLD = 0.3
OPENLID_THRESHOLD = 0.5
# CLD3 ships its OWN calibrated reliability flag (is_reliable) -- verified
# by hand to correctly come back False on a Greenlandic sample it
# (wrongly) guessed as Somali. Used as the primary gate; this probability
# floor is just a belt-and-suspenders backstop.
CLD3_PROBABILITY_FLOOR = 0.5

# --------------------------------------------------------------------------
# Ensemble resolution thresholds
# --------------------------------------------------------------------------
# A winner needs a clear lean, not just a technical plurality -- 0.55
# means the winning code has to hold more than half the total weight
# among judges who actually voted, not just more than any single
# runner-up. This is deliberately independent of HOW the weight got
# there: a landslide among 2 judges and a narrow win among 4 both have
# to clear the same bar. Weighting decides WHO's ahead; this threshold
# decides whether "ahead" is convincing enough to act on. A weighted
# vote with a weak margin is still a real disagreement underneath --
# see the module docstring's opening paragraph on why this project
# wanted that distinction preserved rather than papered over.
CONSENSUS_SHARE_THRESHOLD = 0.55

# However confident a single judge is, one opinion is not corroboration.
# This is the paranoia knob: raising it demands agreement from more
# independent judges before anything gets auto-filed; MIN_CORROBORATING
# _JUDGES = 1 would let GlotLID (or any one judge) decide alone whenever
# everyone else abstains, which defeats the point of building a panel.
MIN_CORROBORATING_JUDGES = 2

DISPUTES_FILENAME = "language_disputes.json"

JudgeVote = namedtuple("JudgeVote", ["code", "confidence"])

LanguageVerdict = namedtuple("LanguageVerdict", [
    "code",           # final ISO code to file under, or None if disputed/unknown
    "disputed",       # True if the panel couldn't reach consensus
    "votes",          # dict: judge name -> JudgeVote, only judges that didn't abstain
    "winner_code",    # the panel's best guess even when disputed (for logging)
    "winner_share",   # winner's weight / total participating weight (for logging)
])

# --------------------------------------------------------------------------
# ISO 639-3 (+ script) -> ISO 639-1 normalization
# --------------------------------------------------------------------------
# GlotLID and OpenLID both label with three-letter ISO codes plus a
# script tag (e.g. "nld_Latn", "kal_Latn") -- this project's folder
# layout and Stanza lemmatizer calls expect the shorter two-letter codes
# lingua and CLD3 already speak. This table is a closed list on purpose,
# same philosophy as _LINGUA_LANGUAGES in the main script: covers what's
# actually been scraped so far, extend it here first if a new language
# starts showing up. Anything NOT in this table falls back to keeping
# the raw three-letter code as the folder name instead of forcing it
# into UNKNOWN_LANG_FOLDER -- GlotLID's whole value is covering languages
# far outside this project's current list, and a real (if unmapped) ISO
# code is still far more useful as a folder name than "unknown" would be.
ISO_639_3_TO_1 = {
    "isl": "is", "eng": "en", "cym": "cy", "nob": "nb", "nno": "nn",
    "swe": "sv", "dan": "da", "deu": "de", "fra": "fr", "spa": "es",
    "nld": "nl", "gle": "ga", "kal": "kl",
}


def _normalize_iso639_3_label(raw_label):
    """'__label__nld_Latn' -> 'nl' (or 'nld' if not in ISO_639_3_TO_1)."""
    code3 = raw_label.replace("__label__", "").split("_")[0].lower()
    return ISO_639_3_TO_1.get(code3, code3)


# --------------------------------------------------------------------------
# Individual judges -- each returns a JudgeVote or None (abstain: package/
# model unavailable, or this judge's own confidence didn't clear its
# floor). Every loader is lazy + cached + wrapped so one judge failing to
# load (no internet for the model download, missing package, etc.) never
# takes down the others -- same "losing one voice doesn't take down the
# other" principle language_voices.py already established for the old
# two-voice system, just extended to four judges instead of two.
# --------------------------------------------------------------------------

_glotlid_model = None
_glotlid_load_failed = False


def _load_glotlid():
    global _glotlid_model, _glotlid_load_failed
    if _glotlid_model is not None or _glotlid_load_failed:
        return _glotlid_model
    try:
        import fasttext
        from huggingface_hub import hf_hub_download
        model_path = hf_hub_download(repo_id="cis-lmu/glotlid", filename="model.bin")
        _glotlid_model = fasttext.load_model(model_path)
    except Exception as e:
        _glotlid_load_failed = True
        print(f"⚠️  GlotLID unavailable ({e}) -- this judge will abstain for "
              "the rest of the run. (Needs internet access to download "
              "cis-lmu/glotlid's ~1.7GB model.bin the first time, and the "
              "fasttext + huggingface_hub packages installed.)")
    return _glotlid_model


def judge_glotlid(text):
    model = _load_glotlid()
    if model is None:
        return None
    sample = text[:2000].replace("\n", " ")
    labels, probs = model.predict(sample, k=1, threshold=GLOTLID_THRESHOLD)
    if not labels:
        return None  # below GLOTLID_THRESHOLD -- treated as abstain, not a guess
    return JudgeVote(_normalize_iso639_3_label(labels[0]), float(probs[0]))


_openlid_model = None
_openlid_load_failed = False


def _load_openlid():
    global _openlid_model, _openlid_load_failed
    if _openlid_model is not None or _openlid_load_failed:
        return _openlid_model
    try:
        import fasttext
        from huggingface_hub import hf_hub_download
        model_path = hf_hub_download(repo_id="HPLT/OpenLID-v3", filename="openlid-v3.bin")
        _openlid_model = fasttext.load_model(model_path)
    except Exception as e:
        _openlid_load_failed = True
        print(f"⚠️  OpenLID-v3 unavailable ({e}) -- this judge will abstain "
              "for the rest of the run. (Needs internet access to download "
              "HPLT/OpenLID-v3's ~1.2GB model the first time.)")
    return _openlid_model


_OPENLID_NONWORD_RE = re.compile(r"[^\w\s]|\d", re.UNICODE)
_OPENLID_SPACE_RE = re.compile(r"\s\s+")


def _openlid_preprocess(text):
    """Mirrors OpenLID-v3's own documented preprocessing (lowercase,
    flatten newlines, strip digits/punctuation, squeeze whitespace) --
    the model card's own usage example applies this before predicting,
    and skipping it measurably lowers confidence (see the OpenLID-v2
    card's own before/after example: 0.81 with cleaning vs. lower
    without, on the exact same sentence)."""
    text = text.strip().replace("\n", " ").lower()
    text = _OPENLID_NONWORD_RE.sub("", text)
    return _OPENLID_SPACE_RE.sub(" ", text)


def judge_openlid(text):
    model = _load_openlid()
    if model is None:
        return None
    sample = _openlid_preprocess(text[:2000])
    labels, probs = model.predict(sample, k=1, threshold=OPENLID_THRESHOLD)
    if not labels:
        return None
    return JudgeVote(_normalize_iso639_3_label(labels[0]), float(probs[0]))


_cld3_detector = None
_cld3_load_failed = False


def _load_cld3():
    global _cld3_detector, _cld3_load_failed
    if _cld3_detector is not None or _cld3_load_failed:
        return _cld3_detector
    try:
        import gcld3
        _cld3_detector = gcld3.NNetLanguageIdentifier(min_num_bytes=0, max_num_bytes=2000)
    except Exception as e:
        _cld3_load_failed = True
        print(f"⚠️  CLD3 unavailable ({e}) -- this judge will abstain for "
              "the rest of the run. (pip install gcld3 -- needs a C++ "
              "compiler and the protobuf compiler at install time.)")
    return _cld3_detector


def judge_cld3(text):
    detector = _load_cld3()
    if detector is None:
        return None
    result = detector.FindLanguage(text=text[:2000])
    # is_reliable is CLD3's own calibrated confidence gate, verified by
    # hand to correctly flip False on a Greenlandic sample it otherwise
    # would have silently misfiled as Somali -- this is the judge's real
    # abstain signal; the probability floor below is just a backstop for
    # whatever is_reliable doesn't catch.
    if not result.is_reliable or result.probability < CLD3_PROBABILITY_FLOOR:
        return None
    return JudgeVote(result.language.lower(), float(result.probability))


def judge_lingua(text, lingua_detector):
    """lingua_detector is passed in rather than built here -- it's already
    built once at import time in icelandic_text_extractor.py (adding
    Dutch and Irish to that build list is a separate, one-line fix; see
    the conversation this module came out of), and there's no reason to
    duplicate that detector or its startup cost."""
    if lingua_detector is None:
        return None
    sample = text[:2000]
    result = lingua_detector.detect_language_of(sample)
    if result is None:
        return None  # lingua's own internal ambiguity gate -- verified by
                      # hand that this returns None on genuinely unusable
                      # input rather than forcing a low-confidence guess
    confidence_values = lingua_detector.compute_language_confidence_values(sample)
    top_confidence = confidence_values[0].value if confidence_values else 0.0
    return JudgeVote(result.iso_code_639_1.name.lower(), top_confidence)


# --------------------------------------------------------------------------
# Ensemble resolution
# --------------------------------------------------------------------------

def _resolve_votes(votes):
    """votes: dict of judge name -> JudgeVote (abstaining judges already
    excluded by the caller). Returns (winner_code, winner_share,
    disputed) -- pure function, no I/O, so it's cheap to re-run after a
    Gemini tiebreak vote gets added to the same dict."""
    if not votes:
        return None, 0.0, True

    weighted = {}
    for judge_name, vote in votes.items():
        weighted[vote.code] = weighted.get(vote.code, 0.0) + JUDGE_WEIGHTS[judge_name] * vote.confidence

    total_weight = sum(weighted.values())
    winner_code, winner_weight = max(weighted.items(), key=lambda kv: kv[1])
    winner_share = (winner_weight / total_weight) if total_weight > 0 else 0.0

    disputed = (
        len(votes) < MIN_CORROBORATING_JUDGES
        or winner_share < CONSENSUS_SHARE_THRESHOLD
    )
    return winner_code, winner_share, disputed


def judge_language(text, lingua_detector):
    """Runs all four local judges (Gemini is NOT called here -- see
    reconcile_gemini_tiebreak below) and resolves their votes.

    Returns a LanguageVerdict. Three outcomes:

      1. No judge had anything to say at all (every model failed to
         load, or the text was too short/garbled for all four) ->
         code=None, disputed=False. This is the old UNKNOWN_LANG_FOLDER
         case, distinct from a genuine dispute: nobody voted, so there's
         nothing to disagree ABOUT.
      2. The panel reached consensus (a code holds >= CONSENSUS_SHARE_
         THRESHOLD of the participating weight, from >= MIN_CORROBORATING
         _JUDGES judges) -> code=<that code>, disputed=False.
      3. Real disagreement, OR only one judge managed to vote at all ->
         code=None, disputed=True. winner_code/winner_share are still
         filled in (the panel's best guess and how weak its lean was)
         so the caller can log a useful dispute record even though it's
         not being trusted to file the article under that language.
    """
    votes = {}
    for judge_name, vote in (
        ("glotlid", judge_glotlid(text)),
        ("openlid", judge_openlid(text)),
        ("cld3", judge_cld3(text)),
        ("lingua", judge_lingua(text, lingua_detector)),
    ):
        if vote is not None:
            votes[judge_name] = vote

    if not votes:
        return LanguageVerdict(code=None, disputed=False, votes={}, winner_code=None, winner_share=0.0)

    winner_code, winner_share, disputed = _resolve_votes(votes)
    return LanguageVerdict(
        code=None if disputed else winner_code,
        disputed=disputed,
        votes=votes,
        winner_code=winner_code,
        winner_share=winner_share,
    )


def reconcile_gemini_tiebreak(verdict, gemini_code):
    """Only called by the caller when verdict.disputed is True AND the
    --detect-language-llm flag is on -- this is the whole point of
    demoting Gemini to a tiebreaker: it's invoked ONLY for the subset of
    articles the local panel couldn't already resolve on its own, not
    per-article, which is the actual fix for the RPM-pressure problem
    this module was built to address, not just a smaller version of it.

    gemini_code: whatever language_voices.py's detect_language_llm()
    returned (already lowercase 2-letter, or None if the API voice had
    nothing to say -- quota exhausted, transient failure, etc).

    Returns a new LanguageVerdict with Gemini's vote folded in and the
    dispute re-resolved. If Gemini's vote is enough to push a code over
    CONSENSUS_SHARE_THRESHOLD, the dispute resolves; if not (Gemini
    agrees with nobody, or the panel was split three-plus ways), it
    stays disputed -- adding one more opinion doesn't manufacture
    consensus that isn't there, on purpose."""
    if gemini_code is None:
        return verdict  # API voice had nothing to add -- unchanged

    votes = dict(verdict.votes)
    votes["gemini"] = JudgeVote(gemini_code, 1.0)  # Gemini doesn't return a
                                                     # calibrated confidence
                                                     # today, so it votes at
                                                     # full strength -- its
                                                     # WEIGHT (0.9) is what
                                                     # keeps it from
                                                     # outvoting two
                                                     # corroborating local
                                                     # judges on its own
    winner_code, winner_share, disputed = _resolve_votes(votes)
    return LanguageVerdict(
        code=None if disputed else winner_code,
        disputed=disputed,
        votes=votes,
        winner_code=winner_code,
        winner_share=winner_share,
    )


# --------------------------------------------------------------------------
# Dispute logging
# --------------------------------------------------------------------------

def log_language_dispute(output_dir, url, verdict):
    """Appends a full per-judge vote breakdown to language_disputes.json,
    the direct successor to the old language_disagreements.json --
    renamed since this is no longer a two-voice disagreement log, it's a
    full panel breakdown. Deduped by URL, same as before.

    Returns True if a new entry was added, False if this URL was already
    logged."""
    path = os.path.join(output_dir, DISPUTES_FILENAME)

    existing = []
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing = json.load(f)
            if not isinstance(existing, list):
                existing = []
        except (json.JSONDecodeError, OSError):
            existing = []

    if any(isinstance(e, dict) and e.get("url") == url for e in existing):
        return False

    existing.append({
        "url": url,
        "domain": urlparse(url).netloc.replace("www.", ""),
        "votes": {name: {"code": v.code, "confidence": v.confidence} for name, v in verdict.votes.items()},
        "best_guess": verdict.winner_code,
        "best_guess_share": round(verdict.winner_share, 3),
        "logged_at": datetime.now(timezone.utc).isoformat(),
    })
    with open(path, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    return True
