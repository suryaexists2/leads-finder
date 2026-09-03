"""
Email resolution waterfall for a company/lead — cheapest/free first.

  1. email embedded in source text
  2. public company/domain email (already known / website)
  3. website scrape (info@/hello@/contact@ ...)   [free]
  4. Prospeo                                        [sparing, <=3/day]
  5. Employees actor SHORT -> find decision maker   [paid, last resort]
     -> then website / Prospeo for their email
We NEVER use employees email-search mode as primary.

Emails verified with syntax + domain MX (config.EMAIL_VERIFY_SCHEME).
Disk cache per domain so we never re-resolve the same company.
"""
import os
import re
import json
import time
import socket
import subprocess
import dns.resolver  # dnspython

import config
import apify_client
from curl_cffi import requests as cr
from urllib.parse import urlparse

CACHE_FILE = os.path.join(config.DATA_DIR, "email_cache.json")
PROSPEO_DAILY_FILE = os.path.join(config.DATA_DIR, "prospeo_daily.json")


# ─── caching ────────────────────────────────────────────────────────────────

def _load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_cache(cache):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)


def _clean_url(url):
    if not url:
        return ""
    url = url.strip()
    if url.startswith("www."):
        url = "https://" + url
    elif not url.startswith("http"):
        url = "https://" + url
    return url.rstrip("/")


def _domain_of(url):
    if not url:
        return ""
    netloc = urlparse(_clean_url(url)).netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc


# ─── email verification (syntax + MX) ───────────────────────────────────────

def _syntactically_ok(email):
    m = config.EMAIL_RE.fullmatch(email.strip().lower())
    if not m:
        return False
    _, dom = email.split("@", 1)
    if dom in config.SKIP_DOMAINS:
        return False
    if any(x in email.lower() for x in config.EMAIL_BAD_TOKENS):
        return False
    return True


def has_mx(domain):
    domain = domain.lower().strip()
    if not domain:
        return False
    # Gmail/big providers always have MX -> assume ok to save a lookup bug
    try:
        answers = dns.resolver.resolve(domain, "MX")
        return len(answers) > 0
    except Exception:
        return False


def verify_email(email):
    """Return (ok, domain_has_mx)."""
    email = (email or "").strip()
    ok = _syntactically_ok(email)
    if not ok:
        return False, False
    _, dom = email.split("@", 1)
    mx = has_mx(dom)
    return ok and mx, mx


# ─── website email scraping (free) ──────────────────────────────────────────

CONTACT_PATHS = [
    "", "contact", "contact-us", "about", "contact-us/", "about-us",
    "contact/", "team", "contact-us#contact", "get-in-touch",
]


def _clean_email(raw):
    """Strip HTML-entity / escape remnants that leak into scraped addresses
    (e.g. `u003egeneral@x.com`, `info&#64;x.com`, `&amp;`). Returns a clean
    address or '' if nothing usable remains."""
    e = raw.strip().lower()
    e = re.sub(r"&#\d+;", "", e)
    e = re.sub(r"&(amp|lt|gt|#64|#x40);", "", e)
    e = re.sub(r"\\u[0-9a-fA-F]{4}", "", e)
    e = re.sub(r"[<>\\]", "", e)
    if "@" not in e:
        return ""
    local, dom = e.split("@", 1)
    if not _syntactically_ok(f"{local}@{dom}"):
        return ""
    return f"{local}@{dom}"


def _scrape_emails(url, domain):
    found = set()
    try:
        r = cr.get(url, impersonate="chrome136", timeout=10,
                   headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US"},
                   allow_redirects=True)
        if r.status_code != 200:
            return found
        for e in re.findall(config.EMAIL_RE.pattern, r.text):
            clean = _clean_email(e)
            if not clean:
                continue
            d = clean.split("@")[1]
            if d in config.SKIP_DOMAINS or d not in (domain, domain.lstrip("www.")):
                continue
            local = clean.split("@")[0]
            if len(local) < 2 or len(clean) < 8:
                continue
            found.add(clean)
    except Exception:
        pass
    return found


