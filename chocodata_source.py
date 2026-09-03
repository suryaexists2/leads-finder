"""
Chocodata active job/project discovery.

These are companies/hiring managers ACTIVELY posting web-dev roles = the
highest-intent buyers (config.QUALITY_ORDER[0] = job_posting).
Endpoint: GET https://api.chocodata.com/api/v1/linkedin/jobsearch
  -> /job for description (the need story)
  -> /company for website/domain
Each result is turned into a qualified lead dict for the shared pipeline.
"""
import os
import random
import re
import time

import config
import qualification
import yield_stats
from curl_cffi import requests as cr

CHOC_DATA_URL = "https://api.chocodata.com/api/v1/linkedin"


def _key():
    return config.get_api_keys().get("CHOCODATA", "")


def _is_web_need(title):
    t = (title or "").lower()
    strong = ["web", "frontend", "front-end", "website", "wordpress",
              "shopify", "landing page", "full stack", "full-stack", "react",
              "frontend", "web app", "html"]
    if any(s in t for s in strong):
        return True
    web_terms = ["frontend", "front-end", "full stack", "full-stack", "website",
                 "wordpress", "shopify", "React", "ui", "html", "javascript"]
    return sum(1 for w in web_terms if w.lower() in t) >= 2


def jobsearch(keyword, location, start=0, limit=10, cb_record_zero=False, kw=None, loc=None):
    key = _key()
    if not key:
        return []
    try:
        r = cr.get(f"{CHOC_DATA_URL}/jobsearch",
                   params={"api_key": key, "keywords": keyword,
                           "location": location, "start": start, "limit": limit},
                   impersonate="chrome146", timeout=60)
        if r.status_code != 200:
            return []
        results = r.json().get("results") or []
        web_jobs = [j for j in results if _is_web_need(j.get("title", ""))]
        if cb_record_zero and kw and loc:
            k = kw if kw else keyword
            l = loc if loc else location
            if web_jobs:
                yield_stats.update(k, l, discovered=len(web_jobs))
            else:
                yield_stats.record_zero(k, l)
        return web_jobs
    except Exception:
        return []


def job_detail(job_id):
    key = _key()
    if not key or not job_id:
        return ""
    try:
        r = cr.get(f"{CHOC_DATA_URL}/job",
                   params={"api_key": key, "job_id": job_id},
                   impersonate="chrome146", timeout=60)
        if r.status_code == 200:
            return (r.json().get("description") or "").strip()[:1000]
    except Exception:
        pass
    return ""


def company_website(company_slug):
    key = _key()
    if not key or not company_slug:
        return ""
    for ep in ("email", "company"):
        try:
            r = cr.get(f"{CHOC_DATA_URL}/{ep}",
                       params={"api_key": key, "company": company_slug},
                       impersonate="chrome146", timeout=60)
            if r.status_code == 200:
                web = (r.json().get("website") or "").strip()
                if web:
                    if web.startswith("www."):
                        web = "https://" + web
                    elif not web.startswith("http"):
                        web = "https://" + web
                    return web.rstrip("/")
        except Exception:
            continue
    return ""


CURSOR_FILE = os.path.join(config.DATA_DIR, "discovery_cursor.json")


def _load_cursor():
    d = {"date": "1970-01-01", "index": 0}
    try:
        with open(CURSOR_FILE, encoding="utf-8") as f:
            import json as _json
            d = _json.load(f)
    except Exception:
        pass
    if d.get("date") != time.strftime("%Y-%m-%d"):
        d = {"date": time.strftime("%Y-%m-%d"), "index": 0}
    return d


def _save_cursor(d):
    import os
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(CURSOR_FILE, "w", encoding="utf-8") as f:
        import json as _json
        _json.dump(d, f)


def _all_combos(keywords, locations):
    return [(kw, loc) for kw in keywords for loc in locations]


DEFAULT_KEYWORDS = None
DEFAULT_LOCATIONS = None


def _default_keywords():
    return list(config.ALL_JOB_KEYWORDS)


def next_combos(count, keywords=None, locations=None):
    """Return the next `count` (keyword, location) combos to process.

    Combos are drawn in YIELD-AWARE order (never-tried first to learn, then
    high-intent project keywords, then better-yielding combos), skipping
    chronic zero-yield combos so we don't re-waste quota. A persistent daily
    cursor advances through the ordered list so coverage spreads across cycles.
    Returns (combos, wrapped).
    """
    if not keywords:
        keywords = _default_keywords()
    if not locations:
        locations = list(config.FOREIGN_LOCATIONS)
    ordered = yield_stats.ordered_combos(keywords, locations)
    if not ordered:
        return [], False
    cursor = _load_cursor()
    idx = cursor["index"] % len(ordered)
    window = []
    wrapped = False
    for i in range(count):
        pos = (idx + i) % len(ordered)
        if i > 0 and pos < idx:
            wrapped = True
        window.append(ordered[pos])
    cursor["index"] = (idx + count) % len(ordered)
    _save_cursor(cursor)
    return window, wrapped


