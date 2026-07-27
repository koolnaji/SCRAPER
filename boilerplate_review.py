"""
boilerplate_review.py
======================
Turns the manual "read boilerplate_candidates.json, hand-write a regex,
edit boilerplate_patterns.py" loop into an interactive review: reads the
candidates, SUGGESTS a pattern for each one (generalized automatically
when enough examples exist to see what varies), and on approval writes
it straight into boilerplate_patterns.py -- comment block and all, same
style as every hand-written entry there.

This does NOT make boilerplate_patterns.py read the JSON at runtime, and
does NOT remove it. The two files still do different jobs (see the
answer this shipped alongside): boilerplate_patterns.py is what actually
filters every scrape, instantly and for free; this script is just what
now writes new entries into it, instead of you doing it by hand.

Generalization strategy: candidates are grouped by (domain, reason).
  - A group with 2+ examples gets diffed down to a regex: whatever's
    IDENTICAL across every example in the group stays literal, whatever
    DIFFERS (names, show titles, dates -- the "Bob Howard" in "Additional
    reporting by Bob Howard.") collapses to `.+`. This is exactly the
    manual judgment call made promoting the first three BBC patterns,
    just automated once there's more than one example to diff against.
  - A group with only 1 example can't be safely generalized (nothing to
    diff against, so there's no way to tell what's the fixed shape vs.
    what's incidental to this one instance) -- it's suggested as a
    literal, exact-match pattern instead, flagged as such so you know to
    keep an eye out for a second example before trusting it broadly.

Every suggestion is validated (compiles, matches every fragment in its
own group) before being shown, and prompted for approval one at a time --
nothing is ever written to boilerplate_patterns.py without an explicit
'y'. Declined and approved candidates are both marked "reviewed" in the
JSON so the same fragment doesn't get re-suggested next run.
"""

import re
import os
import json
import difflib
from datetime import datetime, timezone

from term_ui import rule, half_width

CANDIDATES_FILENAME = "boilerplate_candidates.json"

# Below these thresholds, a diffed prefix/suffix is too thin to trust as
# a real recurring shape rather than coincidental overlap between two
# unrelated fragments -- fall back to per-fragment literal patterns
# instead of forcing a shaky generalization.
MIN_PREFIX_CHARS = 8
MIN_SUFFIX_CHARS = 1


def _load_candidates(output_dir):
    path = os.path.join(output_dir, CANDIDATES_FILENAME)
    if not os.path.exists(path):
        return [], path
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            data = []
    except (json.JSONDecodeError, OSError):
        data = []
    return data, path


def _save_candidates(entries, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)


def _common_prefix(strings):
    """Common prefix across an arbitrary-size list of strings, found via
    the lexicographic-min/max shortcut: in sorted order, the two extreme
    strings bound every string between them character-by-character up to
    the point they diverge, so comparing just those two is equivalent to
    comparing all of them."""
    if not strings:
        return ""
    s1, s2 = min(strings), max(strings)
    i = 0
    while i < len(s1) and i < len(s2) and s1[i] == s2[i]:
        i += 1
    return s1[:i]


def _common_suffix(strings):
    return _common_prefix([s[::-1] for s in strings])[::-1]


def _generalize_group(fragments):
    """Returns a suggested regex string for a group of 2+ same-shaped
    fragments, or None if there isn't enough in common to trust a
    generalization (near-identical fragments with nothing to diff, or a
    shared prefix/suffix too thin to be meaningful -- see module
    docstring). Caller falls back to per-fragment literals in either
    case."""
    if len(fragments) < 2:
        return None

    prefix = _common_prefix(fragments)
    suffix = _common_suffix(fragments)
    shortest = min(len(f) for f in fragments)

    if len(prefix) + len(suffix) >= shortest:
        return None  # fragments are identical or near-identical -- nothing to generalize
    if len(prefix.strip()) < MIN_PREFIX_CHARS or len(suffix.strip()) < MIN_SUFFIX_CHARS:
        return None

    return f"^{re.escape(prefix.strip())}.+{re.escape(suffix.strip())}$"


def _literal_pattern(fragment):
    return f"^{re.escape(fragment.strip())}$"


def _build_suggestion(group):
    """group: list of candidate dicts sharing (domain, reason).
    Returns (pattern_str, is_generalized) -- validated to compile and
    match every fragment in the group before being returned."""
    fragments = [c["fragment"].strip() for c in group]
    pattern_str = _generalize_group(fragments)
    is_generalized = pattern_str is not None
    if pattern_str is None:
        # One-example group (or nothing meaningful to diff) -- literal
        # pattern per fragment isn't a single suggestion, so the caller
        # handles single-fragment groups one at a time instead of hitting
        # this path; this fallback only covers 2+ fragments that diffed
        # down to "basically identical".
        pattern_str = _literal_pattern(fragments[0])

    compiled = re.compile(pattern_str, re.IGNORECASE)
    if not all(compiled.search(f) for f in fragments):
        # Sanity check failed -- shouldn't happen given how the pattern
        # was built, but never show a suggestion that doesn't actually
        # match its own evidence.
        return None, False
    return pattern_str, is_generalized