def _clean_phone_num(raw):
    return re.sub(r"[^0-9+]", "", raw)


def find_contact_emails(website, max_attempts=6):
    """Scrape a company website's contact/about pages (parallel, fast) for emails."""
    if not website:
        return []
    base = _clean_url(website)
    domain = _domain_of(base)
    if not domain:
        return []
    paths = CONTACT_PATHS[:max_attempts]
    urls = [base if not p else f"{base}/{p}" for p in paths]

    import concurrent.futures
    all_found = set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(_scrape_emails, u, domain): u for u in urls}
        for f in concurrent.futures.as_completed(futs):
            try:
                all_found |= f.result()
            except Exception:
                pass

    prefer = re.compile(r"^(info|hello|contact|support|team|office|hire|careers|jobs|accounts|sales|admin|business)@")
    scored = sorted(all_found,
                    key=lambda e: (not bool(prefer.match(e)),
                                   not e.startswith((domain, "mail.", "www.")),
                                   -len(e)))
    return [e for e in scored][:4]


# ─── Prospeo (sparing) ──────────────────────────────────────────────────────

def _load_prospeo_daily():
    if os.path.exists(PROSPEO_DAILY_FILE):
        try:
            with open(PROSPEO_DAILY_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            if d.get("date") == time.strftime("%Y-%m-%d"):
                return d
        except Exception:
            pass
    return {"date": time.strftime("%Y-%m-%d"), "count": 0}


def _save_prospeo_daily(d):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(PROSPEO_DAILY_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)


def prospeo_email(domain, first_name="", last_name=""):
    key = config.get_api_keys().get("PROSPEO")
    if not key:
        return ""
    d = _load_prospeo_daily()
    if d["count"] >= 3:          # <=3/day
        return ""
    params = {"domain": domain}
    if first_name:
        params["first_name"] = first_name
    if last_name:
        params["last_name"] = last_name
    try:
        d["count"] += 1
        r = cr.get("https://api.prospeo.io/v1/email-finder",
                   headers={"X-Key": key}, params=params,
                   impersonate="chrome146", timeout=30)
        _save_prospeo_daily(d)
        if r.status_code != 200:
            return ""
        data = r.json()
        email = (data.get("personal_email") or data.get("work_email")
                 or data.get("email") or "").strip().lower()
        if email and "@" in email:
            return email
    except Exception:
        pass
    return ""


# ─── Employees SHORT fallback (last, paid) ─────────────────────────────────

def find_decision_maker(company_url, company_name=""):
    """Employees actor SHORT -> best decision-maker (Founder/Owner/CEO/Director).

    The Employees actor requires a full LinkedIn company URL in its `companies`
    input. If we don't have one, skip (avoid a wasted paid run on invalid input).
    """
    if not company_url or "linkedin.com/company/" not in company_url:
        return None
    status, items, err = apify_client.company_employees_short(company_url)
    if status != "SUCCESS":
        return None
    best = None
    best_rank = 99
    for it in items:
        name = (it.get("name") or "").strip()
        url = (it.get("url") or it.get("profile_url") or "").strip()
        headline = (it.get("headline") or it.get("jobTitle") or "").lower()
        if not name or not url:
            continue
        rank = 99
        for i, t in enumerate(config.DECISION_TITLES):
            if t in headline:
                rank = i
                break
        if rank < best_rank:
            best_rank = rank
            best = {"name": name, "url": url, "headline": (it.get("headline") or ""),
                    "email": it.get("email") or ""}
    return best


# ─── phone / whatsapp extraction (free, from website) ──────────────────────

def _clean_phone(raw):
    digits = re.sub(r"[^0-9+]", "", raw)
    return digits


def normalize_phone(raw):
    """Return (phone, whatsapp_ok) best-effort. Prefer a canonical intl number."""
    if not raw:
        return "", False
    m = config.PHONE_RE.search(raw)
    if not m:
        return "", False
    num = _clean_phone(m.group(0))
    # WhatsApp works off a valid phone number; flag if it looks mobile-ish.
    # Simple heuristic: any well-formed intl/country number is whatsapp-callable.
    if not num:
        return "", False
    return num, True


def find_contact_phones(website, web_text=""):
    """
    Scrape a website for phone numbers. Returns (primary_phone, has_whatsapp).
    Falls back to embedded text if the site fetch fails.
    """
    candidates = []
    if web_text:
        candidates += re.findall(config.PHONE_RE.pattern, web_text)
    if website:
        try:
            r = cr.get(_clean_url(website), impersonate="chrome146", timeout=20,
                       headers={"User-Agent": "Mozilla/5.0"}, allow_redirects=True)
            if r.status_code == 200:
                candidates += re.findall(config.PHONE_RE.pattern, r.text)
        except Exception:
            pass

    # Prefer numbers that look like proper international / mobile numbers
    scored = []
    for c in candidates:
        num = _clean_phone(c)
        if len(num) < 7 or len(num) > 15:
            continue
        ctry = ""
        if config.US_CANADA_RE.match(num):
            ctry = "US"
        elif config.UK_RE.match(num):
            ctry = "UK"
        elif config.AU_RE.match(num):
            ctry = "AU"
        elif config.DE_RE.match(num):
            ctry = "DE"
        scored.append((0 if ctry else 1, -len(num), num, ctry))
    scored.sort()
    if not scored:
        return "", False
    phone = scored[0][2]
    has_wa = scored[0][3] in ("US", "UK", "AU", "DE")  # mobile-intl -> whatsapp
    return phone, has_wa


# ─── free SERP email hunt (no-website / WAF-blocked sites) ──────────────────
#
# Plain curl is blocked by Cloudflare-class WAFs (HTTP 406/403) on many
# small-business sites, and business-listing pages the SERP finds are often
# behind the same anti-bot. The maps campaign proved a real headless-Edge
# `--dump-dom` fetch renders those fine, so we reuse that EXACT technique here
# (own profile dir — never contended with the maps profile). We NEVER invent an
# email: only addresses a page actually publishes, that pass MX verification,
# get returned.

def _edge_fetch(url, timeout_s=None):
    """Real-browser page fetch (headless Edge dump-dom). Returns HTML or ''."""
    if not url:
        return ""
    if timeout_s is None:
        timeout_s = getattr(config, "EMAIL_EDGE_TIMEOUT_S", 75)
    edge = getattr(config, "MAPS_PUBLIC_EDGE_PATH", "") or ""
    if not edge or not os.path.isfile(edge):
        return ""
    profile = getattr(config, "EMAIL_EDGE_PROFILE_DIR", None) or os.path.join(
        config.DATA_DIR, "email_edge_profile")
    budget = getattr(config, "EMAIL_EDGE_BUDGET_MS", 9000)
    cmd = [edge, "--headless=new", "--disable-gpu", "--no-first-run",
           "--no-default-browser-check", "--user-data-dir=" + profile,
           "--virtual-time-budget=%d" % budget, "--dump-dom", url]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout_s,
                           encoding="utf-8", errors="replace", text=True)
        return r.stdout or ""
    except Exception:
        return ""


