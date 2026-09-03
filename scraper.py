"""
LinkedIn deep scraper — pro-level.
- Comment-to-lead (warm leads from post commenters)
- Waterfall email enrichment (post text -> Google -> GitHub -> SMTP verify)
- All-time history dedup before saving
- Enhanced buyer vs seller filtering
"""
import re
import os
import time
import random
from curl_cffi import requests as cffi_requests


SKIP_DOMAINS = {
    "example.com", "sentry.io", "linkedin.com", "w3.org", "schema.org",
    "adobe.org", "email-verification.com", "tempmail.com", "guerrillamail.com",
    "mailinator.com", "yopmail.com", "trashmail.com", "proteusthemes.com",
    "developer.apple.com", "fonts.googleapis.com", "github.io", "githubassets.com",
    "googleapis.com", "gstatic.com", "jquery.com", "cloudflare.com",
    "creativecommons.org", "gravatar.com", "wixpress.com", "sentry-next.wixpress.com",
}

OFFERING_PHRASES = [
    "i'm a full-stack developer", "i am a full-stack developer",
    "i'm a web developer", "i am a web developer",
    "i specialize in", "i specialize",
    "let's build it together", "lets build it together",
    "let us build it together", "we can build", "i can build",
    "i build websites", "i create websites",
    "my services", "hire me", "dm me for",
    "available for hire", "available for freelance",
    "freelance developer", "full-stack developer specializing",
    "web developer specializing", "i offer", "our services",
    "contact me for", "reach out to me", "looking for work",
    "open to work", "my expertise", "specializing in building",
    "building modern, scalable", "custom software solution",
    "passionate about building", "experienced in building",
    "as a developer", "as a freelancer", "if you need a developer",
    "if you need a website", "looking for clients", "need clients",
    "check out my portfolio", "see my work", "link in comments",
    "drop a comment", "dm for rates", "affordable rates",
    "starting at", "price starts", "starting from",
]

NEED_PHRASES = [
    "we need a", "i need a", "looking for a", "seeking a",
    "hiring a", "want to hire", "anyone know", "any recommendations",
    "who can", "can anyone", "urgent", "asap",
    "please dm if you can", "please dm me if", "dm me if you can",
    "need developer", "need someone", "budget", "project",
    "timeline", "deadline", "rfp", "proposal",
    "we're hiring", "we are hiring", "hiring for",
    "looking to hire", "searching for", "in search of",
    "must have", "requirements", "responsibilities",
    "full time", "part time", "contract", "freelance",
    "my business needs", "our business needs", "company needs",
    "startup needs", "we want", "i want someone",
    "recommend me", "suggestions for", "anyone available",
]


def parse_cookies(raw):
    cookies = {}
    for part in raw.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            cookies[k.strip()] = v.strip()
    return cookies


def load_cookies(path=None):
    if path is None:
        path = os.path.join(os.path.dirname(__file__), "data", "cookies.txt")
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return parse_cookies(f.read().strip())


def build_session(cookie_str_or_path=None):
    s = cffi_requests.Session(impersonate="chrome146")
    if cookie_str_or_path is None:
        cookies = load_cookies()
    elif os.path.exists(cookie_str_or_path):
        cookies = load_cookies(cookie_str_or_path)
    else:
        cookies = parse_cookies(cookie_str_or_path)
    for k, v in cookies.items():
        s.cookies.set(k, v, domain=".linkedin.com")
    jsid = cookies.get("JSESSIONID", "").strip('"')
    s.headers.update({
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "csrf-token": jsid,
    })
    return s


def test_login(session):
    try:
        r = session.get("https://www.linkedin.com/feed/", timeout=20, allow_redirects=True)
        if r.status_code == 200 and len(r.text) > 100000:
            return True, f"Logged in ({len(r.text)} chars)"
        elif r.status_code == 429:
            return True, "Rate limited but cookies valid"
        elif r.status_code == 302:
            return False, "Redirected to login - cookies expired"
        return False, f"Status {r.status_code}"
    except Exception as e:
        return False, str(e)[:100]


def get_auto_session(email, password, refresh=False):
    import auth
    cookie_str, msg = auth.get_li_at_cookie(email, password, refresh=refresh)
    if not cookie_str:
        return None, msg
    session = build_session(cookie_str)
    ok, login_msg = test_login(session)
    if not ok:
        return None, login_msg
    return session, "Auto-login OK"


