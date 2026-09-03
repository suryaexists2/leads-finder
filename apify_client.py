"""
Thin Apify REST client + daily spend tracker.

FREE plan = US$5/month wallet. We strictly cap daily spend at
config.MAX_DAILY_APIFY_SPEND and page per-actor daily caps so the wallet
never exhausts mid-month. Cheap sources first; pricey actors last.

Actors used:
  apify/google-search-scraper            (intent discovery, ~$0.00105/result)
  harvestapi/linkedin-company-search     (company fallback, start $0.001 + $0.002/company)
  harvestapi/linkedin-company-employees  (decision-maker fallback, start $0.02 + $0.004/short)
"""
import json
import os
import time
import threading
from datetime import datetime
from urllib.parse import quote, urlencode

import config
from curl_cffi import requests as cr

SPEND_FILE = os.path.join(config.DATA_DIR, "apify_spend.json")
CACHE_DIR = os.path.join(config.DATA_DIR, "apify_cache")
APIFY_LOG_FILE = os.path.join(config.DATA_DIR, "apify_client.log")

AUTH_BASE = "https://api.apify.com/v2"
_lock = threading.Lock()


def _log(msg):
    """Structured Apify activity log (actor name, status, counts, errors)."""
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    try:
        with open(APIFY_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line)


# ─── spend state ────────────────────────────────────────────────────────────

def _default_spend():
    return {"date": _today(), "total": 0.0, "sources": {}}


def _today():
    return time.strftime("%Y-%m-%d")