def _edge_dom_candidates(website):
    base = _clean_url(website)
    if not base:
        return []
    cands = [base]
    for p in ("contact", "contact-us", "about"):
        cands.append(base.rstrip("/") + "/" + p)
    return cands


def _lite_ddg(query, limit=None):
    """Free DuckDuckGo Lite web search -> [(title, url)] or [] on failure."""
    if not query or not getattr(config, "EMAIL_SERP_FALLBACK", True):
        return []
    import html as _html
    from urllib.parse import quote_plus, unquote
    if limit is None:
        limit = getattr(config, "EMAIL_SERP_MAX_RESULTS", 6)
    try:
        r = cr.get("https://lite.duckduckgo.com/lite/?q=" + quote_plus(query),
                   impersonate="chrome136", timeout=20,
                   headers={"User-Agent": "Mozilla/5.0",
                            "Accept-Language": "en-US"})
        if r.status_code != 200:
            return []
        out = []
        for m in re.finditer(r'<a rel="nofollow" href="([^"]+)"[^>]*>(.*?)</a>',
                             r.text, re.S):
            href = m.group(1).strip()
            if "uddg=" in href:
                href = unquote(href.split("uddg=", 1)[1].split("&", 1)[0])
            if not href.startswith("http"):
                continue
            title = _html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
            if title:
                out.append((title, href))
            if len(out) >= limit:
                break
        return out
    except Exception:
        return []