# ─── Enhanced buyer vs seller filtering ───

def _is_developer_post(post_text, author_name=""):
    """Returns True if post is FROM a developer offering services (should be EXCLUDED)."""
    lower = post_text.lower()
    offer_score = 0
    for p in OFFERING_PHRASES:
        if p in lower:
            offer_score += 2

    need_score = 0
    for p in NEED_PHRASES:
        if p in lower:
            need_score += 2

    if "hiring" in lower and ("web developer" in lower or "frontend" in lower or "full stack" in lower):
        need_score += 5

    if "looking for" in lower and any(x in lower for x in ["developer", "builder", "someone to"]):
        need_score += 3

    if "dm me" in lower and any(x in lower for x in ["project", "budget", "website"]):
        need_score += 2

    if "commenting" in lower or "comment below" in lower or "tag someone" in lower:
        need_score += 1

    if offer_score > 0 and need_score == 0:
        return True

    if offer_score >= 4 and offer_score > need_score:
        return True

    return False


def _is_buyer_post(post_text):
    """Returns True if post is from someone SEEKING a developer (should be INCLUDED)."""
    lower = post_text.lower()
    buyer_score = 0
    buyer_signals = [
        "we need", "i need", "looking for", "seeking", "hiring",
        "anyone know", "recommend", "urgent", "asap", "budget",
        "project", "timeline", "deadline", "we're hiring", "we are hiring",
        "looking to hire", "searching for", "must have", "requirements",
        "responsibilities", "my business needs", "our company needs",
        "startup needs", "i want someone", "suggestions for", "anyone available",
        "full time", "part time", "contract", "freelance",
    ]
    for signal in buyer_signals:
        if signal in lower:
            buyer_score += 1

    return buyer_score >= 2


def _sleep_random(min_s=2, max_s=5):
    time.sleep(random.randint(min_s, max_s))


# ─── Email enrichment (waterfall) ───

def _extract_emails_from_page(html):
    """Extract real person emails from a LinkedIn profile page."""
    raw_emails = re.findall(
        r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', html
    )
    valid = []
    for e in raw_emails:
        domain = e.split("@")[1].lower()
        if domain in SKIP_DOMAINS:
            continue
        if re.search(r'\d{5,}', e):
            continue
        if e.startswith(".") or e.endswith("."):
            continue
        local = e.split("@")[0].lower()
        if len(local) < 3:
            continue
        if any(x in local for x in ["noreply", "no-reply", "test", "example", "admin", "support"]):
            continue
        valid.append(e)
    seen = set()
    unique = []
    for e in valid:
        el = e.lower()
        if el not in seen:
            seen.add(el)
            unique.append(e)
    return unique


def _extract_email_from_post_text(post_text):
    """Extract email directly from post text (people sometimes share their email)."""
    if not post_text:
        return ""
    emails = re.findall(
        r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', post_text
    )
    for e in emails:
        domain = e.split("@")[1].lower()
        if domain in SKIP_DOMAINS:
            continue
        local = e.split("@")[0].lower()
        if len(local) >= 3 and not any(x in local for x in ["noreply", "no-reply", "test"]):
            return e
    return ""


def _load_api_keys():
    """Load Apollo/Prospeo API keys from .env."""
    keys = {"APOLLO": "", "PROSPEO": ""}
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    try:
        if os.path.exists(env_path):
            with open(env_path, encoding="utf-8") as f:
                for line in f:
                    if "=" in line and not line.strip().startswith("#"):
                        k, v = line.strip().split("=", 1)
                        if k == "APOLLO_API_KEY":
                            keys["APOLLO"] = v
                        elif k == "PROSPEO_API_KEY":
                            keys["PROSPEO"] = v
    except Exception:
        pass
    return keys