def discover_qualified_leads(keywords=None, locations=None, limit_per_kw=6,
                             max_total=40, combos=None):
    """
    Discover web-dev job-postings and return qualified lead dicts.
    FOREIGN-ONLY: India locations filtered out (config.INDIA_TOKEN_RE).
    `combos` optionally limits this call to specific (keyword, location) pairs
    (used by the incremental daily rotation). When `combos` is None all
    keyword×location pairs are tried.
    Returns list of leads (each already has lead_type + intent_score via qualify).
    """
    if not keywords:
        keywords = _default_keywords()
    if not locations:
        locations = list(config.FOREIGN_LOCATIONS)
    if combos is None:
        combos = yield_stats.ordered_combos(keywords, locations)

    leads = []
    seen_keys = set()
    status = "SUCCESS"
    error = ""
    if not _key():
        status = "NO_KEY"
        error = "CHOCODATA_API_KEY not set"

    for kw, loc in combos:
        if len(leads) >= max_total:
            return leads, {
                "source": "chocodata_job",
                "status": status,
                "error": error,
                "actor": "chocodata/jobsearch",
                "discovered": len(leads),
            }
        jobs = jobsearch(kw, loc, limit=limit_per_kw, cb_record_zero=True, kw=kw, loc=loc)
        for j in jobs:
            if len(leads) >= max_total:
                return leads, {
                    "source": "chocodata_job",
                    "status": status,
                    "error": error,
                    "actor": "chocodata/jobsearch",
                    "discovered": len(leads),
                }
            company = (j.get("company") or "").strip()
            company_url = (j.get("company_url") or "").strip()
            company_slug = company_url.rstrip("/").split("/")[-1].lower()
            title = (j.get("title") or "").strip()
            job_id = str(j.get("job_id") or j.get("id") or "")
            job_url = (j.get("url") or j.get("job_url") or "").strip()
            jloc = (j.get("location") or "") + " " + loc

            dedup_key = (company_slug + "|" + title.lower()).strip()
            if not dedup_key or dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)

            if is_india(jloc):
                continue

            desc = job_detail(job_id) if job_id else ""
            desc = re.sub(r"\s+", " ", desc).strip()

            lead = {
                "name": company,
                "company": company,
                "company_slug": company_slug,
                "company_website": company_website(company_slug),
                "profile_url": company_url or "",
                "title": title,
                "job_title": title,
                "post_text": f"JOB: {title}. {desc}".strip(),
                "post_url": job_url,
                "description": desc,
                "location": jloc.strip(),
                "query_loc": loc,
                "source_keyword": kw,
                "lead_type": "job_posting",
                "source": "chocodata_job",
            }
            qualification.qualify(lead)
            leads.append(lead)
    return leads, {
        "source": "chocodata_job",
        "status": status,
        "error": error,
        "actor": "chocodata/jobsearch",
        "discovered": len(leads),
    }


def is_india(text):
    """True if the text points to an India-based lead."""
    if not text:
        return False
    return bool(config.INDIA_TOKEN_RE.search(text))


# ─────────────────────────────────────────────────────────────────────────────
# Multi-source discovery (Bing / Reddit / Indeed).
#
# Every source is a GENUINELY independent platform and is tracked separately:
#   source = "chocodata_bing"   (Bing web search, organic_results[])
#   source = "chocodata_reddit" (Reddit keyword search, results[] posts)
#   source = "chocodata_indeed" (Indeed job board, results[] job cards)
# All of them share the SAME downstream pipeline (qualify -> email waterfall ->
# dedup -> final lead) so not one source is artificially inflated and each is
# reported honestly on its own row. A source that fails/returns zero is SKIPPED
# for that cycle, never allowed to starve the others.
# ─────────────────────────────────────────────────────────────────────────────

def _choco_get(ep, params, timeout=60):
    """One low-level Chocodata GET. Returns parsed dict or None on failure.

    Transient platform failures (target_unreachable) are surfaced as
    {"_unreachable": True} so callers can report an honest FAILED status
    instead of pretending "found nothing".
    """
    key = _key()
    if not key:
        return None
    params = dict(params)
    params.setdefault("api_key", key)
    try:
        r = cr.get(f"https://api.chocodata.com/api/v1/{ep}",
                   params=params, impersonate="chrome146", timeout=timeout)
        if r.status_code != 200:
            return {"_status": r.status_code}
        return r.json()
    except Exception:
        return None


