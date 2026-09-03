"""
Centralized daily Apify budget allocation (single source of truth).

Every Apify-backed discovery source — and engine.run_cycle's Maps pre-check —
consults THIS module before spending. Goals (Option 1):

1. Google Maps/Business (the PRIMARY no-website channel) runs FIRST each
      cycle and keeps a daily reserved amount (config.MAPS_BUDGET_RESERVE) that
      no other Apify source may spend, so SERP/etc. can never starve it.
      (In "public" Maps mode — config.MAPS_DISCOVERY_MODE — Maps is a FREE
      keyless headless-browser channel: zero Apify spend, no reserve, no gate.)
   2. The hard daily cap config.MAX_DAILY_APIFY_SPEND is never exceeded.
   3. Non-Apify sources (bing/job/reddit/indeed) keep running regardless.
   4. Maps failure is never silent: "MAPS SKIPPED: reserved budget unavailable".

Estimates mirror the real actor ratecards; the live daily total comes from
data/apify_spend.json via apify_client.spend_today() (the same file the
Apify client records real usageTotalUsd into), so the tracker and this module
can never disagree.
"""
import config
import apify_client

MAPS_ACTOR_LABEL = "apify/google-search-scraper (maps/search)"


def _spend_total():
    try:
        return float(apify_client.spend_today().get("total", 0.0))
    except Exception:
        return 0.0


def available():
    """USD still available under the hard daily cap."""
    return max(0.0, config.MAX_DAILY_APIFY_SPEND - _spend_total())


def maps_reserve():
    """USD permanently kept free for the Maps/Business no-website channel."""
    return max(0.0, float(getattr(config, "MAPS_BUDGET_RESERVE", 0.032)))


def est_google_business(num_results):
    """google-search-scraper SERP query estimate (~$0.00105/result + overhead)."""
    return round(float(num_results) * 0.0012 + 0.002, 4)


def est_intent(num_results):
    return est_google_business(num_results)


def est_maps(num_results):
    """compass/crawler-google-places (Maps listing) query estimate.

    Measured live per 4-item run varies wildly: $0.0002 (cached/zero-result)
    to $0.0162 (real scraped listings ≈ $0.004/result). The estimate is the
    WORST-CASE per-query model (per-result $0.0045 + $0.003 overhead) so the
    per-run cap guard can never be out-run by a more expensive bill — the
    hard daily cap ($0.15) therefore can never be exceeded by surprise.
    Loops re-read real spend every query (see apify_leads), so the estimate
    throttles nothing except a check that one more query fits the true budget.
    """
    return round(float(num_results) * 0.0045 + 0.003, 4)


def maps_est_one_query():
    return est_maps(getattr(config, "MAPS_DISCOVERY_RESULTS", 6))


def maps_can_run():
    """PRIMARY channel gate: one full Maps query must fit the remaining cap.

    Only meaningful in "apify" Maps mode. In "public" mode Maps is a FREE
    keyless headless-browser channel (config.MAPS_DISCOVERY_MODE == "public",
    see maps_public.py) that never touches the Apify budget, so the budget has
    no say over it — use maps_is_free() to bypass this gate entirely.
    """
    return maps_est_one_query() <= available() + 1e-9


def maps_is_free():
    """True when Google Maps discovery runs in KEYLESS public mode: cost is
    exactly $0, zero Apify spend, zero reserve — Maps must always be attempted
    regardless of the daily Apify cap. When False, Maps runs on Apify and is
    subject to maps_can_run()/maps_skip_message()."""
    return str(getattr(config, "MAPS_DISCOVERY_MODE", "apify")).lower() == "public"


def maps_skip_message():
    return ("MAPS SKIPPED: reserved budget unavailable "
            "(est ${0:.4f} > available ${1:.4f})").format(
                maps_est_one_query(), available())


def non_maps_pool():
    """Budget non-Maps Apify sources may spend while the reserve stays intact.

    If even the reserve no longer funds one Maps query, the whole remainder is
    released (hoarding an unfundable reserve wastes budget — the priority rule
    already lost, maps_can_run() is False, so every leftover dollar may be used
    by other Apify sources up to the hard cap).
    """
    av = available()
    if av >= maps_est_one_query():
        return max(0.0, av - maps_reserve())
    return av


def can_spend(est, maps_priority=False):
    """Central gate -> (allowed, reason). maps_priority=True => Maps channel."""
    if maps_priority:
        if est <= available() + 1e-9:
            return True, ""
        return False, maps_skip_message()
    pool = non_maps_pool()
    if est <= pool + 1e-9:
        return True, ""
    return False, ("non-Maps Apify budget exhausted beyond "
                   "Maps reserve (pool ${0:.4f} < est ${1:.4f})").format(pool, est)


def non_maps_skip_message(tag, pool, est):
    return ("{0} SKIPPED: no Apify budget available beyond the Maps reserve "
            "(pool ${1:.4f} < est ${2:.4f})").format(tag.upper(), pool, est)


def reserve_status():
    return {
        "cap": config.MAX_DAILY_APIFY_SPEND,
        "spend": _spend_total(),
        "available": available(),
        "maps_reserve": maps_reserve(),
        "maps_est_one_query": maps_est_one_query(),
        "maps_can_run": maps_can_run(),
        "non_maps_pool": non_maps_pool(),
    }