def _apollo_enrich(linkedin_url, name=""):
    """Apollo.io people enrichment — LinkedIn URL/name → email. 10K free credits."""
    if not linkedin_url:
        return ""
    key = _load_api_keys()["APOLLO"]
    if not key:
        return ""
    url = "https://api.apollo.io/v1/people/match"
    payload = {
        "api_key": key,
        "linkedin_url": linkedin_url,
        "reveal_personal_emails": False,
    }
    if name:
        payload["name"] = name
    try:
        r = cffi_requests.post(url, json=payload, impersonate="chrome146", timeout=20)
        if r.status_code != 200:
            return ""
        data = r.json()
        person = data.get("person") or {}
        email = (person.get("email") or "").strip()
        if not email:
            for key_name in ("primary_email", "organization_email"):
                e = (person.get(key_name) or "").strip()
                if e and "@" in e:
                    email = e
                    break
        if email and "@" in email:
            domain = email.split("@")[1].lower()
            if domain not in SKIP_DOMAINS:
                return email
    except Exception:
        pass
    return ""


def _prospeo_enrich(linkedin_url, name=""):
    """Prospeo email finder — LinkedIn URL → email. 100 free credits/mo."""
    if not linkedin_url:
        return ""
    key = _load_api_keys()["PROSPEO"]
    if not key:
        return ""
    url = "https://api.prospeo.io/v1/email-finder"
    headers = {"X-Key": key}
    params = {"linkedin_url": linkedin_url}
    if name:
        params["name"] = name
    try:
        r = cffi_requests.get(url, headers=headers, params=params, impersonate="chrome146", timeout=20)
        if r.status_code != 200:
            return ""
        data = r.json()
        email = (data.get("personal_email") or data.get("work_email") or "").strip()
        if not email:
            for k in ("email", "primary_email"):
                e = (data.get(k) or "").strip()
                if e and "@" in e:
                    email = e
                    break
        if email and "@" in email:
            domain = email.split("@")[1].lower()
            if domain not in SKIP_DOMAINS:
                return email
    except Exception:
        pass
    return ""


def _waterfall_email(name, post_text="", company="", linkedin_url="", session=None):
    """Try post text → Apollo → Prospeo to find email."""
    # 1. Email in post text (highest confidence)
    email = _extract_email_from_post_text(post_text)
    if email:
        return email, "post_text"

    # 2. Apollo.io API
    if linkedin_url:
        email = _apollo_enrich(linkedin_url, name)
        if email:
            return email, "apollo"

    # 3. Prospeo API
    if linkedin_url:
        email = _prospeo_enrich(linkedin_url, name)
        if email:
            return email, "prospeo"

    return "", ""


def _try_profile_email(session, name):
    """Try to find email on LinkedIn profile pages."""
    slug = re.sub(r"[^a-z0-9-]", "", name.lower().replace(" ", "-"))
    profile_url = f"https://www.linkedin.com/in/{slug}/"
    urls_to_try = [
        profile_url + "about",
        profile_url,
    ]
    for url in urls_to_try:
        try:
            r = session.get(url, timeout=20, allow_redirects=True)
            if r.status_code == 200 and len(r.text) > 5000:
                emails = _extract_emails_from_page(r.text)
                if emails:
                    return emails[0]
            _sleep_random(1, 3)
        except Exception:
            _sleep_random(1, 2)
            continue
    return ""


# ─── Content search + parsing ───

def search_content(session, keyword, count=20, max_pages=5, date_posted="past-week"):
    all_leads = []
    seen_names = set()

    date_ranges = [date_posted]
    if date_posted == "past-week":
        date_ranges = ["past-24h", "past-week", "past-month"]

    for dr in date_ranges:
        if len(all_leads) >= count:
            break
        for page in range(1, max_pages + 1):
            start = (page - 1) * 10
            url = (
                f"https://www.linkedin.com/search/results/content/"
                f"?keywords={keyword.replace(' ', '+')}"
                f"&datePosted={dr}"
                f"&sortBy=RELEVANCE"
                f"&start={start}"
                f"&origin=GLOBAL_SEARCH_HEADER"
            )
            try:
                r = session.get(url, timeout=30, allow_redirects=True)
                if r.status_code == 429:
                    time.sleep(random.randint(20, 45))
                    r = session.get(url, timeout=30, allow_redirects=True)
                if r.status_code != 200 or len(r.text) < 1000:
                    break
                leads = _parse_content_html(r.text, keyword)
                new = 0
                for l in leads:
                    name_key = l.get("name", "").lower().strip()
                    if name_key and name_key not in seen_names:
                        seen_names.add(name_key)
                        all_leads.append(l)
                        new += 1
                if new == 0 or len(all_leads) >= count:
                    break
                _sleep_random(3, 6)
            except Exception:
                break

    return all_leads[:count]