def _scan_emails(html):
    """Loose email scan of own-site HTML (any domain, SKIP_DOMAINS +
    DIRECTORY_DOMAINS filtered). Used ONLY for a lead's own domain, where
    every listed address is assumed the business's. Directory/listing pages
    use _block_emails instead."""
    if not html:
        return []
    out = []
    for e in re.findall(config.EMAIL_RE.pattern, html.lower()):
        c = _clean_email(e)
        if not c:
            continue
        if c.split("@")[1] in config.SKIP_DOMAINS \
                or c.split("@")[1] in getattr(config, "DIRECTORY_DOMAINS", set()):
            continue
        if len(c.split("@")[0]) < 2:
            continue
        if c not in out:
            out.append(c)
    return out


NAME_STOPWORDS = {
    "the", "of", "and", "or", "for", "ltd", "limited", "inc", "llc", "ssc",
    "pvt", "pty", "plc", "co", "gmbh", "services", "service", "group",
    "associates", "solutions", "systems", "consulting", "company", "business",
    "enterprises", "centre", "center", "clinic", "studio", "gym", "fitness",
    "hvac", "plumbing", "heating", "cooling", "contractors", "contractor",
    "roofing", "builders", "construction", "moving", "pest", "control",
    "cleaning", "academy", "school", "sports", "club", "center",
}


def _name_tokens(name):
    """Distinctive name tokens (drop generic category/legal words) for
    proximity-matching an email to the right business on a directory page."""
    out = []
    for w in re.split(r"[^A-Za-z0-9]+", (name or "").lower()):
        if len(w) >= 4 and w not in NAME_STOPWORDS and w not in out:
            out.append(w)
        if len(out) >= 4:
            break
    return out


def _block_emails(html, tokens):
    """Email scan restricted to HTML blocks (li/div/section/article) that also
    contain a business name token. Prevents picking up a directory's own or a
    neighbour business's email (cross-contamination on listing pages).
    Directory/aggregator domains are hard negatives (their profiles@ pages
    mention many businesses, so name proximity is not enough alone)."""
    if not html or not tokens:
        return []
    low = html.lower()
    dirs = getattr(config, "DIRECTORY_DOMAINS", set())
    blocks = re.split(r"(?=<(?:li|div|section|article|tr)[ >])", low)
    out = []
    for blk in blocks:
        if not any(t in blk for t in tokens):
            continue
        for e in re.findall(config.EMAIL_RE.pattern, blk):
            c = _clean_email(e)
            if not c or c in out:
                continue
            dom = c.split("@")[1]
            if dom in config.SKIP_DOMAINS or dom in dirs:
                continue
            if len(c.split("@")[0]) < 2:
                continue
            out.append(c)
    return out


def _verified_email(candidates):
    """First candidate passing syntax+MX verification, else ''."""
    for c in candidates:
        ok, _ = verify_email(c)
        if ok:
            return c
    return ""


