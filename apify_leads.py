"""
Apify-powered discovery:
  - Google SERP intent search (free-ish, cheap) -> explicit buyers/agencies
  - LinkedIn company search (industry fallback) -> businesses that need a site
Each result is classified (qualification) and returned as a lead dict.
All results disk-cached so we never re-scrape the same source URL.
"""
import os
import json
import random
import re
import time

import config
import qualification
import apify_client
import web_presence
import budget
import maps_public


def _cache_path():
    return os.path.join(config.DATA_DIR, "google_intent_cache.json")


def _load_gcache():
    if os.path.exists(_cache_path()):
        try:
            with open(_cache_path(), "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_gcache(c):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(_cache_path(), "w", encoding="utf-8") as f:
        json.dump(c, f, indent=2)


def google_intent_discovery(max_results=8, max_new=20, phrases=None):
    """
    Google SERP intent discovery -> (leads, meta).

    Every result is normalized to an internal lead with company_website/domain
    derived from the result URL, so the website-email waterfall can run later.
    Direct service-seller pages are dropped at the source (they are not buyers).
    All surviving leads + the actor result are returned; the ENGINE performs the
    global buying-intent/foreign qualification so per-source counters stay true.

    `phrases` optionally constrains this call to a specific phrase subset
    (used by the source-aware keyword rotation in engine.run_cycle). When None,
    defaults to the full intent+agency phrase list, shuffled.

    meta = {source, status, error, actor, discovered, seller_dropped}
    status is the structured actor status (SUCCESS/START_FAILED/HTTP_4XX/
    TIMEOUT/ACTOR_FAILED/BUDGET/NO_KEY) — never silently [].
    Stops on the first committed actor failure (no budget-wasting retries).
    """
    cache = _load_gcache()
    leads = []
    seller_dropped = 0
    if phrases:
        rot_phrases = list(phrases)
    else:
        rot_phrases = list(config.GOOGLE_INTENT_PHRASES) + list(config.AGENCY_INTENT_PHRASES)
        random.shuffle(rot_phrases)

    # live pool: recomputed every phrase so real charges (not estimates) drive it
    status = "SUCCESS"
    error = ""
    for phrase in rot_phrases:
        budget_left = budget.non_maps_pool()
        if len(leads) >= max_new:
            break
        est = budget.est_intent(max_results)
        if est > budget_left:
            status = "BUDGET"
            error = budget.non_maps_skip_message("google_intent", pool=budget_left, est=est)
            break

        s, records, rerr = apify_client.google_serp_search(phrase, num_results=max_results)
        if s != "SUCCESS":
            status = s
            error = rerr
            break  # stop burning budget on a failing actor this cycle

        for r in records:
            if len(leads) >= max_new:
                break
            url = r.get("url") or ""
            if not url or url in cache:
                continue
            cache[url] = True

            raw_text = f"{r.get('title') or ''}. {r.get('description') or ''}"
            # Direct service-sellers are NOT buyers -> drop at source.
            if qualification.is_service_seller(raw_text):
                seller_dropped += 1
                continue

            # Merge the query intent into the text so classification sees intent.
            merged = f"{raw_text}. {phrase}"
            domain = r.get("domain") or ""
            lead = {
                "name": title_for_display(r.get("title") or ""),
                "title": r.get("title") or "",
                "post_text": merged,
                "description": r.get("description") or "",
                "post_url": url,
                "profile_url": url,
                "source_keyword": phrase,
                "source": "google_intent",
                "company": title_for_display(r.get("title") or ""),
                "domain": domain,
                "company_website": _url_from_domain(domain),
                "location": "",
            }
            qualification.qualify(lead)
            leads.append(lead)
        _save_gcache(cache)
        time.sleep(1)

    return leads, {
        "source": "google_intent",
        "status": status,
        "error": error,
        "actor": "apify/google-search-scraper",
        "discovered": len(leads) + seller_dropped,
        "seller_dropped": seller_dropped,
    }


def _is_business_noise(url, domain):
    """True if a SERP result points at an aggregator/portal/low-value page rather
    than the business's OWN website. These would pollute the client list."""
    if not domain:
        return True
    if domain in config.BUSINESS_NOISE_DOMAINS:
        return True
    lower = url.lower()
    for p in config.BUSINESS_NOISE_PATHS:
        if p in lower:
            return True
    return False


# Profile hosts where the URL is the BUSINESS's OWN social/listing page.
# A legit profile slug on these = the business exists but has NO owned website.
_PROFILE_HOSTS = [
    "facebook.com", "instagram.com", "linkedin.com", "tiktok.com",
    "twitter.com", "x.com", "youtube.com", "yelp.com", "yelp.ca", "pinterest.com",
]


def _social_profile_slug(url, domain):
    """If `url` is the business's OWN profile page on a social/listing platform,
    return the profile slug ('' otherwise). Only direct business profile pages
    count - never category/search/directory pages."""
    host = (domain or "").lower().lstrip("www.")
    if not any(h == host or h in host for h in _PROFILE_HOSTS):
        return ""
    if not url:
        return ""
    try:
        from urllib.parse import urlparse
        parts = [p for p in urlparse(url).path.split("/") if p]
    except Exception:
        return ""
    # facebook.com/<page> | instagram.com/<handle> | yelp.com/biz/<slug>
    # linkedin.com/company/<slug> | youtube.com/@handle|c/<slug>|<custom>
    if ("facebook.com" == host or "instagram.com" == host or host == "x.com" or host == "twitter.com") and len(parts) == 1:
        return parts[0][:80]
    if "linkedin.com" == host and len(parts) == 2 and parts[0] == "company":
        return parts[1][:80]
    if "tiktok.com" == host and len(parts) == 1 and parts[0].startswith("@"):
        return parts[0][:80]
    if "youtube.com" == host and parts and (parts[0].startswith("@") or len(parts) == 1):
        return "/".join(parts[:2])[:80]
    if ("yelp.com" == host or "yelp.ca" == host) and len(parts) == 2 and parts[0] == "biz":
        return parts[1][:80]
    if "pinterest.com" == host and len(parts) == 2 and parts[0] == "business-2":
        return parts[1][:80]
    return ""


def _plausible_business_title(title):
    """A business-name title is short and has no listicle/job mechanics."""
    if not title:
        return False
    t = title.lower().strip().strip("|")
    if any(x in t for x in ("&", "list of", "directory of", "top 10", "best ", " jobs",
                            " jobs in", "for rent", "events", " classes &", " activities",
                            " hiring", " template ")):
        return False
    words = [w for w in t.split() if w]
    return 1 <= len(words) <= 9


def google_business_discovery(max_results=5, max_new=25, phrases=None):
    """
    SERP-based BUSINESS-CLIENT discovery -> (leads, meta).

    Unlike google_intent_discovery (which hunts for explicit web-dev buyers),
    this targets OPERATING local businesses (real estate, dentists, restaurants,
    etc.) that are themselves POTENTIAL web/app/automation clients. It is HONEST
    about intent: it does NOT fabricate "needs a website". Leads are tagged
    lead_type='business_client' and qualified on evidence-based
    business_client_score (see qualification.is_business_client).

    Aggregator/portal results (Yelp, Zillow, directories, google maps, etc.) are
    dropped so we keep only the business's own-site URL for the email waterfall.
    EXCEPTION: the business's OWN social/listing profile page (Facebook/Instagram/
    Yelp biz/Yelp, etc.) is kept as a real no-website business (per user profile
    rule: "only a social/listing profile but no actual website" -> ACCEPT).

    `phrases` optional (used by engine's source-aware rotation). When None it
    builds industry x city queries from config and shuffles them.

    meta = {source, status, error, actor, discovered, dropped}
    Stops on first committed actor failure (no budget-wasting retries).
    """
    import web_presence
    if phrases:
        rot = list(phrases)
    else:
        rot = list(config.build_business_queries())
        random.shuffle(rot)

    leads = []
    dropped = 0
    # live non-Maps pool: recomputed every phrase so real charges (not estimates)
    # drive it, and the Maps reserve is re-honored on every iteration.
    status = "SUCCESS"
    error = ""
    for phrase in rot:
        budget_left = budget.non_maps_pool()
        if len(leads) >= max_new:
            break
        est = budget.est_google_business(max_results)
        if est > budget_left:
            status = "BUDGET"
            error = budget.non_maps_skip_message("google_business", pool=budget_left, est=est)
            break

        s, records, rerr = apify_client.google_serp_search(phrase, num_results=max_results)
        if s != "SUCCESS":
            status = s
            error = rerr
            break

        city = phrase.strip('"').split('"')[-1].strip() if '" "' in phrase else ""
        for r in records:
            if len(leads) >= max_new:
                break
            url = r.get("url") or ""
            domain = r.get("domain") or ""
            if not url or not domain:
                continue
            title = r.get("title") or ""

            # The business's OWN social/listing profile = a real no-website
            # business (user rule: "only Facebook/Instagram/Yelp/Google Business
            # profile but no actual website" -> ACCEPT). Bypasses the
            # aggregator-noise drop; the lead's "website" is the profile page,
            # which web_presence classifies as NO_WEBSITE.
            social_slug = _social_profile_slug(url, domain)
            if social_slug and _plausible_business_title(title):
                name = title.split("|")[0].split("-")[0].strip()[:80] or social_slug
                post_text = f"{title}. {r.get('description') or ''}"
                prof_url = _url_from_domain(domain)
                wp = {"web_status": "NO_WEBSITE", "web_confidence": 1.0,
                      "web_reason": "Only a social/listing profile ({0}) - no owned website".format(domain),
                      "signals": ["social/listing profile only"]}
                lead = {
                    "name": name, "company": name, "title": title,
                    "post_text": post_text, "description": r.get("description") or "",
                    "post_url": url, "profile_url": url, "source_url": url,
                    "source_keyword": phrase, "source": "google_business",
                    "domain": domain, "company_website": prof_url, "website": prof_url,
                    "location": city, "lead_type": "business_client",
                    "web_status": wp["web_status"], "web_confidence": wp["web_confidence"],
                    "web_reason": wp["web_reason"], "web_gap_signals": wp["signals"],
                    "business_client_score": 90,
                    "opportunity_reason": web_presence.web_gap_reason(domain, title, city, wp),
                }
                qualification.qualify(lead)
                leads.append(lead)
                continue

            if _is_business_noise(url, domain):
                dropped += 1
                continue

            name = title.split("|")[0].split("-")[0].strip()[:80] or domain
            # Honest context: facts only (industry + city), NO fabricated need.
            post_text = f"{title}. {r.get('description') or ''}"

            site_url = _url_from_domain(domain)
            wp = web_presence.classify(site_url, domain)

            # Early drop: only when HIGH confidence the business already
            # has a real usable website. Low-confidence REAL results stay
            # in the pipeline for further review.
            if wp["web_status"] == "REAL_WEBSITE" and wp["web_confidence"] >= 0.85:
                dropped += 1
                continue

            lead = {
                "name": name,
                "company": name,
                "title": title,
                "post_text": post_text,
                "description": r.get("description") or "",
                "post_url": url,
                "profile_url": url,
                "source_url": url,
                "source_keyword": phrase,
                "source": "google_business",
                "domain": domain,
                "company_website": site_url,
                "website": site_url,
                "location": city,
                "lead_type": "business_client",
                "web_status": wp["web_status"],
                "web_confidence": wp["web_confidence"],
                "web_reason": wp["web_reason"],
                "web_gap_signals": wp["signals"],
                "business_client_score": max(config.BUSINESS_CLIENT_QUALIFY_SCORE,
                                             wp["web_confidence"] * 100),
                "opportunity_reason": web_presence.web_gap_reason(
                    domain, title, city, wp),
            }
            qualification.qualify(lead)
            leads.append(lead)
        time.sleep(1)

    return leads, {
        "source": "google_business",
        "status": status,
        "error": error,
        "actor": "apify/google-search-scraper",
        "discovered": len(leads) + dropped,
        "dropped": dropped,
    }


def google_maps_discovery(max_results=None, max_new=None, phrases=None, countries=None):
    """GOOGLE MAPS / GOOGLE BUSINESS listing discovery -> (leads, meta).

    PRIMARY channel. Searches "<category> in <city>" across ALL target markets
    (US/UK/UAE/India/Canada/Australia) using the Apify google-search-scraper
    actor's `localResults` (the Google Maps pack = real Google Business data).

    THE KEY SIGNAL is the listing's `website` field:
      - EMPTY/missing  -> NO_WEBSITE (highest-priority new-website prospect).
                         No blind assumption: Google itself declares no website.
                         (No domain exists to web-verify; the listing IS the
                         verification source.)
      - present        -> web_presence.classify(domain):
                            REAL_WEBSITE   -> HARD reject (even if ugly/slow)
                            PLACEHOLDER    -> high priority
                            BROKEN         -> lower-priority recheck
                            UNKNOWN        -> kept but never qualifies until
                                              verified (engine rejects)

    Business-contact data comes straight off the listing (phone, category,
    rating, reviews, address) -> instantly contactable, exactly the
    "ABC Plumbing LLC, 4.7, Miami FL, no website" ideal client.

    `phrases` optional: list of ("<category> in <city>", city) tuples, or plain
    strings. `countries` restricts the rotation to a market subset (set both to
    "" / a single country for per-country tests). Uses the SAME natural rotation
    so winning countries/categories are re-searched, chronic zeroes are skipped.

    meta = {source, status, error, actor, discovered, dropped, real_website_dropped,
            no_website, maps}
    """
    if max_results is None:
        max_results = config.MAPS_DISCOVERY_RESULTS
    if max_new is None:
        max_new = config.MAPS_DISCOVERY_MAX_PER_CYCLE
    if phrases:
        rot = list(phrases)
    else:
        try:
            rot = list(config.build_maps_queries(countries=countries))
        except Exception:
            rot = []
        random.shuffle(rot)

    leads = []
    dropped = 0
    real_website_dropped = 0
    no_website_count = 0
    listed_website_count = 0
    seen = set()
    # live budget: recomputed every query so real charges (not estimates) drive
    # it — a cheap actor is never starved by a stale estimate.
    status = "SUCCESS"
    error = ""

    for q in rot:
        budget_left = budget.available()
        if len(leads) >= max_new:
            break
        if isinstance(q, (tuple, list)):
            query, city = q[0], q[1]
        else:
            query, city = q, ""
        est = budget.est_maps(max_results)
        if est > budget_left and not budget.maps_is_free():
            status = "BUDGET"
            error = budget.maps_skip_message()
            break
        if budget.maps_is_free():
            # KEYLESS public mode (maps_public.py): headless Edge, $0, no Apify,
            # no budget reserve — always attempted even when the Apify cap is
            # exhausted. Same record schema, same dedup/qualification below.
            s, records, rerr = maps_public.search_results(query)
        else:
            s, records, rerr = apify_client.google_maps_search(query, num_results=max_results)
        if s != "SUCCESS":
            status = s
            error = rerr
            break

        country = config._country_of_city(city) or ""
        for r in records:
            if len(leads) >= max_new:
                break
            name = (r.get("title") or "").strip()
            dk = (name.lower(), city.lower())
            if not name or dk in seen:
                continue
            seen.add(dk)
            category = (r.get("category") or "").strip()
            address = (r.get("address") or "").strip()
            phone = (r.get("phone") or "").strip()
            rating = r.get("rating")
            try:
                rating = float(rating) if rating not in (None, "") else None
            except Exception:
                rating = None
            reviews = r.get("reviews") or 0
            try:
                reviews = int(reviews)
            except Exception:
                reviews = 0
            website = (r.get("website") or "").strip()
            domain = (r.get("domain") or "").strip()

            # Card-level "no website" is provisional in public mode: Google
            # renders the Website button with changing classes and sometimes
            # ONLY on the place page (regression: LIFT Gym Wangara's search
            # card showed no lcr4fd anchor, yet its place panel declares
            # gymmemberships.com.au). Confirm against the place page BEFORE
            # EVER declaring NO_WEBSITE. Paid mode stays authoritative (Apify
            # already returns the listing's own website field).
            if not website and budget.maps_is_free() \
                    and getattr(config, "MAPS_PUBLIC_VERIFY_WEBSITE", True):
                mp_url = (r.get("maps_link") or r.get("url") or "").strip()
                if mp_url:
                    try:
                        site = maps_public.place_website(mp_url)
                    except Exception:
                        site = ""
                    if site:
                        website = site
                        domain = maps_public._hostname(site) or domain

            # ── website field present -> run web-presence verification ──
            if website:
                listed_website_count += 1
                site_url = _url_from_domain(domain) if domain else website
                wp = web_presence.classify(site_url, domain or site_url)
                ws = wp["web_status"]
                if ws == "REAL_WEBSITE" and wp["web_confidence"] >= 0.85:
                    real_website_dropped += 1
                    dropped += 1
                    continue
                if ws == "NO_WEBSITE" and wp["web_confidence"] >= 0.8:
                    no_website_count += 1
                bscore = max(config.BUSINESS_CLIENT_QUALIFY_SCORE,
                             int(wp["web_confidence"] * 70))
                reason = wp["web_reason"]
            else:
                # ── THE SIGNAL: Google Business listing website field EMPTY ──
                no_website_count += 1
                ws = "NO_WEBSITE"
                wp = {"web_status": "NO_WEBSITE", "web_confidence": 1.0,
                      "web_reason": "Google Business/Maps listing has NO website (field empty)"}
                bscore = 70
                reason = wp["web_reason"]
            if ws == "NO_WEBSITE":
                bscore = max(bscore, 72)

            # priority boost: established presence + contact + commercial category
            if ws == "NO_WEBSITE":
                if rating and rating >= 4.0 and reviews >= 5:
                    bscore += 10     # established reviews/presence
                if phone:
                    bscore += 10     # contactable via phone/WhatsApp
                catl = (category or "").lower()
                if any(hv in catl for hv in config.HIGH_VALUE_CATEGORIES):
                    bscore += 10     # commercial / high-value category
                if rating and rating >= 4.5 and reviews >= 200:
                    bscore += 5      # established business + strong reputation/reviews

            post_text = "Google Business/Maps listing: {0}. Category: {1}. {2}{3}.{4}".format(
                name, category or "n/a", (("Rating " + str(rating)) if rating else ""),
                ((" (" + str(reviews) + " reviews)") if reviews else ""),
                (" " + address) if address else "")
            opp = "Google Business listing ({category}) in {city}{country} — no website listed, a new-website prospect".format(
                category=category or "local business", city=city or address or "their city",
                country=(" (" + country + ")") if country else "")

            lead = {
                "name": name,
                "company": name,
                "category": category,
                "rating": rating,
                "reviews": reviews,
                "address": address,
                "phone": phone,
                "whatsapp": "yes" if phone else "no",
                "location": city or address or "",
                "query_loc": city or "",
                "country": country,
                "company_website": site_url if website else "",
                "website": site_url if website else "",
                "domain": domain or "",
                "profile_url": r.get("maps_link") or r.get("url") or "",
                "source_url": r.get("maps_link") or r.get("url") or "",
                "source_keyword": query,
                "source": "google_business",
                "lead_type": "business_client",
                "web_status": ws,
                "web_confidence": wp["web_confidence"],
                "web_reason": reason,
                "web_gap_signals": (wp.get("signals") or []) if wp.get("signals") else ["Google Business website field empty" if not website else wp.get("web_reason", "")],
                "business_client_score": min(bscore, 95),
                "opportunity_reason": opp,
                "post_text": post_text,
            }
            qualification.qualify(lead)
            leads.append(lead)

    return leads, {
        "source": "google_maps",
        "status": status,
        "error": error,
        "actor": ("maps-public/headless-edge" if budget.maps_is_free()
                  else "apify/google-search-scraper (maps/search)"),
        "cost": 0.0 if budget.maps_is_free() else None,
        "discovered": len(leads) + dropped,
        "dropped": dropped,
        "real_website_dropped": real_website_dropped,
        "no_website": no_website_count,
        "listed_website": listed_website_count,
        "maps": 1,
    }


def _url_from_domain(domain):
    if not domain:
        return ""
    if domain.startswith("http"):
        return domain
    return "https://" + domain


def title_for_display(title):
    """Best-effort display/company name from a search title (strip role words)."""
    words = re.split(r"\s+|\||–|—|:", title)
    stop = {"web", "developer", "hiring", "looking", "junior", "senior",
            "wordpress", "frontend", "front-end", "react", "full", "stack",
            "job", "opening", "opportunity", "position", "apply", "careers"}
    comp = [w for w in words if w and w.lower() not in stop and not re.search(r"\d", w)]
    return " ".join(comp[:3]).strip() or title.strip()[:40]


_company_rotation_idx = [0]


def company_discovery(industries=None, max_total=20, locations=None):
    """
    LinkedIn company discovery -> (leads, meta).

    Rotates across INDUSTRY_FALLBACK_IDS each cycle (never hardcoded to one
    industry) and passes `locations` for foreign-only targeting. Result text is
    HONEST (facts only, no fabricated "needs a website"). Leads are tagged
    lead_type='business_client' and (like SERP business discoveries) pass the
    engine pipeline on evidence-based business_client_score.

    NOTE: the linkedin-company-search actor (short mode) may omit website/domain;
    those records yield no email via the free waterfall and are dropped later
    (no domain). meta.status reflects the actor result.

    meta = {source, status, error, actor, discovered}
    """
    industry_list = industries or []
    if not industry_list:
        order = list(config.INDUSTRY_FALLBACK_IDS.items())
        if order:
            label, ids = order[_company_rotation_idx[0] % len(order)]
            _company_rotation_idx[0] += 1
            industry_list = ids if isinstance(ids, list) else [ids]
            industries = industry_list  # preserve for meta
        else:
            return [], {"source": "linkedin_company", "status": "SUCCESS",
                        "error": "no industries", "actor": "harvestapi/linkedin-company-search",
                        "discovered": 0}
    if not locations:
        locations = getattr(config, "BUSINESS_CITIES", None) or []
    status, records, err = apify_client.company_search(industry_list, limit=max_total,
                                                       locations=locations)
    meta = {
        "source": "linkedin_company",
        "status": status,
        "error": err,
        "actor": "harvestapi/linkedin-company-search",
        "discovered": len(records),
    }
    leads = []
    if status != "SUCCESS":
        return leads, meta
    for it in records:
        if len(leads) >= max_total:
            break
        name = it.get("name") or ""
        industry = it.get("industry") or ""
        website = it.get("website") or ""
        domain = it.get("domain") or ""
        # Honest, factual context: no fabricated need.
        post_text = f"Company {name} (industry: {industry or 'n/a'}). Located: {it.get('location') or ''}."
        lead = {
            "name": name,
            "company": name,
            "company_slug": it.get("company_slug") or "",
            "company_website": _url_from_domain(website) if website else "",
            "website": website,
            "domain": domain,
            "profile_url": it.get("url") or "",
            "post_url": it.get("url") or "",
            "title": industry,
            "post_text": post_text,
            "description": industry,
            "source_keyword": f"industry:{industry or 'n/a'}",
            "source": "linkedin_company",
            "lead_type": "business_client",
            "business_client_score": config.BUSINESS_CLIENT_QUALIFY_SCORE,
            "opportunity_reason": "LinkedIn company result; potential web/app services client",
            "location": it.get("location") or "",
        }
        qualification.qualify(lead)
        leads.append(lead)
    return leads, meta


def all_sources_layout_needed():
    st = budget.reserve_status()
    return {
        "budget_left_today": round(st["available"], 4),
        "spend_today": st["spend"],
        "maps_reserve": st["maps_reserve"],
        "maps_can_run": st["maps_can_run"],
        "non_maps_pool": st["non_maps_pool"],
    }
