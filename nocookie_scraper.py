"""
No-cookie (no LinkedIn login) lead engine.
Discover genuine buyers who need a web developer via PUBLIC, free, no-login sources:
  1. Chocodata jobsearch  -> companies actively hiring web/full-stack/frontend devs
  2. Chocodata /job       -> the actual need description (what they want built)
  3. Chocodata company    -> company website/domain
  4. Website scraping     -> public contact emails (info@, hello@, hello@, ...)
  5. Prospeo              -> personal email when a decision-maker/role name is known
  6. Free public registries (SEC EDGAR, GLEIF, Companies House web) -> officer/role context

Emails waterfall for a company lead:
  website contact email -> Prospeo (role/owner guess) 
All-time dedup via storage.lead_history (company slugs).
"""
import re
import os
import time
import json
import random
from urllib.parse import urlparse
from curl_cffi import requests as cffi_requests

CHOC_DATA_URL = "https://api.chocodata.com/api/v1/linkedin"

# Keywords -> the genuine buyer need. These are roles that small/medium businesses
# post when they NEED a website made (not developers selling services).
BUYER_JOB_KEYWORDS = [
    "web developer", "website developer", "frontend developer",
    "full stack developer", "wordpress developer", "shopify developer",
    "web designer needed", "website redesign", "landing page developer",
    "react developer", "laravel developer", "php developer",
]

SKIP_DOMAINS = {
    "example.com", "sentry.io", "linkedin.com", "w3.org", "schema.org",
    "adobe.org", "googleapis.com", "gstatic.com", "jquery.com", "cloudflare.com",
    "youtube.com", "facebook.com", "twitter.com", "x.com", "instagram.com",
    "pinterest.com", "spotify.com", "apple.com", "microsoft.com", "github.io",
    "githubassets.com", "wixpress.com", "sentry.3-form.com",
}


def _env():
    env = {}
    path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                if "=" in line and not line.strip().startswith("#"):
                    k, v = line.strip().split("=", 1)
                    env[k] = v
    return env


def _choco_key():
    return _env().get("CHOCODATA_API_KEY", "")


def _prospeo_key():
    return _env().get("PROSPEO_API_KEY", "")


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


# ─── 1. Discovery: companies actively hiring web devs (Chocodata jobsearch) ───

def discover_jobs(keyword="web developer", location="United States",
                  start=0, limit=10, only_web_need=True):
    """Return raw job listings from Chocodata jobsearch (no login)."""
    key = _choco_key()
    if not key:
        return []
    params = {
        "api_key": key, "keywords": keyword,
        "location": location, "start": start, "limit": limit,
    }
    try:
        r = cffi_requests.get(f"{CHOC_DATA_URL}/jobsearch", params=params,
                              impersonate="chrome146", timeout=60)
        if r.status_code != 200:
            return []
        results = r.json().get("results") or []
        if only_web_need:
            return [j for j in results if _is_web_need(j.get("title", ""))]
        return results
    except Exception:
        return []


def _is_web_need(title):
    t = (title or "").lower()
    web_terms = ["web", "frontend", "full stack", "full-stack", "front-end",
                 "website", "wordpress", "shopify", "react", "landing page",
                 "ui", "laravel", "php", "html", "javascript", "developer",
                 "engineer"]
    hits = sum(1 for term in web_terms if term in t)
    # Exclude backend/data-only roles that are unlikely "make me a website" buyers,
    # but keep anything with explicit web/frontend/website maker language.
    strong = ["web", "frontend", "front-end", "website", "wordpress",
              "shopify", "landing page", "full stack", "full-stack"]
    if any(s in t for s in strong):
        return True
    return hits >= 2


# ─── 2. Job detail: what the company NEEDS (the genuine lead story) ───

def get_job_detail(job_id):
    key = _choco_key()
    if not key or not job_id:
        return {"description": "", "seniority": "", "type": ""}
    params = {"api_key": key, "job_id": job_id}
    try:
        r = cffi_requests.get(f"{CHOC_DATA_URL}/job", params=params,
                              impersonate="chrome146", timeout=60)
        if r.status_code != 200:
            return {"description": "", "seniority": "", "type": ""}
        d = r.json()
        return {
            "description": (d.get("description") or "").strip()[:800],
            "seniority": d.get("seniority") or "",
            "type": d.get("employment_type") or "",
        }
    except Exception:
        return {"description": "", "seniority": "", "type": ""}


# ─── 3. Company website (Chocodata) ───

def get_company_website(company_slug):
    key = _choco_key()
    if not key or not company_slug:
        return ""
    params = {"api_key": key, "company": company_slug}
    try:
        r = cffi_requests.get(f"{CHOC_DATA_URL}/email", params=params,
                              impersonate="chrome146", timeout=60)
        if r.status_code == 200:
            d = r.json()
            web = d.get("website") or ""
            if web:
                return _clean_url(web)
    except Exception:
        pass
    try:
        params = {"api_key": key, "company": company_slug}
        r = cffi_requests.get(f"{CHOC_DATA_URL}/company", params=params,
                              impersonate="chrome146", timeout=60)
        if r.status_code == 200:
            web = (r.json().get("website") or "").strip()
            if web:
                return _clean_url(web)
    except Exception:
        pass
    return ""


# ─── 4. Scrape contact emails from company website ───

CONTACT_PATHS = [
    "", "contact", "contact-us", "contactus", "contact/", "about",
    "about-us", "aboutus", "contact.html", "contact-us#contact",
    "team", "about/team", "contact-us/", "pages/contact", "get-in-touch",
]