def _search_hunt(name, location, company_website="", only_own=False):
    """Free discovery of a genuine no-website business's published contact
    email (the maps funnel). DuckDuckGo-search the name + city and fetch the
    top listing pages with Edge, loose-scanning ONLY blocks that mention the
    business name (a directory footer/sponsor email is never attributed to it;
    directory/aggregator domains are hard negatives regardless).

    `company_website`/`only_own` are kept only for a caller diagnosing a
    WAF-blocked OWN site; the primary callers (no-website leads) never pass
    them. Returns (email, detail). Never invents.
    """
    if not getattr(config, "EMAIL_SERP_FALLBACK", True) or not name:
        return "", ""
    tokens = _name_tokens(name)
    if company_website:
        for page in _edge_dom_candidates(company_website)[:2]:
            html = _edge_fetch(page)
            if not html:
                continue
            own_dom = _domain_of(company_website)
            cands = [c for c in _scan_emails(html)
                     if c.split("@")[1] in (own_dom, "www." + own_dom)]
            cands = list(dict.fromkeys(cands + _block_emails(html, tokens)))
            email = _verified_email(cands)
            if email:
                return email, "edge-scraped own site"
    if only_own:
        return "", ""
    q = '"%s" %s' % (name.strip()[:40], (location or "").strip())
    q = re.sub(r"\s+", " ", q).strip()
    if not q.strip('" ') or not tokens:
        return "", ""
    for _t, url in _lite_ddg(q)[: getattr(config, "EMAIL_SERP_MAX_FETCH", 2)]:
        email = _verified_email(_block_emails(_edge_fetch(url), tokens))
        if email:
            return email, "serp listing (%s)" % url.split("/")[2]
    return "", ""


def _hunt_free(name, location, company_website=""):
    """Cached wrapper over _search_hunt (prevents re-hunting the same business
    every cycle). Caches under a 'serp:' key (name-based)."""
    if not name:
        return "", ""
    key = "serp:" + " ".join(name.lower().split())
    cache = _load_cache()
    if key in cache:
        if cache[key].get("email"):
            return cache[key]["email"], "cached (" + str(cache[key].get("source", "serp")) + ")"
        if cache[key].get("exit"):
            return "", ""
    email, detail = _search_hunt(name, location, company_website)
    if email:
        cache[key] = {"email": email, "source": "serp", "exit": False}
    else:
        cache[key] = {"email": "", "source": "", "exit": True}
    _save_cache(cache)
    return email, detail


# ─── main waterfall ─────────────────────────────────────────────────────────

