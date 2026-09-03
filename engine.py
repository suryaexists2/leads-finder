"""
Orchestrator: continuous discover -> qualify -> enrich -> verify -> dedup -> save -> Telegram.

Per-cycle safety + quality rules:
  - daily counter (discovered/qualified/emails/verified/final/rejected/spend)
  - strict Apify budget guard (config.MAX_DAILY_APIFY_SPEND)
  - global all-time dedup (storage)
  - email verified = syntax + MX (free)
  - stop when daily verified target reached OR budget exhausted OR sources dry.
Imports are deferred to the call sites to avoid heavy web import at module load.
"""
import os
import time
import json
from datetime import datetime

import config
import storage

DAILY_STATE = os.path.join(config.DATA_DIR, "daily_state.json")


def _today():
    return time.strftime("%Y-%m-%d")


def _default_state():
    return {
        "date": _today(),
        "discovered": 0,
        "qualified": 0,
        "website": 0,
        "accessible": 0,
        "email_found": 0,
        "email_verified": 0,
        "final_saved": 0,
        "rejected": 0,
        "phone_contactable": 0,
        "no_email_saved": 0,
        "paid_enrich": 0,
        "spend_today": 0.0,
        "per_source": {},
        "country_dist": {},
        "maps_country_cursor": 0,
        # Status-wise funnel tracking
        "funnel": {
            "raw": 0,
            "valid_businesses": 0,
            "no_website": 0,
            "broken": 0,
            "placeholder": 0,
            "real_website": 0,
            "unknown": 0,
            "qualified_leads": 0,
            "contactable": 0,
            "email_found": 0,
            "verified": 0,
            "final": 0,
            "rejected": 0,
        },
    }


def load_daily_state():
    if os.path.exists(DAILY_STATE):
        try:
            with open(DAILY_STATE, "r", encoding="utf-8") as f:
                d = json.load(f)
            if d.get("date") == _today():
                return d
        except Exception:
            pass
    return _default_state()


def save_daily_state(d):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(DAILY_STATE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)
    return d


def _bump(d, key, n=1, source=None):
    d[key] = d.get(key, 0) + n
    if source:
        per = d.get("per_source")
        if not isinstance(per, dict):
            per = {}
            d["per_source"] = per
        sub = per.get(source)
        if not isinstance(sub, dict):
            sub = {}
            per[source] = sub
        sub[key] = sub.get(key, 0) + n
    return d


def _bump_funnel(d, stage, n=1, source=None):
    """Track raw -> valid_businesses -> web_status -> qualified -> contactable -> email_found -> verified -> final.
    Mutates the in-memory daily state dict; caller saves at end of run_cycle."""
    funnel = d.get("funnel", {})
    funnel[stage] = funnel.get(stage, 0) + n
    d["funnel"] = funnel


def _bump_country(d, country):
    """Country-distribution counter (US/UK/UAE/INDIA/CANADA/AUSTRALIA/other)."""
    cd = d.get("country_dist")
    if not isinstance(cd, dict):
        cd = {}
        d["country_dist"] = cd
    cc = (country or "").strip().upper() or "OTHER"
    cd[cc] = cd.get(cc, 0) + 1
    return d


def _maps_country_rotation(d):
    """Country-balanced rotation for the Maps channel. Returns the countries to
    search THIS cycle (advances a persisted cursor across all target markets so
    we never hammer one country)."""
    markets = ["US", "UK", "UAE", "INDIA", "CANADA", "AUSTRALIA"]
    ovr = getattr(config, "MAPS_COUNTRY_OVERRIDE", None)
    if ovr:
        return [ovr]   # test hook: per-country Maps validation runs
    n = max(1, int(getattr(config, "MAPS_COUNTRIES_PER_CYCLE", 3)))
    cur = int(d.get("maps_country_cursor", 0)) % len(markets)
    chosen = [markets[(cur + i) % len(markets)] for i in range(min(n, len(markets)))]
    d["maps_country_cursor"] = (cur + n) % len(markets)
    return chosen