def _already_covered(fragment, compiled_existing):
    return bool(compiled_existing.search(fragment.strip()))


def _compile_alternation(patterns):
    if not patterns:
        return re.compile(r"(?!x)x")  # never matches
    return re.compile("|".join(patterns), re.IGNORECASE)


def _insert_pattern_into_file(patterns_file_path, pattern_str, comment_lines):
    with open(patterns_file_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    start = next(i for i, l in enumerate(lines) if l.strip().startswith("BOILERPLATE_PATTERNS = ["))
    end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "]")

    new_lines = [f"    {c}\n" for c in comment_lines]
    new_lines.append(f"    {pattern_str!r},\n")

    lines = lines[:end] + new_lines + lines[end:]
    with open(patterns_file_path, "w", encoding="utf-8") as f:
        f.writelines(lines)


def _prompt(question):
    while True:
        ans = input(f"{question} [y/n/q] ").strip().lower()
        if ans in ("y", "yes", "n", "no", "q"):
            return ans[0]
        print("   Please answer y, n, or q.")


def run_review(output_dir, patterns_module):
    """patterns_module: the imported boilerplate_patterns module (passed
    in rather than imported here, so the caller controls exactly which
    copy on disk gets edited -- same "don't guess the path" caution as
    everywhere else file locations matter in this codebase)."""
    candidates, json_path = _load_candidates(output_dir)
    if not candidates:
        print(f"   No {CANDIDATES_FILENAME} found under {output_dir} -- nothing to review.")
        return

    active_patterns = list(patterns_module.BOILERPLATE_PATTERNS)
    compiled_existing = _compile_alternation(active_patterns)

    pending = [
        c for c in candidates
        if isinstance(c, dict)
        and not c.get("reviewed")
        and not _already_covered(c.get("fragment", ""), compiled_existing)
    ]
    if not pending:
        print("   Nothing new to review -- every candidate is already reviewed or already covered.")
        return

    groups = {}
    for c in pending:
        key = (c.get("domain", ""), c.get("reason", ""))
        groups.setdefault(key, []).append(c)

    promoted, skipped, quit_early = 0, 0, False

    for (domain, reason), group in groups.items():
        pattern_str, is_generalized = _build_suggestion(group)
        if pattern_str is None:
            print(f"\n⚠️  Couldn't build a safe pattern for {domain} / {reason!r} -- skipping, still in the log.")
            continue

        examples = [c["fragment"] for c in group][:3]
        # 11 chars of leading indent + 2 quote marks accounts for the
        # "           \"...\"" wrapper below, so the fragment preview
        # itself still lands inside half_width() rather than the
        # wrapper pushing the whole line past it.
        max_ex_len = max(20, half_width() - 13)
        print(f"\n{rule('-')}")
        print(f"Domain:  {domain}")
        print(f"Reason:  {reason}")
        print(f"Seen:    {len(group)} example(s){' (generalized from these)' if is_generalized else ' (single example -- literal match only)'}")
        for ex in examples:
            print(f"           \"{ex[:max_ex_len]}{'...' if len(ex) > max_ex_len else ''}\"")
        print(f"Pattern: {pattern_str!r}")

        choice = _prompt("Add this to boilerplate_patterns.py?")
        if choice == "q":
            quit_early = True
            break

        for c in group:
            c["reviewed"] = True
        if choice == "y":
            urls = sorted({c.get("url", "") for c in group if c.get("url")})
            comment_lines = [
                f"# Auto-suggested from {CANDIDATES_FILENAME} review "
                f"({datetime.now(timezone.utc).strftime('%Y-%m-%d')}) -- "
                f"{domain}, {reason}, {len(group)} example(s)."
            ]
            if urls:
                comment_lines.append(f"# e.g. {urls[0]}")
            _insert_pattern_into_file(patterns_module.__file__, pattern_str, comment_lines)
            active_patterns.append(pattern_str)
            compiled_existing = _compile_alternation(active_patterns)
            for c in group:
                c["promoted"] = True
            promoted += 1
            print("   ✅ Added.")
        else:
            skipped += 1
            print("   ↩️  Skipped (marked reviewed, won't be re-suggested).")

    _save_candidates(candidates, json_path)

    remaining = sum(1 for c in candidates if isinstance(c, dict) and not c.get("reviewed"))
    print(f"\n{rule('=')}")
    print(f"  Promoted: {promoted}   Skipped: {skipped}"
          f"{'   (stopped early -- q)' if quit_early else ''}")
    print(f"  Still unreviewed: {remaining}")
    print(rule("="))