def search_groups(session, keyword, count=20, max_pages=3):
    all_leads = []
    seen_names = set()
    search_url = (
        f"https://www.linkedin.com/search/results/groups/"
        f"?keywords={keyword.replace(' ', '+')}"
        f"&origin=GLOBAL_SEARCH_HEADER"
    )
    try:
        r = session.get(search_url, timeout=30, allow_redirects=True)
        if r.status_code != 200 or len(r.text) < 1000:
            return all_leads
        group_urls = re.findall(r'href="(https://www\.linkedin\.com/groups/[^"]+)"', r.text)
        group_urls = list(dict.fromkeys(group_urls))[:5]
        for gu in group_urls:
            if len(all_leads) >= count:
                break
            for page in range(1, max_pages + 1):
                start = (page - 1) * 10
                gr_url = f"{gu}search/?keywords={keyword.replace(' ', '+')}&start={start}"
                try:
                    gr = session.get(gr_url, timeout=30, allow_redirects=True)
                    if gr.status_code != 200 or len(gr.text) < 1000:
                        break
                    leads = _parse_content_html(gr.text, keyword)
                    new = 0
                    for l in leads:
                        name_key = l.get("name", "").lower().strip()
                        if name_key and name_key not in seen_names:
                            seen_names.add(name_key)
                            all_leads.append(l)
                            new += 1
                    if new == 0:
                        break
                    _sleep_random(3, 6)
                except Exception:
                    break
    except Exception:
        pass
    return all_leads[:count]


# ─── Comment-to-lead (warm leads) ───

def scrape_post_comments(session, post_url, max_comments=50):
    """Scrape commenters from a LinkedIn post — these are warm leads."""
    leads = []
    seen_names = set()
    try:
        r = session.get(post_url, timeout=30, allow_redirects=True)
        if r.status_code != 200 or len(r.text) < 5000:
            return leads

        commenters = re.findall(
            r'href="(https://www\.linkedin\.com/in/[^"]+)"[^>]*>[^<]*<[^>]*>([^<]+)',
            r.text
        )
        if not commenters:
            commenters = re.findall(
                r'Open control menu for post by ([^"\\]+)',
                r.text
            )
            for name in commenters:
                name = name.strip().rstrip("\\").strip()
                if name.lower() not in seen_names:
                    seen_names.add(name.lower())
                    slug = re.sub(r"[^a-z0-9-]", "", name.lower().replace(" ", "-"))
                    leads.append({
                        "name": name,
                        "profile_url": f"https://www.linkedin.com/in/{slug}/",
                        "post_text": "",
                        "post_url": post_url,
                        "email": "",
                        "source_keyword": "comment_harvest",
                    })
            return leads[:max_comments]

        for url, name in commenters:
            name = name.strip()
            if not name or name.lower() in seen_names or len(name) < 3:
                continue
            if "/in/" not in url:
                continue
            seen_names.add(name.lower())
            leads.append({
                "name": name,
                "profile_url": url if url.startswith("http") else f"https://www.linkedin.com{url}",
                "post_text": "",
                "post_url": post_url,
                "email": "",
                "source_keyword": "comment_harvest",
            })
    except Exception:
        pass
    return leads[:max_comments]


def scrape_top_post_engagement(session, keyword, count=20):
    """Find top posts by engagement and harvest their commenters as warm leads."""
    all_leads = []
    seen_names = set()
    search_url = (
        f"https://www.linkedin.com/search/results/content/"
        f"?keywords={keyword.replace(' ', '+')}"
        f"&sortBy=RELEVANCE"
        f"&count=10"
    )
    try:
        r = session.get(search_url, timeout=30, allow_redirects=True)
        if r.status_code != 200 or len(r.text) < 1000:
            return all_leads

        post_urls = list(set(re.findall(
            r'https://www\.linkedin\.com/feed/update/urn:li:activity:\d+',
            r.text
        )))[:5]

        for pu in post_urls:
            if len(all_leads) >= count:
                break
            comments = scrape_post_comments(session, pu, max_comments=10)
            for l in comments:
                nk = l.get("name", "").lower().strip()
                if nk and nk not in seen_names:
                    seen_names.add(nk)
                    all_leads.append(l)
            _sleep_random(3, 6)
    except Exception:
        pass
    return all_leads[:count]