def _now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _source_set(d, source, **fields):
    """Set non-counting per-source fields (error, last_success, last_error, ...)."""
    per = d.get("per_source")
    if not isinstance(per, dict):
        per = {}
        d["per_source"] = per
    sub = per.get(source)
    if not isinstance(sub, dict):
        sub = {}
        per[source] = sub
    sub.update(fields)
    return d


def _update_spend(d):
    try:
        import apify_client
        s = apify_client.spend_today()
        d["spend_today"] = s.get("total", 0.0)
    except Exception:
        pass
    return d


def _website_accessible(url, timeout=10):
    """Best-effort check that a business's own website is up (2xx/3xx). Used
    only for the per-source 'accessible' funnel metric — never blocks saving."""
    if not url:
        return False
    try:
        import curl_cffi.requests as cr
        r = cr.get(url, impersonate="chrome136", timeout=timeout,
                   headers={"User-Agent": "Mozilla/5.0"}, allow_redirects=True)
        return 200 <= r.status_code < 400
    except Exception:
        return False


def _record_yield(lead, **fields):
    """Record per-(keyword, location) yield for discovery optimization."""
    try:
        import yield_stats
        kw = lead.get("source_keyword")
        loc = lead.get("query_loc") or lead.get("_query_loc")
        if kw and loc:
            yield_stats.update(kw, loc, **fields)
    except Exception:
        pass


def _notify_new_lead(lead):
    """Send a Telegram card for a freshly saved, email-bearing lead.

    Only runs when a telegram token/user is configured. Uses the all-time
    sent-email set so a lead is never notified twice, and only notifies leads
    that carry a verified email. Failures are swallowed — Telegram must never
    break the save pipeline.
    """
    try:
        import storage
        import telegram_bot
        email = (lead.get("email") or "").strip()
        if not email:
            return False
        if email.lower() in storage.get_notified_emails():
            return False
        token, user = telegram_bot.get_config()
        if not token or not user:
            return False
        ok = telegram_bot.send_lead(lead)
        if ok:
            storage.mark_notified(email)
        return ok
    except Exception:
        return False