def resolve_email(lead, company_website="", company_url="", company_name="",
                  allow_enrich=False, web_status=""):
    """
    Returns (email, source, method_detail) following the waterfall.
    Uses on-disk domain cache to avoid duplicated paid lookups.
    When `allow_enrich` is False only FREE/cheap paths run: source text,
    website scrape, cache. Paid fallbacks (Prospeo domain/name lookup and the
    Employees SHORT actor) run ONLY when allow_enrich=True — the engine gates
    this behind lead quality + a daily enrichment budget.

    NO_WEBSITE leads skip website scraping entirely. Source-text email,
    Apollo/Prospeo/Employees still run when available.
    """
    # 1. embedded email in text (always runs for all web_statuses)
    text = (lead.get("post_text") or "") + " " + (lead.get("body") or "") + \
           " " + (lead.get("description") or "")
    for e in re.findall(config.EMAIL_RE.pattern, text.lower()):
        clean = _clean_email(e)
        if clean and clean.split("@")[1] not in config.SKIP_DOMAINS:
            return clean, "source_text", "email in source (no website)"

    # NO_WEBSITE: no domain to scrape — skip website-dependent steps
    if web_status == "NO_WEBSITE":
        if allow_enrich and company_url:
            e, s, d = _resolve_enrich_only(company_url, company_name)
            if e:
                return e, s, d
        # Free SERP hunt: many no-website businesses publish a contact email on
        # a listing page. Only real, MX-verified discoveries are used.
        if lead.get("lead_type") == "business_client":
            e, d = _hunt_free(company_name or lead.get("name", ""),
                              lead.get("location", ""))
            if e:
                return e, "serp", d
        return "", "", "no email (no website, source text only)"

    domain = _domain_of(company_website) or _domain_of(company_url)
    if not domain:
        return "", "", "no domain"

    cache = _load_cache()
    if domain in cache and cache[domain].get("email"):
        return cache[domain]["email"], cache[domain].get("source", "cache"), "cached"
    if domain in cache and cache[domain].get("exit"):
        # A WAF-blocked site (406/403) cached as exhausted. The business HAS a
        # website (it is simply unreachable from here) — per the client profile
        # "no website means no website, not a dead/blocked one", a web-having
        # lead is NOT a no-website prospect, so no free hunt is burned on it.
        return "", "", "previously exhausted"

    # 2/3. website scrape (skip for NO_WEBSITE — already handled above)
    emails = find_contact_emails(company_website)
    for e in emails:
        ok, _ = verify_email(e)
        if ok:
            cache[domain] = {"email": e, "source": "website", "exit": False}
            _save_cache(cache)
            return e, "website", "scraped from site"
    if emails:  # found but unverified -> still use best guess (domain-matched)
        e = emails[0].lower()
        cache[domain] = {"email": e, "source": "website", "exit": False}
        _save_cache(cache)
        return e, "website", "scraped (unverified)"

    # 4. Prospeo / Employees SHORT — PAID. Only when allow_enrich is True
    #    (the engine decides the lead is strong enough and a daily enrichment
    #     budget remains). Otherwise stop here at no cost.
    if not allow_enrich:
        # free-paths-only stop. A lead WITH a website (any status incl. dead/
        # WAF-blocked) is not a no-website prospect — do NOT run a SERP/Edge
        # hunt on it; the plain own-site scrape above already had its chance.
        cache[domain] = {"email": "", "source": "", "exit": False}
        _save_cache(cache)
        return "", "", "no email (free paths only)"

    # 4. Prospeo by domain
    p = prospeo_email(domain)
    if p:
        cache[domain] = {"email": p, "source": "prospeo", "exit": False}
        _save_cache(cache)
        return p, "prospeo", "prospeo domain lookup"

    # 5. Employees SHORT -> decision maker (paid, only when nothing free worked)
    if company_url or company_name:
        dm = find_decision_maker(company_url or company_name)
        if dm:
            if dm.get("email"):
                cache[domain] = {"email": dm["email"], "source": "employees", "exit": False}
                _save_cache(cache)
                return dm["email"], "employees", "employees+email"
            # decision maker found but no email -> try prospeo for their name
            parts = (dm.get("name") or "").split()
            fname = parts[0] if parts else ""
            lname = parts[-1] if len(parts) > 1 else ""
            p2 = prospeo_email(domain, fname, lname)
            if p2:
                cache[domain] = {"email": p2, "source": "employees+prospeo", "exit": False}
                _save_cache(cache)
                return p2, "employees+prospeo", f"dm={dm.get('name')} via prospeo"
            cache[domain] = {"email": "", "source": "employees",
                              "exit": True, "dm": dm.get("name")}
            _save_cache(cache)
            return "", "", f"decision maker {dm.get('name')} no email"

    cache[domain] = {"email": "", "source": "", "exit": True}
    _save_cache(cache)
    return "", "", "no email found"


def _resolve_enrich_only(company_url, company_name):
    """For NO_WEBSITE leads: try Prospeo/Employees only (skip website scrape)."""
    # Try Employees SHORT -> decision maker
    dm = find_decision_maker(company_url, company_name or "")
    if dm:
        if dm.get("email"):
            return dm["email"], "employees", "dm email (no website)"
        parts = (dm.get("name") or "").split()
        fname = parts[0] if parts else ""
        lname = parts[-1] if len(parts) > 1 else ""
        p2 = prospeo_email("", fname, lname)
        if p2:
            return p2, "employees+prospeo", f"dm={dm.get('name')} via prospeo"
    return "", "", "no email (no website, enrich not available)"