# ─── HTML parsing ───

def _parse_content_html(text, keyword):
    leads = []
    author_positions = []
    for m in re.finditer(r'Open control menu for post by ([^"\\]+)', text):
        name = m.group(1).strip().rstrip("\\").strip()
        author_positions.append((m.start(), name))
    if not author_positions:
        return leads

    url_positions = []
    for m in re.finditer(r'href="(https://www\.linkedin\.com/in/[^"]+)"', text):
        url_positions.append((m.start(), m.group(1)))

    author_urls = {}
    for auth_pos, auth_name in author_positions:
        best_url = ""
        best_dist = float("inf")
        for url_pos, url in url_positions:
            if url_pos > auth_pos:
                dist = url_pos - auth_pos
                if dist < best_dist:
                    best_dist = dist
                    best_url = url
        author_urls[auth_name] = best_url

    author_activities = {}
    for auth_pos, auth_name in author_positions:
        best_urn = ""
        best_dist = float("inf")
        for m in re.finditer(r'urn:li:activity:(\d+)', text):
            if m.start() > auth_pos:
                dist = m.start() - auth_pos
                if dist < best_dist:
                    best_dist = dist
                    best_urn = m.group(1)
        author_activities[auth_name] = best_urn

    seen = set()
    for _, author in author_positions:
        if author.lower() in seen:
            continue
        seen.add(author.lower())
        profile_url = author_urls.get(author, "")
        if not profile_url:
            slug = re.sub(r"[^a-z0-9-]", "", author.lower().replace(" ", "-"))
            profile_url = f"https://www.linkedin.com/in/{slug}/"

        activity_id = author_activities.get(author, "")
        post_url = f"https://www.linkedin.com/feed/update/urn:li:activity:{activity_id}" if activity_id else ""

        leads.append({
            "name": author,
            "profile_url": profile_url,
            "post_text": "",
            "post_url": post_url,
            "email": "",
            "source_keyword": keyword,
        })

    expandable_matches = list(re.finditer(
        r'data-testid="expandable-text-box">(.*?)</span>', text, re.DOTALL
    ))
    for em in expandable_matches:
        content = em.group(1)
        visible = re.sub(r'<[^>]+>', ' ', content)
        visible = re.sub(r'\s+', ' ', visible).strip()
        visible = visible.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
        if len(visible) < 20:
            continue
        best_author = None
        best_dist = float("inf")
        for auth_pos, auth_name in author_positions:
            dist = em.start() - auth_pos
            if 0 < dist < best_dist:
                best_dist = dist
                best_author = auth_name
        if best_author:
            for lead in leads:
                if lead["name"] == best_author and not lead["post_text"]:
                    lead["post_text"] = visible[:500]
                    break

    filtered = []
    for lead in leads:
        if lead["post_text"] and _is_developer_post(lead["post_text"], lead["name"]):
            continue
        filtered.append(lead)
    leads = filtered

    return leads


# ─── Main deep scrape ───