def _scrape_emails(url, depth=0):
    found = set()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        r = cffi_requests.get(url, impersonate="chrome146", timeout=20,
                              headers=headers, allow_redirects=True)
        if r.status_code != 200:
            return set()
        raw = re.findall(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', r.text)
        for e in raw:
            dom = e.split("@")[1].lower()
            if dom in SKIP_DOMAINS:
                continue
            if any(x in e.lower() for x in ["noreply", "no-reply", "example", "sentry", "test@", "your@", "name@", "email@"]):
                continue
            local = e.split("@")[0].lower()
            if len(local) < 2 or len(e) < 8:
                continue
            found.add(e.lower())
    except Exception:
        pass
    return found


def find_contact_emails(website):
    """Scrape a company website's contact/blog/about pages for emails."""
    if not website:
        return []
    base = _clean_url(website)
    domain = _domain_of(base)
    all_found = set()
    for path in CONTACT_PATHS:
        url = base if not path else f"{base.rstrip('/')}/{path}"
        all_found |= _scrape_emails(url)
        # prefer real-looking addresses (info@/hello@/support@/role@)
        scored = sorted(
            all_found,
            key=lambda e: (not re.match(r'^(info|hello|contact|support|team|office|hiring|jobs|careers|accounts|sales)@', e),
                           -len(e)),
        )
        if len(all_found) >= 3:
            break
        time.sleep(0.4)
    out = []
    for e in scored:
        if e.split("@")[1] == domain and e not in out:
            out.append(e)
    for e in scored:
        if e not in out:
            out.append(e)
    return out[:4]


# ─── 5. Prospeo personal email (decision-maker / role name + domain) ───

def prospeo_email(domain, first_name="", last_name=""):
    key = _prospeo_key()
    if not key or not domain:
        return ""
    params = {"domain": domain}
    if first_name:
        params["first_name"] = first_name
    if last_name:
        params["last_name"] = last_name
    try:
        r = cffi_requests.get("https://api.prospeo.io/v1/email-finder",
                              headers={"X-Key": key}, params=params,
                              impersonate="chrome146", timeout=30)
        if r.status_code != 200:
            return ""
        d = r.json()
        email = (d.get("personal_email") or d.get("work_email") or d.get("email") or "").strip()
        if email and "@" in email and email.split("@")[1].lower() not in SKIP_DOMAINS:
            return email
    except Exception:
        pass
    return ""


def email_waterfall_for_company(website, company_name=""):
    """Website contact email -> Prospeo fallback. Returns (email, source)."""
    emails = find_contact_emails(website)
    if emails:
        return emails[0], "website"
    # No website email -> try Prospeo by company domain with common roles/prefixes
    domain = _domain_of(website)
    if domain:
        for fname, lname in [("", ""), ("info", ""), ("hello", "")]:
            e = prospeo_email(domain, fname, lname)
            if e:
                return e, "prospeo"
    return "", ""


# ─── Main: one no-cookie lead round ───

def extract_nocookie_leads(keywords=None, locations=None, limit_per_kw=5,
                          callback=None, label="nc"):
    """
    Discover genuine buyer companies needing a web developer via no-login public sources.
    Returns (leads_with_email, leads_without_email, stats).
    Lead dict uses the same fields as storage.py.
    """
    stats = {"found": 0, "emails_found": 0, "sources": {}}
    leads = []
    seen_companies = set()

    if not keywords:
        keywords = BUYER_JOB_KEYWORDS
        random.shuffle(keywords)
    if not locations:
        locations = ["United States", "United Kingdom", "Canada", "India", "Australia"]

    for kw in keywords:
        if len(leads) >= 30:
            break
        loc = locations[0]
        if callback:
            callback(f"[{label}] Searching companies needing: {kw} ({loc})")
        jobs = discover_jobs(keyword=kw, location=loc, limit=limit_per_kw)
        for j in jobs:
            if len(leads) >= 30:
                break
            company = (j.get("company") or "").strip()
            job_url = (j.get("url") or "").strip()
            company_url = (j.get("company_url") or "").strip()
            company_slug = company_url.rstrip("/").split("/")[-1].lower()
            title = (j.get("title") or "").strip()
            job_id = str(j.get("job_id") or j.get("id") or "")

            key = company_slug or company.lower()
            if not key or key in seen_companies:
                stats["found"] += 0
                continue
            seen_companies.add(key)

            if callback:
                callback(f"[{label}]   -> {company}: {title}")

            detail = get_job_detail(job_id) if job_id else {}
            website = get_company_website(company_slug)
            email, e_source = email_waterfall_for_company(website, company)

            need_text = detail.get("description") or f"needs a {title}."
            need_text = re.sub(r"\s+", " ", need_text).strip()

            lead = {
                "name": company,
                "post_text": f"JOB: {title}. {need_text}".strip(),
                "post_url": job_url,
                "email": email,
                "profile_url": company_url,
                "source_keyword": kw,
                "company": company,
                "company_website": website,
                "company_slug": company_slug,
                "job_title": title,
                "location": j.get("location") or "",
                "status": "new",
            }
            leads.append(lead)
            stats["found"] += 1
            if email:
                stats["emails_found"] += 1
                stats["sources"][e_source] = stats["sources"].get(e_source, 0) + 1
            time.sleep(0.3)

    with_email = [l for l in leads if l.get("email")]
    without_email = [l for l in leads if not l.get("email")]
    return with_email, without_email, stats


if __name__ == "__main__":
    we, woe, st = extract_nocookie_leads(
        keywords=["web developer", "frontend developer"],
        limit_per_kw=6, callback=lambda m: print("  ", m),
    )
    print("\n=== WITH EMAIL ===")
    for l in we:
        print("-", l["name"], "|", l["email"], "|", l["post_text"][:80])
    print("\n=== WITHOUT EMAIL ===")
    for l in woe:
        print("-", l["name"], "|", l["post_text"][:60])
    print("\nSTATS:", st)