def load_spend():
    if os.path.exists(SPEND_FILE):
        try:
            with open(SPEND_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            if d.get("date") == _today():
                return d
        except Exception:
            pass
    return _default_spend()


def _save_spend(spend):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(SPEND_FILE, "w", encoding="utf-8") as f:
        json.dump(spend, f, indent=2)


def spend_today():
    return load_spend()


def can_spend(estimated_usd):
    """True if spending `estimated_usd` keeps us under the daily cap."""
    d = spend_today()
    return d.get("total", 0.0) + estimated_usd <= config.MAX_DAILY_APIFY_SPEND + 1e-6


def record_spend(source, usd):
    """Add a real measured cost (from run usageTotalUsd) to the daily tracker."""
    with _lock:
        d = load_spend()
        d["total"] = round(d.get("total", 0.0) + usd, 6)
        d["sources"][source] = round(d["sources"].get(source, 0.0) + usd, 6)
        d["date"] = _today()
        _save_spend(d)
    return d


# ─── actor run helpers ──────────────────────────────────────────────────────

def _token():
    return config.get_api_keys().get("APIFY", "")


def _run_actor(actor_id, run_input, max_wait=240, timeout_ms=120000,
               estimate_usd=0.0, min_budget_safety=True):
    """Start an Apify actor and return its result records.

    CORRECT v2 API CONTRACT (verified against live Apify docs + a real run):
        POST /v2/actors/{actorId}/runs?token=...&maxItems=N&timeout=S
        Body  = the actor INPUT passed DIRECTLY as the JSON request body.
        The Apify v2 API does NOT accept an `{"input": {...}}` wrapper — doing
        so caused every actor run to fail with `invalid-input: Field input.X
        is required` (the schema validator saw the real fields missing).
        Run-level options (maxItems/maxTotalChargeUsd/memory/timeout) are URL
        query parameters, NOT body fields.

    Returns (status, items, cost, error):
        status : 'SUCCESS' | 'START_FAILED' | 'HTTP_4XX' | 'TIMEOUT'
                 | 'ACTOR_FAILED' | 'BUDGET' | 'NO_KEY'
        items  : list of raw result records (on SUCCESS) else []  (never a
                 misleading [] for a real failure — callers see `status`)
        cost   : float USD actually charged this run
        error  : short human reason ('' on SUCCESS)

    Result retrieval reads the inline `result` first (some actors return data
    inline), then falls back to `defaultDatasetId` — see _extract_run_items.

    The existing daily $0.15 Apify budget guard is preserved (min_budget_safety).
    No uncontrolled retries happen inside this function — a failed run burns
    quota and stops; callers decide whether a NEW run is affordable.
    """
    if min_budget_safety and not can_spend(estimate_usd):
        _log(f"[{actor_id}] BUDGET: skipped (est ${estimate_usd:.4f} > remaining)")
        return "BUDGET", [], 0.0, "daily Apify spend cap reached"
    tok = _token()
    if not tok:
        _log(f"[{actor_id}] NO_KEY")
        return "NO_KEY", [], 0.0, "APIFY_API_TOKEN not set"

    scenic = actor_id.replace("/", "~")
    qp = {"token": tok}
    # `maxItems` is BOTH a harvestapi actor-input field (bounds how many results
    # the actor actually scrapes — REQUIRED to stop LinkedIn unbounded scraping)
    # AND a run-level query option (caps what we are CHARGED for). We keep it in
    # the body so the actor does bounded work, and ALSO pass it as a query param
    # so we are never charged for more results than requested.
    mi = run_input.get("maxItems") or 0
    if mi:
        qp["maxItems"] = int(mi)
    # Hard USD charge cap for pay-per-event actors so a runaway run can never
    # blow the daily budget guard even if our estimate was off. Apify enforces a
    # $0.50 MINIMUM for maxTotalChargeUsd, so we floor at $0.50 (this is a per
    # -run CEILING, not a spend; the daily `can_spend` guard keeps total spend
    # within config.MAX_DAILY_APIFY_SPEND and `maxItems` caps charged results).
    qp["maxTotalChargeUsd"] = 0.50
    qp["timeout"] = max_wait
    try:
        start = cr.post(f"{AUTH_BASE}/actors/{scenic}/runs",
                        params=qp,
                        json=run_input,
                        timeout=timeout_ms)
    except Exception as e:
        _log(f"[{actor_id}] START_FAILED: {e}")
        return "START_FAILED", [], 0.0, f"start failed: {e}"

    if start.status_code >= 400:
        _log(f"[{actor_id}] HTTP_{start.status_code}: {start.text[:200]}")
        status = "HTTP_4XX" if start.status_code < 500 else "START_FAILED"
        return status, [], 0.0, f"start http {start.status_code}: {start.text[:200]}"

    run_id = start.json().get("data", {}).get("id")
    if not run_id:
        _log(f"[{actor_id}] START_FAILED: no run id returned")
        return "START_FAILED", [], 0.0, "no run id returned"

    deadline = time.time() + max_wait
    while time.time() < deadline:
        time.sleep(3)
        try:
            # CORRECT v2 "Get run" endpoint is /v2/actor-runs/{runId}. The
            # legacy /v2/runs/{runId} returns 404, which silently made every
            # poll loop time out even though the actor run had actually
            # SUCCEEDED — the root cause of the pervasive "run wait timed out".
            st = cr.get(f"{AUTH_BASE}/actor-runs/{run_id}",
                        params={"token": tok}, timeout=60000)
        except Exception as e:
            _log(f"[{actor_id}] poll error: {e}")
            continue
        if st.status_code != 200:
            continue
        data = st.json().get("data") or {}
        status_s = data.get("status")
        if status_s == "SUCCEEDED":
            cost = (data.get("usageTotalUsd") or 0.0) or 0.0
            record_spend(actor_id.split("/")[-1], cost)
            items = _extract_run_items(data)
            _log(f"[{actor_id}] SUCCESS items={len(items)} cost=${cost:.4f}")
            return "SUCCESS", items, cost, ""
        if status_s in ("FAILED", "TIMED-OUT", "ABORTED"):
            cost = (data.get("usageTotalUsd") or 0.0) or 0.0
            if cost > 0:
                record_spend(actor_id.split("/")[-1], cost)
            _log(f"[{actor_id}] ACTOR_FAILED ({status_s}) cost=${cost:.4f}: "
                 f"{(data.get('statusMessage') or '')[:200]}")
            return "ACTOR_FAILED", [], cost, f"actor {status_s}: {data.get('statusMessage')}"
    _log(f"[{actor_id}] TIMEOUT after {max_wait}s")
    return "TIMEOUT", [], 0.0, "run wait timed out"


def _extract_run_items(d):
    """Pull result records from a succeeded run.

    1) RUN-mode inline `result` (list, or dict holding a list) — read first so
       RUN-mode never returns empty because defaultDatasetId is null.
    2) else fetch from `defaultDatasetId` -> /datasets/{id}/items.
    Returns a list. Never pretends a real result is empty; logs if a dataset
    was expected but returned 0 items.
    """
    tok = _token()
    inline = d.get("result")
    if inline:
        if isinstance(inline, list):
            if inline:
                return inline
        elif isinstance(inline, dict):
            if inline.get("results") and isinstance(inline["results"], list):
                return inline["results"]
            for v in inline.values():
                if isinstance(v, list) and v:
                    return v
    dsid = d.get("defaultDatasetId")
    if dsid:
        items = _fetch_dataset_items(dsid, tok)
        if items:
            return items
        _log("run SUCCEEDED but dataset fetch returned 0 items")
    return []


def _fetch_dataset_items(dataset_id, tok, limit=500):
    if not dataset_id:
        return []
    try:
        r = cr.get(f"{AUTH_BASE}/datasets/{dataset_id}/items",
                   params={"token": tok, "limit": limit}, timeout=60000)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return []


# ─── typed wrapper methods ──────────────────────────────────────────────────

def google_serp_search(phrase, num_results=10, **overrides):
    """Run one Google SERP intent query.

    Returns (status, records, error):
        status  : structured _run_actor status (SUCCESS/START_FAILED/HTTP_4XX/
                  TIMEOUT/ACTOR_FAILED/BUDGET/NO_KEY) — never silently [])
        records : normalized top-level {title, url, description, domain} list
                  (organicResults flattened) on SUCCESS, else []
        error   : reason/''
    """
    run_input = {"queries": phrase, "maxPagesPerQuery": 1}
    run_input.update(overrides)
    status, items, cost, err = _run_actor(
        "apify/google-search-scraper", run_input,
        max_wait=180, estimate_usd=num_results * 0.0012 + 0.002,
    )
    records = []
    if status == "SUCCESS":
        for it in items or []:
            if not isinstance(it, dict):
                continue
            organic = it.get("organicResults")
            if isinstance(organic, list):
                for r in organic:
                    rec = _norm_google_record(r)
                    if rec:
                        records.append(rec)
            else:
                rec = _norm_google_record(it)
                if rec:
                    records.append(rec)
        if not records:
            _log(f"[google-search-scraper] SUCCESS but 0 organic results parsed "
                 f"({len(items or [])} items)")
    return status, records, err


def _norm_google_record(r):
    """Normalize one Google organic result to the internal record shape.

    Includes `domain` so apify_leads can build company_website for the email
    waterfall (google results themselves carry no company/website field).
    """
    url = (r.get("url") or r.get("link") or "").strip()
    title = (r.get("title") or "").strip()
    if not url or not title:
        return None
    domain = _hostname(url)
    return {
        "title": title,
        "url": url,
        "description": (r.get("description") or r.get("snippet") or "").strip(),
        "domain": domain,
    }


def _norm_maps_record(r):
    """Normalize one Google Maps / Google Business listing (compass
    crawler-google-places output).

    Maps listings carry the STRUCTURED business data SERP organic results don't:
    name, categoryName, address/city, phone, rating (totalScore), reviewsCount,
    and above all the `website` field — whose ABSENCE/EMPTY value is the
    PRIMARY no-website signal (the listing officially declares no website).
    The listing `url` is a google.com/maps/... link and must NOT be treated as
    a business website.
    """
    title = (r.get("title") or "").strip()
    if not title:
        return None
    website = r.get("website")
    if not isinstance(website, str):
        website = ""
    website = website.strip()
    rating = r.get("totalScore") or r.get("rating")
    try:
        rating = float(str(rating).replace(",", ".").strip()) if rating not in (None, "") else None
    except Exception:
        rating = None
    reviews = r.get("reviewsCount") or r.get("reviews") or 0
    try:
        reviews = int(str(reviews).replace(",", "").strip())
    except Exception:
        reviews = 0
    address = (r.get("address") or "").strip()
    city = (r.get("city") or "").strip()
    if not city and (r.get("neighborhood") or address):
        city = (r.get("neighborhood") or "").strip()
    addr_parts = [s for s in (r.get("street"), r.get("postalCode")) if isinstance(s, str) and s.strip()]
    if addr_parts:
        address = ", ".join(addr_parts)
    return {
        "title": title,
        "url": (r.get("url") or "").strip(),
        "category": (r.get("categoryName") or r.get("category") or "").strip(),
        "address": address,
        "city": city,
        "phone": (r.get("phone") or r.get("phoneUnformatted") or "").strip(),
        "rating": rating,
        "reviews": reviews,
        "website": website,
        "domain": _hostname(website) if website else "",
        "state": (r.get("state") or "").strip(),
        "country_code": (r.get("countryCode") or "").strip(),
        "maps_link": (r.get("url") or "https://www.google.com/maps/search/?api=1&query={0}".format(quote(title))).strip(),
    }


def google_maps_search(phrase, num_results=6, **overrides):
    """Run ONE Google Maps / Google Business listing query (compass
    crawler-google-places actor).

    `phrase` = "<category> in <city>" style search string. Returns up to
    `num_results` real Google Business listings per query (maxCrawledPlacesPerSearch),
    so a single call can yield several leads. The listing's `website` field is the
    PRIMARY channel signal: an EMPTY value means Google lists the business with
    no website -> ideal new-website prospect.

    Returns (status, records, error):
        records : normalized Maps listings ({title, category, address, city,
                  phone, rating, reviews, website, domain, maps_link}) on
                  SUCCESS, else [].
    """
    run_input = {
        "searchStringsArray": [phrase],
        "maxCrawledPlacesPerSearch": max(1, int(num_results or 6)),
        "maxReviews": 0,
        "language": "en",
    }
    run_input.update(overrides)
    from budget import est_maps
    status, items, cost, err = _run_actor(
        "compass/crawler-google-places", run_input,
        max_wait=240, estimate_usd=est_maps(num_results),
    )
    records = []
    if status == "SUCCESS":
        for it in items or []:
            if not isinstance(it, dict):
                continue
            rec = _norm_maps_record(it)
            if rec:
                records.append(rec)
        if not records:
            _log(f"[crawler-google-places] maps SUCCESS but 0 listings parsed "
                 f"({len(items or [])} items)")
    return status, records, err


def _hostname(url):
    """Best-effort hostname from a URL, e.g. example.com."""
    try:
        url = url.strip()
        if not url:
            return ""
        if not url.startswith("http"):
            url = "https://" + url
        from urllib.parse import urlparse
        host = urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        return host.split(":")[0]
    except Exception:
        return ""


def company_search(industries, limit=25, locations=None):
    """LinkedIn company search by industry IDs.

    Uses harvestapi/linkedin-company-search. Verified actor input schema:
      scraperMode (short|full), maxItems (int), searchQuery (str),
      locations (array), industryIds (array), companySize (array),
      startPage/takePages. No required fields.

    We always send bounds so the run is cheap and finishes fast:
      - scraperMode='short'  (cheapest; avoids the unbounded FULL default that
                              used to time out because input was also mis-posted)
      - maxItems=limit       bounds results AND charge cap
      - industryIds          correct field name (NOT `industries`)
      - locations (optional) for foreign-only targeting

    Returns (status, records, error); records normalized to
    {name, url, company_slug, website, industry, location, domain} on SUCCESS.
    """
    run_input = {"scraperMode": "short", "maxItems": limit}
    if industries:
        run_input["industryIds"] = industries
    if locations:
        run_input["locations"] = locations
    status, items, cost, err = _run_actor(
        "harvestapi/linkedin-company-search", run_input,
        max_wait=120, estimate_usd=0.001 * limit + 0.005,
    )
    records = []
    if status == "SUCCESS":
        for it in items or []:
            if not isinstance(it, dict):
                continue
            rec = _norm_company(it)
            if rec:
                records.append(rec)
        if not records:
            _log(f"[linkedin-company-search] SUCCESS but 0 company records parsed "
                 f"({len(items or [])} items)")
    return status, records, err


def _norm_company(it):
    """Normalize one LinkedIn company-search record to internal record shape."""
    url = (it.get("url") or it.get("companyUrl") or "").strip()
    name = (it.get("name") or "").strip()
    if not name:
        if url:
            name = url.rstrip("/").split("/")[-1].replace("-", " ").title()
        else:
            return None
    website = (it.get("website") or "")
    if not isinstance(website, str):
        website = ""
    website = website.strip()
    return {
        "name": name,
        "url": url,
        "company_slug": url.rstrip("/").split("/")[-1].lower() if url else "",
        "website": website,
        "domain": _hostname(website),
        "industry": (it.get("industry") or "") if isinstance(it.get("industry"), str) else "",
        "location": (it.get("location") or "") if isinstance(it.get("location"), str) else "",
    }


def company_employees_short(company_url_or_slug, max_profiles=3):
    """Employees actor SHORT mode (enrichment only; not a discovery source).

    Returns (status, items, error) with the new structured status. Only reached
    when enrichment is enabled (allow_enrich=True); Short mode does NOT return
    emails, it only adds a decision-maker name.
    """
    run_input = {
        "companies": [company_url_or_slug],
        "profileScraperMode": "Short ($4 per 1k)",
        "maxItems": max_profiles,
    }
    est = max_profiles * 0.004 + 0.02
    status, items, cost, err = _run_actor(
        "harvestapi/linkedin-company-employees", run_input,
        max_wait=240, estimate_usd=est,
    )
    return status, items, err
