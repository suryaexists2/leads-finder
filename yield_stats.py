"""
Persistent keyword/location performance tracking for the discovery source.

Used to (a) learn which (keyword, location) combos yield genuine web-dev
buyers, and (b) prioritize high-yield combos while skipping repeatedly-empty
ones so we don't waste the one-time Chocodata quota.

Stats are keyed by "keyword|location" and stored in data/discovery_stats.json.
"""
import json
import os
import time

import config

STATS_FILE = os.path.join(config.DATA_DIR, "discovery_stats.json")


def _load():
    try:
        with open(STATS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(d):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)


def _key(kw, loc):
    return f"{kw}|{loc}"


def get(kw, loc):
    return _load().get(_key(kw, loc), {
        "discovered": 0, "qualified": 0, "email": 0, "mx": 0,
        "final": 0, "zero": 0,
    })


def update(kw, loc, **fields):
    """Increment counters for a combo. fields: discovered/qualified/email/mx/final."""
    d = _load()
    rec = d.setdefault(_key(kw, loc), {
        "discovered": 0, "qualified": 0, "email": 0, "mx": 0, "final": 0, "zero": 0})
    for k, v in fields.items():
        if v and k in rec:
            rec[k] = rec.get(k, 0) + int(v)
    d[_key(kw, loc)] = rec
    _save(d)


def record_zero(kw, loc):
    """Mark a combo that produced zero web-dev results this query."""
    update(kw, loc, zero=1)


def is_skip(kw, loc, budget=2):
    """Skip combos that repeatedly produced zero hits and never any lead."""
    rec = get(kw, loc)
    if rec.get("final", 0) > 0 or rec.get("qualified", 0) > 0:
        return False
    return rec.get("zero", 0) >= budget


def ordered_combos(keywords, locations, first_unseen=True):
    """Return combos ordered by yield priority:
      1. never-tried combos first (to spread coverage / learn),
      2. then high-intent keywords (project/contract/agency),
      3. then combos with better final/mx yield.
    Combos that are chronic zero-yield are filtered out.
    """
    kw_order = _priority_rank(keywords)
    stats = _load()

    def rank_of(kw):
        return kw_order[kw]

    # learn: we want to explore unseen combos before re-hitting known ones
    def score(kw, loc):
        rec = stats.get(_key(kw, loc), {})
        final = rec.get("final", 0)
        mx = rec.get("mx", 0)
        q = rec.get("qualified", 0)
        # unseen combos rank higher (spread coverage)
        unseen = 5 if (rec.get("discovered", 0) == 0 and rec.get("zero", 0) == 0) else 0
        return (unseen, -rank_of(kw), final * 1000 + mx * 100 + q)

    combos = [(kw, loc) for kw, loc in _all_combos(keywords, locations)
              if not is_skip(kw, loc)]
    combos.sort(key=lambda c: score(c[0], c[1]), reverse=True)
    return combos


def _all_combos(keywords, locations):
    return [(kw, loc) for kw in keywords for loc in locations]


def _priority_rank(keywords):
    """Assign rank: high-intent project keywords get smaller (better) numbers."""
    high = set(config.PROJECT_HIGH_INTENT_KEYWORDS)
    rank = {}
    i = 0
    for kw in keywords:
        if kw in high:
            rank[kw] = i
            i += 1
    for kw in keywords:
        if kw not in rank:
            rank[kw] = i
            i += 1
    return rank


def performance_report():
    """Flatten stats for reporting (keyword and location level)."""
    stats = _load()
    kw = {}
    loc = {}
    for k, rec in stats.items():
        kw_name, loc_name = k.split("|", 1)
        for agg, part in ((kw, kw_name), (loc, loc_name)):
            a = agg.setdefault(part, {
                "discovered": 0, "qualified": 0, "email": 0, "mx": 0,
                "final": 0, "zero": 0})
            for f in a:
                a[f] += rec.get(f, 0)
    return {"keywords": kw, "locations": loc, "raw_combos": len(stats)}


# ─────────────────────────────────────────────────────────────────────────────
# Source-aware keyword rotation + yield tracking.
#
# Requirement: persistent, source×keyword rotation so every source spreads
# coverage and each source's yield is tracked independently. We keep a fresh
# store keyed by source -> {keyword -> {final, zero}} plus a persistent cursor
# per source so successive cycles pull DIFFERENT keywords for that source
# (spreading coverage) while still favoring never-tried and better-yielding
# keywords and skipping chronic zero-yield ones.
# ─────────────────────────────────────────────────────────────────────────────

SOURCE_STATS_FILE = os.path.join(config.DATA_DIR, "source_stats.json")
SOURCE_ROTATE_FILE = os.path.join(config.DATA_DIR, "source_cursor.json")


def _sload():
    try:
        with open(SOURCE_STATS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _ssave(d):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(SOURCE_STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)


def _cload():
    try:
        with open(SOURCE_ROTATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _csave(d):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(SOURCE_ROTATE_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)


def source_record(source, kw, final=0, zero=0):
    """Increment a keyword's yield stats for a given source."""
    d = _sload()
    srec = d.setdefault(source, {})
    rec = srec.setdefault(kw, {"final": 0, "zero": 0})
    rec["final"] = rec.get("final", 0) + int(final)
    rec["zero"] = rec.get("zero", 0) + int(zero)
    _ssave(d)


def _source_keyword_order(source, keywords):
    """Order keywords for a source: never-tried first, then better final-yield;
    chronic zero-yield keywords (with no final) are pushed to the end."""
    stats = _sload().get(source, {})
    high = set(config.PROJECT_HIGH_INTENT_KEYWORDS)

    def score(kw):
        rec = stats.get(kw, {})
        final = rec.get("final", 0)
        zero = rec.get("zero", 0)
        if final == 0 and zero >= 2:
            return (0, 1, 0)          # chronic zero, no proven yield -> last
        unseen = 1 if (kw not in stats) else 0
        hi = 1 if kw in high else 0
        return (unseen, hi, final)    # never-tried > high-intent > final yield

    return sorted(keywords, key=score, reverse=True)


def source_next_keywords(source, keywords, count):
    """Return the next `count` keywords to run for `source`, rotating through a
    persistent per-source cursor in yield-aware order. Returns (subset, wrapped)."""
    ordered = _source_keyword_order(source, list(keywords))
    if not ordered:
        return [], False
    c = _cload()
    idx = c.get(source, 0) % len(ordered)
    out = []
    wrapped = False
    for i in range(count):
        pos = (idx + i) % len(ordered)
        if i > 0 and pos < idx:
            wrapped = True
        out.append(ordered[pos])
    c[source] = (idx + count) % len(ordered)
    _csave(c)
    return out, wrapped