def deep_scrape(session, keywords, count_per_keyword=10, callback=None):
    """
    Full deep scrape: search posts + groups + comment harvesting + waterfall email enrichment.
    callback(message) for progress updates.
    Returns (leads_with_email, leads_without_email, stats).
    """
    all_candidates = []
    seen_names = set()
    stats = {"found": 0, "filtered": 0, "emails_found": 0, "sources": {}}

    for kw in keywords:
        if callback:
            callback(f"[1/4] Searching posts: {kw}")
        post_leads = search_content(session, kw, count=count_per_keyword, max_pages=5)
        for l in post_leads:
            nk = l.get("name", "").lower().strip()
            if nk and nk not in seen_names:
                seen_names.add(nk)
                all_candidates.append(l)
            else:
                stats["filtered"] += 1

        if callback:
            callback(f"[2/4] Searching groups: {kw}")
        group_leads = search_groups(session, kw, count=5)
        for l in group_leads:
            nk = l.get("name", "").lower().strip()
            if nk and nk not in seen_names:
                seen_names.add(nk)
                all_candidates.append(l)
            else:
                stats["filtered"] += 1

        if callback:
            callback(f"[3/4] Harvesting commenters: {kw}")
        comment_leads = scrape_top_post_engagement(session, kw, count=10)
        for l in comment_leads:
            nk = l.get("name", "").lower().strip()
            if nk and nk not in seen_names:
                seen_names.add(nk)
                all_candidates.append(l)
            else:
                stats["filtered"] += 1

        _sleep_random(2, 4)

    stats["found"] = len(all_candidates)

    if callback:
        callback(f"[4/4] Enriching {len(all_candidates)} candidates with emails (waterfall)...")

    with_email = []
    without_email = []
    for i, candidate in enumerate(all_candidates):
        name = candidate.get("name", "")
        post_text = candidate.get("post_text", "")
        profile_url = candidate.get("profile_url", "")

        if callback and (i + 1) % 5 == 0:
            callback(f"[4/4] Email enrichment {i+1}/{len(all_candidates)}: {name}")

        company = ""
        company_match = re.search(
            r'(?:at|@|from|company)\s+([A-Z][a-zA-Z\s&]+)',
            post_text
        )
        if company_match:
            company = company_match.group(1).strip()

        email, source = _waterfall_email(name, post_text, company, profile_url)

        if email:
            candidate["email"] = email
            with_email.append(candidate)
            stats["emails_found"] += 1
            stats["sources"][source] = stats["sources"].get(source, 0) + 1
        else:
            without_email.append(candidate)

        _sleep_random(1, 3)

    return with_email, without_email, stats


# --- Reusable single-round extraction for scheduler ---

def load_all_keywords(max_count=200):
    """Load all keywords from keywords.txt."""
    path = os.path.join(os.path.dirname(__file__), "data", "keywords.txt")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        kws = [k.strip() for k in f.read().splitlines() if k.strip()]
    return kws[:max_count]


def extract_leads_once(li_email=None, li_pass=None, keywords=None, count_per_keyword=10,
                       date_posted="past-week", callback=None, label="round"):
    """
    One full scrape round used by the scheduler/webapp.
    - Auto-login
    - Deep scrape (posts + groups + commenters)
    - Waterfall email enrichment (post text -> Apollo -> Prospeo)
    - Returns (all_leads_with_email, all_leads_without_email, stats)
    Does NOT save to CSV - caller decides. Dedup is applied by storage.add_leads.
    """
    import auth as auth_mod
    if li_email is None or li_pass is None:
        env = {}
        env_path = os.path.join(os.path.dirname(__file__), ".env")
        if os.path.exists(env_path):
            with open(env_path, encoding="utf-8") as f:
                for line in f:
                    if "=" in line and not line.strip().startswith("#"):
                        k, v = line.strip().split("=", 1)
                        env[k] = v
        li_email = env.get("LINKEDIN_EMAIL", "")
        li_pass = env.get("LINKEDIN_PASSWORD", "")

    if not li_email or not li_pass:
        raise RuntimeError("LinkedIn credentials not set")

    if callback:
        callback(f"[{label}] Authenticating LinkedIn...")

    # 1. Try cached cookie file first (fast, avoids CHALLENGE)
    session = None
    cookie_file = os.path.join(os.path.dirname(__file__), "data", "cookies.txt")
    if os.path.exists(cookie_file):
        try:
            s = build_session(cookie_file)
            ok, lmsg = test_login(s)
            if ok:
                session = s
                if callback:
                    callback(f"[{label}] Using cached cookie session ({lmsg})")
        except Exception:
            session = None

    # 2. Fall back to auto-login refresh
    if session is None:
        cookie_str, msg = auth_mod.get_li_at_cookie(li_email, li_pass)
        if not cookie_str:
            raise RuntimeError(f"Login failed (auto): {msg}")
        session = build_session(cookie_str)
        ok, lmsg = test_login(session)
        if not ok:
            raise RuntimeError(f"Login check failed: {lmsg}")

    if not keywords:
        keywords = load_all_keywords()
        random.shuffle(keywords)
        keywords = keywords[:20]

    if callback:
        callback(f"[{label}] Deep scraping {len(keywords)} keywords ({count_per_keyword} each)...")

    with_email, without_email, stats = deep_scrape(
        session, keywords,
        count_per_keyword=count_per_keyword,
        callback=callback,
    )
    return with_email, without_email, stats