def run_cycle(source_override=None, max_cycle_discover=10,
              max_cycle_final=None):
    """
    One discovery + enrichment batch.
    Returns (final_saved_this_cycle, stats_dict).
    """
    import qualification
    import apify_client
    import chocodata_source
    import apify_leads
    import email_waterfall
    import yield_stats
    import budget

    d = load_daily_state()
    _update_spend(d)

    # ── budget guard ──
    spend = spend_today = d.get("spend_today", 0.0)
    budget_left = config.MAX_DAILY_APIFY_SPEND - spend
    final_already = d.get("final_saved", 0)
    apify_budget_exhausted = budget_left <= 0.02

    # stop conditions
    if final_already >= config.DAILY_TARGET_VERIFIED_LEADS:
        return 0, {"stop": "daily_target_reached", **d}

    # NOTE: an exhausted Apify daily budget does NOT stop the cycle.
    # Budget allocation is centralized in budget.py and maps/Business get the
    # PRIMARY slot: google_maps runs FIRST this cycle and owns a daily reserved
    # amount (config.MAPS_BUDGET_RESERVE). Every Apify-backed source (maps,
    # google_business SERP, google_intent, linkedin_company) consults it and
    # returns meta.status='BUDGET' WITHOUT spending any wallet when its share is
    # gone. With the cap hit, Apify sources simply skip today. NON-Apify sources
    # (chocodata_bing and co: api.chocodata.com via CHOCODATA_API_KEY — a
    # separate provider) keep discovering, and the WHOLE downstream pipeline
    # (qualification, dedup, contact enrichment, email verification, Gemini
    # email generation, Telegram notification, SMTP approval/send) continues to
    # run normally for their leads.

    cycle_stats = {"source": [], "found": 0, "qualified_cycle": 0,
                   "emails_found": 0, "verified": 0, "final": 0, "rejected": 0,
                   "notified": 0, "no_email_saved": 0}

    # ── 1. DISCOVERY — source-fair, independent per-source quotas ────────────
    # Each enabled source runs every cycle with its OWN quota (no source is
    # starved by another filling a shared cap, and no `len(raw_leads) < 5`
    # fallback gate). Sources are switched per profile in config:
    #   google_maps : Google Business/Maps listings  (Apify)  - PRIMARY no-website
    #   google_business: Google SERP local businesses (Apify)  - runs off non-maps pool
    #   chocodata_bing    : Bing engine local businesses (Chocodata)
    #   chocodata_job     : job boards / int of "hire a web dev"  - DISABLED
    #   chocodata_reddit  : Reddit hiring posts                    - DISABLED
    #   chocodata_indeed  : Indeed job board                       - DISABLED
    #   google_intent     : "looking for a developer" SERPs         - DISABLED
    #   linkedin_company  : LinkedIn company search (Apify)         - DISABLED
    CYCLE_COMBOS = 6
    cycle_combos, wrapped = (chocodata_source.next_combos(CYCLE_COMBOS)
                             if getattr(config, "ENABLE_CHOCODATA_JOB", False) else ([], False))
    raw_leads = []
    source_status = {}

    QUOTA = max(2, max_cycle_discover // 3)   # per-source cap per cycle

    bing_phrases, _ = yield_stats.source_next_keywords(
        "chocodata_bing", config.build_business_queries(), 3)
    if not bing_phrases:
        bing_phrases = config.build_business_queries()[:3]
    reddit_phrases, _ = (yield_stats.source_next_keywords(
        "chocodata_reddit", config.GOOGLE_INTENT_PHRASES, 2)
        if getattr(config, "ENABLE_CHOCODATA_REDDIT", False) else ([], False))
    indeed_kws, _ = (yield_stats.source_next_keywords(
        "chocodata_indeed", config.ALL_JOB_KEYWORDS, 3)
        if getattr(config, "ENABLE_CHOCODATA_INDEED", False) else ([], False))
    google_phrases, _ = (yield_stats.source_next_keywords(
        "google_intent", config.GOOGLE_INTENT_PHRASES + config.AGENCY_INTENT_PHRASES, 3)
        if getattr(config, "ENABLE_GOOGLE_INTENT", False) else ([], False))

    if source_override in (None, "chocodata") and getattr(config, "ENABLE_CHOCODATA_JOB", False):
        cjobs, cmeta = chocodata_source.discover_qualified_leads(
            limit_per_kw=4, max_total=QUOTA, combos=cycle_combos)
        raw_leads.extend(cjobs)
        source_status["chocodata_job"] = cmeta
        cycle_stats["source"].append("chocodata_job")

    if source_override in (None, "bing") and getattr(config, "ENABLE_CHOCODATA_BING", False):
        bleads, bmeta = chocodata_source.discover_bing_business_leads(
            phrases=bing_phrases, limit_per_phrase=6, max_total=QUOTA)
        raw_leads.extend(bleads)
        source_status["chocodata_bing"] = bmeta
        cycle_stats["source"].append("chocodata_bing")

    if source_override in (None, "reddit") and getattr(config, "ENABLE_CHOCODATA_REDDIT", False):
        rleads, rmeta = chocodata_source.discover_reddit_leads(
            phrases=reddit_phrases, limit_per_phrase=25, max_total=QUOTA)
        raw_leads.extend(rleads)
        source_status["chocodata_reddit"] = rmeta
        cycle_stats["source"].append("chocodata_reddit")

    if source_override in (None, "indeed") and getattr(config, "ENABLE_CHOCODATA_INDEED", False):
        ileads, imeta = chocodata_source.discover_indeed_leads(
            keywords=indeed_kws, limit_per_kw=4, max_total=QUOTA)
        raw_leads.extend(ileads)
        source_status["chocodata_indeed"] = imeta
        cycle_stats["source"].append("chocodata_indeed")

    if source_override in (None, "google") and getattr(config, "ENABLE_GOOGLE_INTENT", False):
        g_leads, gmeta = apify_leads.google_intent_discovery(
            max_results=8, max_new=QUOTA, phrases=google_phrases)
        raw_leads.extend(g_leads)
        source_status["google_intent"] = gmeta
        cycle_stats["source"].append("google_intent")

    # linkedin_company: DISABLED by default. The linkedin-company-search actor
    # (short mode) does not reliably return website/domain and returned 0 usable
    # leads in the controlled test. We never spend production Apify budget on it
    # until it is fixed and independently proven. Re-enable via
    # config.ENABLE_LINKEDIN_COMPANY = True when that changes.
    if source_override in (None, "company") and getattr(config, "ENABLE_LINKEDIN_COMPANY", False):
        co_leads, lmeta = apify_leads.company_discovery(
            max_total=QUOTA, locations=getattr(config, "BUSINESS_CITIES", None))
        raw_leads.extend(co_leads)
        source_status["linkedin_company"] = lmeta
        cycle_stats["source"].append("linkedin_company")

    # google_maps: PRIMARY no-website channel — runs FIRST among all Apify
    # sources so later SERP/other Apify calls can never starve it. Listings
    # with an EMPTY `website` field are the highest-quality website-development
    # prospects.
    #   - "public" Maps mode (config.MAPS_DISCOVERY_MODE): keyless headless-Edge
    #     channel, cost exactly $0 — ALWAYS attempted, there is no budget gate
    #     (the Apify daily cap/$0.15 can exhaust and Maps still runs).
    #   - "apify" mode: budget decided by the centralized budget module; when
    #     the reserved amount is unavailable the skip is loud, never silent.
    if source_override in (None, "gbiz", "google_business", "gmaps", "google_maps") \
            and getattr(config, "ENABLE_GOOGLE_BUSINESS", False):
        if budget.maps_is_free() or budget.maps_can_run():
            mcountries = _maps_country_rotation(d)
            gmaps_leads, gmmap = apify_leads.google_maps_discovery(
                countries=mcountries)
            for _l in gmaps_leads:
                _l["_maps_countries"] = ",".join(mcountries)
            if gmmap.get("status") != "SUCCESS" and gmmap.get("error"):
                print("[MAPS] " + gmmap.get("error"))
            elif gmmap.get("status") == "SUCCESS":
                print("[MAPS] status=SUCCESS actor={0} cost=${1:.4f} leads={2}".format(
                    gmmap.get("actor"), gmmap.get("cost") or 0.0, len(gmaps_leads)))
        else:
            gmaps_leads = []
            gmmap = {"source": "google_maps", "status": "BUDGET",
                     "actor": budget.MAPS_ACTOR_LABEL,
                     "error": budget.maps_skip_message(), "discovered": 0,
                     "dropped": 0, "real_website_dropped": 0,
                     "no_website": 0, "listed_website": 0, "maps": 1}
            print("[MAPS] " + budget.maps_skip_message())
        raw_leads.extend(gmaps_leads)
        source_status["google_maps"] = gmmap
        if gmaps_leads or gmmap.get("status") == "SUCCESS":
            cycle_stats["source"].append("google_maps")

    # google_business: SERP discovery of operating local businesses (potential
    # web/app/automation clients) — tagged business_client, no fabricated intent.
    # The discovery function builds industry x city queries internally and
    # rotates them each call, so we don't pass explicit phrases here. Its budget
    # is the non-Maps pool: it runs only while the Maps reserved amount stays
    # untouched, so a SERP-heavy morning can never eat the Maps allowance.
    if source_override in (None, "gbiz", "google_business") and getattr(config, "ENABLE_GOOGLE_BUSINESS", False):
        gb_leads, gbmeta = apify_leads.google_business_discovery(
            max_results=config.BUSINESS_DISCOVERY_RESULTS,
            max_new=config.BUSINESS_DISCOVERY_MAX_PER_CYCLE)
        raw_leads.extend(gb_leads)
        source_status["google_business"] = gbmeta
        cycle_stats["source"].append("google_business")

    # per-source discovered + health fields
    for src, meta in source_status.items():
        _bump(d, "discovered", meta.get("discovered", 0), src)
        st = meta.get("status")
        if st != "SUCCESS":
            _source_set(d, src, error=(meta.get("error") or meta.get("status") or ""),
                        last_error=_now_str())
        else:
            _source_set(d, src, error="", last_success=_now_str())

    cycle_stats["found"] = len(raw_leads)
    cycle_stats["source_status"] = source_status

    # Track raw leads entering the funnel
    _bump_funnel(d,"raw", len(raw_leads))

    # ── 2. QUALIFY (drop service-sellers / non-buyers) ──
    qualified = []
    for lead in raw_leads:
        if not lead:
            continue
        if not qualification.is_qualified(lead):
            _bump(d, "rejected", 1, lead.get("source"))
            cycle_stats["rejected"] += 1
            cycle_stats.setdefault("reject_reasons", {})
            reason = qualification.reject_reason(lead)
            cycle_stats["reject_reasons"][reason] = \
                cycle_stats["reject_reasons"].get(reason, 0) + 1
            continue
        qualified.append(lead)

    # Track valid businesses and web_status funnel stages
    for lead in qualified:
        _bump_funnel(d,"valid_businesses", 1, lead.get("source"))
        ws = lead.get("web_status")
        if ws == "NO_WEBSITE":
            _bump_funnel(d,"no_website", 1, lead.get("source"))
        elif ws == "BROKEN_WEBSITE":
            _bump_funnel(d,"broken", 1, lead.get("source"))
        elif ws == "PLACEHOLDER_WEBSITE":
            _bump_funnel(d,"placeholder", 1, lead.get("source"))
        elif ws == "REAL_WEBSITE":
            _bump_funnel(d,"real_website", 1, lead.get("source"))
        elif ws == "UNKNOWN":
            _bump_funnel(d,"unknown", 1, lead.get("source"))
        _bump(d, "qualified", 1, lead.get("source"))
        _bump_funnel(d,"qualified_leads", 1, lead.get("source"))
        # Country distribution (maps/business source carries its market; SERP
        # leads resolve via the query city).
        if not lead.get("country"):
            lead["country"] = config._country_of_city(lead.get("location") or "")
        _bump_country(d, lead.get("country") or "OTHER")
        _record_yield(lead, qualified=1)
        try:
            yield_stats.source_record(
                lead.get("source"), lead.get("source_keyword"), final=0, zero=0)
        except Exception:
            pass
    cycle_stats["qualified_cycle"] = len(qualified)

    # ── 3+4+5. ENRICH + VERIFY + final dedup + save ──
    # Final-save source fairness: an email-rich source must not drown the
    # output just because it is easier to extract emails from. Each source gets
    # a per-cycle final cap, and local-business leads are processed first so
    # the output reflects what the system actually finds across sources.
    per_source_final = {}
    MAX_FINAL_PER_SOURCE = max(1, config.DAILY_TARGET_VERIFIED_LEADS // max(1, len(cycle_stats["source"])))

    def _biz_key(lead):
        """Priority queue: business-client leads first, then by lead score desc.
        Strongest no-website prospects (high business_client_score) rise to the
        top so they are processed, notified and drafted first."""
        lt = 0 if (lead.get("lead_type") == "business_client") else 1
        score = lead.get("business_client_score") or lead.get("intent_score") or 0
        return (lt, -score)

    # Cap the email-extraction window. Default comes from config so the throttle
    # can be tuned centrally; all real limits (per-source final cap, daily
    # target, Apify budget) still apply below.
    if max_cycle_final is None:
        max_cycle_final = int(getattr(config, "MAX_FINAL_PROCESS_PER_CYCLE", 25))
    ordered = sorted(qualified[:max_cycle_final], key=_biz_key)
    final_this_cycle = 0
    no_email_save = []
    paid_enrich_used = d.get("paid_enrich", 0)
    for lead in ordered:
        src = lead.get("source")
        if src and per_source_final.get(src, 0) >= MAX_FINAL_PER_SOURCE:
            _bump(d, "rejected", 1, src)
            cycle_stats["rejected"] += 1
            continue

        # Safety: skip REAL_WEBSITE leads that slipped through
        # (shouldn't happen but belt-and-suspenders).
        if lead.get("web_status") == "REAL_WEBSITE":
            _bump(d, "rejected", 1, src)
            cycle_stats["rejected"] += 1
            continue

        # Safety: skip UNKNOWN leads - ambiguous technical result,
        # don't treat as a no-website lead.
        if lead.get("web_status") == "UNKNOWN":
            _bump(d, "rejected", 1, src)
            cycle_stats["rejected"] += 1
            continue

        # Consistency guard: the lead carries a REAL domain (website field) yet
        # web_status says NO_WEBSITE for a non-social host. That can only mean
        # the website field and the classifier disagree (Everkool-type bug).
        # Re-run the classifier now; REAL/UNKNOWN results are rejected here so a
        # business with a working website can never be saved as a no-website lead.
        if lead.get("web_status") == "NO_WEBSITE":
            _site = lead.get("company_website") or lead.get("website") or ""
            if _site:
                try:
                    import web_presence
                    _h = web_presence._host_of(_site)
                    if _h and _h not in web_presence.SOCIAL_PROFILE_HOSTS:
                        _wp = web_presence.classify(_site, _h)
                        lead["web_status"] = _wp["web_status"]
                        lead["web_confidence"] = _wp["web_confidence"]
                        lead["web_reason"] = _wp["web_reason"]
                        if lead["web_status"] in ("REAL_WEBSITE", "UNKNOWN"):
                            _bump(d, "rejected", 1, src)
                            cycle_stats["rejected"] += 1
                            continue
                except Exception:
                    pass

        # Safety: skip BROKEN_WEBSITE leads — a dead/unreachable website
        # (WAF block, Cloudflare, 404, timeout) is still a WEBSITE. "No
        # website" means the listing declares none; a blocked/offline page is
        # not a missing page, so a broken one must never be saved or emailed
        # as a no-website prospect (John Reed-class false positive).
        if lead.get("web_status") in ("BROKEN_WEBSITE", "BROKEN"):
            _bump(d, "rejected", 1, src)
            cycle_stats["rejected"] += 1
            continue

        company_website = lead.get("company_website", "")
        company_url = lead.get("profile_url", "") or lead.get("company_url", "")
        company = lead.get("company") or lead.get("name", "")

        # Funnel stage: has an owned website (needed for the free email path),
        # followed by an accessibility check for the per-source funnel metric.
        # SKIP for NO_WEBSITE leads - no website exists.
        if company_website and lead.get("web_status") != "NO_WEBSITE":
            _bump(d, "website", 1, src)
            if _website_accessible(company_website):
                _bump(d, "accessible", 1, src)

        # Paid enrichment (Prospeo/Employees SHORT) only for strong leads worth
        # enriching AND while the daily budget remains. Strong = qualified
        # (already filtered) + we hold a real LinkedIn company URL the actor
        # can process. Otherwise run only free paths (source text + website).
        is_linkedin_company = "linkedin.com/company/" in (company_url or "")
        worth_enrich = (config.AUTO_PAID_ENRICH and
                        is_linkedin_company and
                        paid_enrich_used < config.MAX_PAID_ENRICH_DAY and
                        not (lead.get("phone") or ""))
        if worth_enrich:
            paid_enrich_used += 1
            d["paid_enrich"] = paid_enrich_used

        # NO_WEBSITE: skip website-dependent email waterfall.
        # Source-text email, Apollo, Prospeo still run.
        # Primary contact channel for NO_WEBSITE is phone/SMS/WhatsApp.
        email, source, detail = email_waterfall.resolve_email(
            lead, company_website=company_website,
            company_url=company_url, company_name=company,
            allow_enrich=worth_enrich,
            web_status=lead.get("web_status", ""))
        lead["email"] = email
        lead["email_source"] = source

        phone = (lead.get("phone") or "").strip()
        has_wa = bool(phone)
        # Website has no phone-searchable domain for NO_WEBSITE leads (their
        # "website" is just a social profile page), so pull phone candidates
        # from the discovery snippet text via the same heuristics. This turns
        # social-only/no-email businesses (primary client) contactable.
        if not phone:
            if lead.get("web_status") == "NO_WEBSITE":
                phone, has_wa = email_waterfall.find_contact_phones(
                    "", web_text=lead.get("post_text", ""))
            elif company_website:
                phone, has_wa = email_waterfall.find_contact_phones(
                    company_website, web_text=lead.get("post_text", ""))
        lead["phone"] = phone
        lead["whatsapp"] = "yes" if has_wa else "no"

        if email:
            _bump(d, "email_found", 1, lead.get("source"))
            cycle_stats["emails_found"] += 1
            _record_yield(lead, email=1)
            ok, _ = email_waterfall.verify_email(email)
            lead["email_verified"] = "yes" if ok else "no"
            if ok:
                _bump(d, "email_verified", 1, lead.get("source"))
                _bump(d, "mx_valid", 1, lead.get("source"))
                cycle_stats["verified"] += 1
                _record_yield(lead, mx=1)
            else:
                # fall back: keep lead but not "verified"; store in no-email side later
                continue
        else:
            lead["email_verified"] = "no"
            # PRIMARY client = a local business WITHOUT a website. Their public
            # phone/WhatsApp (contact priority #2/#3) is often their ONLY
            # reachable channel when no email exists. Preserve contactable
            # business leads instead of silently dropping them.
            has_contact = bool((lead.get("phone") or "").strip()) or \
                lead.get("whatsapp") == "yes" or bool(lead.get("email_source"))
            if lead.get("lead_type") == "business_client" and has_contact:
                no_email_save.append(lead)
                _bump(d, "phone_contactable", 1, src)
                _bump_funnel(d, "contactable", 1, src)
            continue

        # ── final save with global dedup ──
        saved_email, saved_no_email, rejected = storage.add_leads([lead], [])
        cycle_stats["final"] += saved_email
        final_this_cycle += saved_email
        if saved_email:
            _record_yield(lead, final=1)
            _bump(d, "final_saved", saved_email, lead.get("source"))
            per_source_final[src] = per_source_final.get(src, 0) + saved_email
            _bump(d, "rejected", 0)
            try:
                yield_stats.source_record(
                    lead.get("source"), lead.get("source_keyword"), final=saved_email, zero=0)
            except Exception:
                pass
            _bump_funnel(d,"contactable", 1, lead.get("source"))
            _bump_funnel(d,"email_found", 1, lead.get("source"))
            if _notify_new_lead(lead):
                cycle_stats["notified"] += 1
            # AI cold-email automation: generate -> quality -> draft/auto-send.
            # Never blocks/short-circuits the discovery loop; failures are
            # isolated inside email_automation (returns status, never raises).
            try:
                import email_automation
                email_automation.process_new_lead(lead)
            except Exception:
                pass
        if rejected:
            _bump(d, "rejected", rejected, lead.get("source"))
            cycle_stats["rejected"] += rejected

        if d.get("final_saved", 0) >= config.DAILY_TARGET_VERIFIED_LEADS:
            break

    # Persist email-less business leads that only have a public phone/WhatsApp
    # contact (still reachable, still a qualified no-website client).
    if no_email_save:
        se, sne, rej = storage.add_leads([], no_email_save)
        if sne:
            _bump(d, "no_email_saved", sne)
            cycle_stats["no_email_saved"] = sne
        if rej:
            _bump(d, "rejected", rej)
            cycle_stats["rejected"] += rej

    # tally spend again
    _update_spend(d)
    save_daily_state(d)
    cycle_stats.update(d)
    return final_this_cycle, cycle_stats


if __name__ == "__main__":
    print("engine module — run via scheduler_worker or webapp")