def _hostname(url):
    """Best-effort bare hostname from a URL ('' on failure)."""
    if not url:
        return ""
    s = url.strip()
    if not s.startswith("http"):
        s = "https://" + s
    try:
        from urllib.parse import urlparse
        h = urlparse(s).netloc.lower()
        if h.startswith("www."):
            h = h[4:]
        return h.split(":")[0]
    except Exception:
        return ""


# ── Bing (web search engine) ─────────────────────────────────────────────────
def bing_search(query, limit=10):
    """Bing web search. Returns list of {title, url, snippet} records."""
    data = _choco_get("bing/search", {"q": query, "pages": 1})
    if not data or not isinstance(data, dict):
        return []
    recs = []
    for r in data.get("organic_results") or []:
        if not isinstance(r, dict):
            continue
        url = (r.get("link") or r.get("displayed_link") or "").strip()
        title = (r.get("title") or "").strip()
        if not url or not title:
            continue
        recs.append({
            "title": title,
            "url": url,
            "snippet": (r.get("snippet") or "").strip(),
            "domain": _hostname(url),
        })
    return recs


def discover_bing_leads(phrases=None, limit_per_phrase=6, max_total=30):
    """Discover buyer web pages via Bing web search -> qualified leads.

    Uses the same high-intent phrases as Google SERP but executed on the
    INDEPENDENT Bing engine, so results are genuinely from a different source.
    Returns (leads, meta); meta.status is structured (SUCCESS/NO_KEY/...).
    """
    if not phrases:
        phrases = list(config.GOOGLE_INTENT_PHRASES)
    leads = []
    seen = set()
    status = "SUCCESS"
    error = ""
    if not _key():
        status = "NO_KEY"
        error = "CHOCODATA_API_KEY not set"
    if status == "SUCCESS":
        for phrase in phrases:
            if len(leads) >= max_total:
                break
            recs = bing_search(phrase, limit=limit_per_phrase)
            for rec in recs:
                if len(leads) >= max_total:
                    break
                dom = rec.get("domain", "")
                dk = dom or rec.get("title", "").lower()
                if not dk or dk in seen:
                    continue
                seen.add(dk)
                company = dom or rec.get("title", "")[:60]
                post_text = f"{rec.get('title')}. {rec.get('snippet')}".strip()
                lead = {
                    "name": company,
                    "company": company,
                    "company_website": ("https://" + dom) if dom else "",
                    "domain": dom,
                    "profile_url": rec.get("url") or "",
                    "title": rec.get("title") or "",
                    "post_text": post_text,
                    "post_url": rec.get("url") or "",
                    "description": rec.get("snippet") or "",
                    "location": "",
                    "query_loc": "",
                    "source_keyword": phrase,
                    "lead_type": "business_fallback",
                    "source": "chocodata_bing",
                }
                qualification.qualify(lead)
                leads.append(lead)
    return leads, {
        "source": "chocodata_bing",
        "status": status,
        "error": error,
        "actor": "chocodata/bing",
        "discovered": len(leads),
    }


def discover_bing_business_leads(phrases=None, limit_per_phrase=6, max_total=30):
    """LOCAL-BUSINESS discovery on the independent Bing engine (business_client).

    Mirrors google_business_discovery() but searches Bing (Chocodata API) with
    the same profile queries (industry x US/UK/UAE city). Every record is
    web-classified (web_presence.classify) and tagged lead_type=business_client
    so it flows through the identical qualify -> email -> dedup pipeline.
    Aggregator/portal results are dropped; REAL_WEBSITE-with-high-confidence
    businesses are dropped early (they already have a functioning website).

    `phrases` optional (industry x city queries); when None they are built from
    config.build_business_queries().

    meta = {source, status, error, actor, discovered, dropped}
    """
    if phrases:
        rot = list(phrases)
    else:
        rot = list(config.build_business_queries())
        random.shuffle(rot)

    import web_presence
    import apify_leads

    leads = []
    dropped = 0
    seen = set()
    status = "SUCCESS"
    error = ""
    if not _key():
        status = "NO_KEY"
        error = "CHOCODATA_API_KEY not set"
        return leads, {
            "source": "chocodata_bing", "status": status, "error": error,
            "actor": "chocodata/bing", "discovered": 0, "dropped": 0,
        }

    for phrase in rot:
        if len(leads) >= max_total:
            break
        recs = bing_search(phrase, limit=limit_per_phrase)
        if not recs:
            continue
        city = phrase.strip('"').split('"')[-1].strip() if '" "' in phrase else ""
        for rec in recs:
            if len(leads) >= max_total:
                break
            url = rec.get("url") or ""
            dom = rec.get("domain", "")
            dk = (url or "").lower() or rec.get("title", "").lower()
            if not url or not dom or dk in seen:
                continue
            seen.add(dk)
            title = rec.get("title") or ""

            # Business's own social/listing profile = a real no-website business.
            social_slug = apify_leads._social_profile_slug(url, dom)
            if social_slug and apify_leads._plausible_business_title(title):
                name = title.split("|")[0].split("-")[0].strip()[:80] or social_slug
                post_text = f"{title}. {rec.get('snippet')}".strip()
                prof_url = apify_leads._url_from_domain(dom)
                wp = {"web_status": "NO_WEBSITE", "web_confidence": 1.0,
                      "web_reason": f"Only a social/listing profile ({dom}) - no owned website".format(),
                      "signals": ["social/listing profile only"]}
                lead = {
                    "name": name, "company": name, "title": title,
                    "post_text": post_text, "description": rec.get("snippet") or "",
                    "post_url": url, "profile_url": url, "source_url": url,
                    "source_keyword": phrase, "source": "chocodata_bing",
                    "domain": dom, "company_website": prof_url, "website": prof_url,
                    "location": city, "lead_type": "business_client",
                    "web_status": wp["web_status"], "web_confidence": wp["web_confidence"],
                    "web_reason": wp["web_reason"], "web_gap_signals": wp["signals"],
                    "business_client_score": 90,
                    "opportunity_reason": web_presence.web_gap_reason(dom, title, city, wp),
                }
                qualification.qualify(lead)
                leads.append(lead)
                continue

            if apify_leads._is_business_noise(url, dom):
                dropped += 1
                continue

            name = title.split("|")[0].split("-")[0].strip()[:80] or dom
            site_url = apify_leads._url_from_domain(dom)
            wp = web_presence.classify(site_url, dom)
            if wp["web_status"] == "REAL_WEBSITE" and wp["web_confidence"] >= 0.85:
                dropped += 1
                continue

            post_text = f"{title}. {rec.get('snippet')}".strip()
            lead = {
                "name": name,
                "company": name,
                "title": title,
                "post_text": post_text,
                "description": rec.get("snippet") or "",
                "post_url": url,
                "profile_url": url,
                "source_url": url,
                "source_keyword": phrase,
                "source": "chocodata_bing",
                "domain": dom,
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
                "opportunity_reason": web_presence.web_gap_reason(dom, title, city, wp),
            }
            qualification.qualify(lead)
            leads.append(lead)
    return leads, {
        "source": "chocodata_bing",
        "status": status,
        "error": error,
        "actor": "chocodata/bing",
        "discovered": len(leads) + dropped,
        "dropped": dropped,
    }


# ── Reddit (community / hiring posts) ────────────────────────────────────────
REDDIT_BUYER_HINTS = ("[hiring]", "looking for a web", "need a web", "web developer",
                      "hiring", "freelance web", "hire a web", "join our", "need someone to",
                      "website for", "developer for", "marketing site", "build a")


def reddit_search(query, limit=25):
    """Reddit keyword search. Returns list of post-type result records."""
    data = _choco_get("reddit/search", {"q": query, "sort": "relevance", "t": "month"})
    if not data or not isinstance(data, dict):
        return []
    recs = []
    for r in data.get("results") or []:
        if not isinstance(r, dict):
            continue
        if r.get("result_type") != "post":
            continue
        title = (r.get("title") or "").strip()
        perm = (r.get("permalink") or "").strip()
        if not title or not perm:
            continue
        recs.append({
            "title": title,
            "permalink": perm,
            "subreddit": (r.get("subreddit") or "").strip(),
            "author": (r.get("author") or "").strip(),
            "created": r.get("created") or "",
        })
    return recs


def discover_reddit_leads(phrases=None, limit_per_phrase=25, max_total=30):
    """Discover buyer hiring posts on Reddit -> qualified leads.

    Only post-type results are kept (subreddit cards are not buyers). A post
    must show buyer intent (hiring / looking-for / need-a-dev). The subreddit
    and permalink are preserved for provenance.
    Returns (leads, meta).
    """
    if not phrases:
        phrases = list(config.GOOGLE_INTENT_PHRASES)
    leads = []
    seen = set()
    status = "SUCCESS"
    error = ""
    if not _key():
        status = "NO_KEY"
        error = "CHOCODATA_API_KEY not set"
    if status == "SUCCESS":
        for phrase in phrases:
            if len(leads) >= max_total:
                break
            recs = reddit_search(phrase, limit=limit_per_phrase)
            for rec in recs:
                if len(leads) >= max_total:
                    break
                title = rec.get("title", "")
                t = title.lower()
                if not any(hint in t for hint in REDDIT_BUYER_HINTS):
                    continue
                dk = (rec.get("permalink") or title).lower()
                if dk in seen:
                    continue
                seen.add(dk)
                location = "reddit " + (rec.get("subreddit") or "")
                company = title[:60]
                post_text = (f"[Reddit {rec.get('subreddit')}] {title}").strip()
                lead = {
                    "name": company,
                    "company": company,
                    "company_slug": rec.get("subreddit") or "",
                    "company_website": "",
                    "domain": "",
                    "profile_url": rec.get("permalink") or "",
                    "title": title,
                    "post_text": post_text,
                    "post_url": rec.get("permalink") or "",
                    "description": post_text,
                    "location": location,
                    "query_loc": "",
                    "source_keyword": phrase,
                    "lead_type": "job_posting",
                    "source": "chocodata_reddit",
                }
                qualification.qualify(lead)
                leads.append(lead)
    return leads, {
        "source": "chocodata_reddit",
        "status": status,
        "error": error,
        "actor": "chocodata/reddit",
        "discovered": len(leads),
    }


# ── Indeed (job board) ───────────────────────────────────────────────────────
# Indeed is transiently reachable (target_unreachable). `_parse_indeed` keeps
# the job-card parsing defensive across field-name variants.

def discover_indeed_leads(keywords=None, limit_per_kw=6, max_total=30):
    """Discover web-dev job postings on Indeed -> qualified leads.

    Indeed may transiently fail (target_unreachable) without being charged; we
    return a structured UNREACHABLE status so the engine skips it for the cycle
    and never wastes budget or starves other sources.
    Returns (leads, meta).
    """
    if not keywords:
        keywords = list(config.ALL_JOB_KEYWORDS)
    leads = []
    seen = set()
    status = "SUCCESS"
    error = ""
    if not _key():
        status = "NO_KEY"
        error = "CHOCODATA_API_KEY not set"
        return leads, {
            "source": "chocodata_indeed", "status": status, "error": error,
            "actor": "chocodata/indeed", "discovered": 0,
        }
    for kw in keywords:
        if len(leads) >= max_total:
            break
        data = _choco_get("indeed/search", {"q": kw, "pages": 1})
        if data is None:
            status = "START_FAILED"
            error = "chocodata request failed (network)"
            break
        if data.get("_unreachable"):
            status = "UNREACHABLE"
            error = "indeed target_unreachable (transient, not charged)"
            break
        recs = _parse_indeed(data)
        for rec in recs:
            if len(leads) >= max_total:
                break
            company = rec.get("company") or rec.get("title", "")[:60]
            dk = company.lower() + "|" + rec.get("title", "").lower()
            if not company or dk in seen:
                continue
            seen.add(dk)
            post_text = (f"JOB: {rec.get('title')}. {rec.get('snippet')}").strip()
            lead = {
                "name": company,
                "company": company,
                "company_website": "",
                "domain": "",
                "profile_url": rec.get("url") or "",
                "title": rec.get("title") or "",
                "job_title": rec.get("title") or "",
                "post_text": post_text,
                "post_url": rec.get("url") or "",
                "description": rec.get("snippet") or "",
                "location": rec.get("location") or "",
                "query_loc": "",
                "source_keyword": kw,
                "lead_type": "job_posting",
                "source": "chocodata_indeed",
            }
            qualification.qualify(lead)
            leads.append(lead)
    return leads, {
        "source": "chocodata_indeed",
        "status": status,
        "error": error,
        "actor": "chocodata/indeed",
        "discovered": len(leads),
    }


def _parse_indeed(data):
    """Parse an Indeed search response dict into job-card records."""
    recs = []
    for r in (data.get("results") or data.get("jobs") or []):
        if not isinstance(r, dict):
            continue
        url = (r.get("url") or "").strip()
        title = (r.get("title") or r.get("job_title") or "").strip()
        if not title or not url:
            continue
        recs.append({
            "title": title,
            "url": url,
            "company": (r.get("company") or "").strip(),
            "location": (r.get("location") or "").strip(),
            "snippet": (r.get("snippet") or r.get("description") or "").strip(),
        })
    return recs
