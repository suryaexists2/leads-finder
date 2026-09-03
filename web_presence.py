"""
5-state web presence classifier for local-business leads.

Decides: "Does this business have a real usable website?"
Never scores "how many gaps" — classifies into one of 5 states:

  NO_WEBSITE        -> no URL/domain supplied at all          -> LEAD
  BROKEN_WEBSITE    -> site returns 404 / dead / unreachable  -> LEAD
  PLACEHOLDER_WEBSITE -> parking / under-construction / default -> LEAD
  REAL_WEBSITE      -> genuine business-specific content      -> REJECT
  UNKNOWN           -> 403 / Cloudflare / timeout / ambiguous -> DO NOT qualify

Precision-first: 10 genuine no-website leads > 30 false-positive leads.
"""
import re
import time

import config

PLACEHOLDER_PATTERNS = [
    r"this domain is parked",
    r"domain is parked",
    r"website is parked",
    r"sedo\s*parking",
    r"sedoparking",
    r"godaddy\s*parking",
    r"godaddyparking",
    r"domains\.google",
    r"domain\s*marketplace",
    r"domainmarketplace",
    r"buy\s+this\s+domain",
    r"register\s+this\s+domain",
    r"this\s+domain\s+is\s+available",
    r"this\s+domain\s+is\s+for\s+sale",
    r"whois\s+protection",
    r"parked\s*by",
    r"parking\s+page",
    r"page\s+not\s+found",
    r"404\s*[-–]\s*page\s+not\s+found",
    r"no\s+webpage\s+found",
    r"domain\s+not\s+found",
    r"waiting\s+for",
    r"nxdomain",
    r"this\s+website\s+is\s+parked",
    r"under\s+construction",
    r"coming\s+soon",
    r"construction\s+in\s+progress",
    r"site\s+under\s+construction",
    r"maintenance\s+mode",
    r"maintenance\s+page",
    r"be\s+back?\s+shortly",
    r"back?\s+soon",
    r"we'?ll?\s+be\s+back",
    r"sorry\s+for\s+the\s+inconvenience",
    r"launching?\s+soon",
    r"^soon$",
    r"welcome\s+to\s+nginx",
    r"welcome\s+to\s+apache",
    r"welcome\s+to\s+iis",
    r"it\s+works!",
    r"it\s+works",
    r"default\s+page",
    r"default\s+server",
    r"powered\s+by\s+(nginx|apache|iis)",
    r"ubuntu\s+default",
    r"photon\s+by",
    r"simply\s+hosted",
]

PLACEHOLDER_TITLE_PATTERNS = [
    r"parking page",
    r"parked",
    r"coming soon",
    r"under construction",
    r"site suspended",
    r"domain suspended",
    r"domain expired",
    r"website suspended",
    r"this domain is",
]

BUSINESS_CONTENT_PATTERNS = [
    r"\b(services|our\s+services|what\s+we\s+do|solutions)\b",
    r"\b(about\s+us|about\s+the\s+company|our\s+company|who\s+we\s+are)\b",
    r"\b(contact\s+us|contact\s+page|get\s+in\stouch|reach\s+out)\b",
    r"\b(portfolio|our\s+work|case\s+stud(?:y|ies)|projects|our\s+projects)\b",
    r"\b(our\s+team|meet\s+the\s+team|team\s+members)\b",
    r"\b(our\s+clients|our\s+customers|what\s+clients\s+say|testimonials)\b",
    r"\b(hours|hours\s+of\s+operation|opening\s+hours|appointment|schedule\s+an?\s+appointment)\b",
    r"\b(pricing|prices|packages|plans|premium|quote)\b",
]

CONTACT_SIGNALS = [
    r"\bphone[:\s]*\d",
    r"\btel[:\s]*\+?\d",
    r"\bemail[:\s]*[\w.]+@[\w.]+\.\w",
    r"\baddress[:\s]*[\w\s#,]+",
    r"\b(street|avenue|boulevard|drive|road)\b.*\b\d",
    r"\bcity|state|zip|postal",
]

SCHEMA_LOCALBUSINESS = r'"@type"\s*:\s*"LocalBusiness"'
SCHEMA_ORGANIZATION = r'"@type"\s*:\s*"Organization"'


def _now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


# Hosts that are social profiles / listing portals, NOT owned websites.
# A business whose only "website" is a Facebook/Instagram/Yelp/Google Business
# profile has NO owned website -> it is a valid no-website lead, never a REAL
# website (the owner's profile pages would otherwise score as a real site).
SOCIAL_PROFILE_HOSTS = {
    "facebook.com", "instagram.com", "twitter.com", "x.com", "tiktok.com",
    "youtube.com", "linkedin.com", "pinterest.com", "snapchat.com",
    "yelp.com", "yelp.ca", "tripadvisor.com", "yellowpages.com",
    "superpages.com", "angieslist.com", "angi.com", "houzz.com",
    "maps.google.com", "google.com", "bing.com", "yandex.com", "foursquare.com",
}

# Dead-domain language (DNS NXDOMAIN - the domain simply does not exist).
DEAD_DOMAIN_PATTERNS = [
    "could not resolve host", "name resolution error", "name could not be resolved",
    "no address associated with hostname", "nxdomain", "dns exception",
    "server failed to answer", "address family for hostname not supported",
    "could not resolve", "domain does not exist", "domain not found",
]


def _extract_text_from_html(html):
    if not html:
        return ""
    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&#\d+;", " ", text)
    text = re.sub(r"&(amp|lt|gt|quot|#x?[0-9a-fA-F]+);", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _has_element(html, pattern):
    return bool(re.search(pattern, html, re.IGNORECASE | re.DOTALL))


def classify(url, domain):
    """Classify a business website into one of 5 states.

    Returns:
        {
            "web_status":     "NO_WEBSITE" | "BROKEN_WEBSITE" | "PLACEHOLDER_WEBSITE"
                              | "REAL_WEBSITE" | "UNKNOWN",
            "web_confidence": float 0.0-1.0,
            "web_reason":     str,
            "url":            str,
            "domain":         str,
            "signals": {
                "status_code": int|None,
                "error":       str|None,
                "final_url":   str,
                "placeholder_hits": [...],
                "business_hits":    [...],
                "contact_hits":     [...],
                "tech_indicators":  [...],
                "has_navigation":   bool,
                "has_schema":       bool,
                "has_real_content": bool,
                "html_length":      int,
            },
            "web_checked_at": str (ISO timestamp),
        }
    """
    result = {
        "web_status": "UNKNOWN",
        "web_confidence": 0.0,
        "web_reason": "",
        "url": url,
        "domain": domain,
        "signals": {
            "status_code": None,
            "error": None,
            "final_url": "",
            "placeholder_hits": [],
            "business_hits": [],
            "contact_hits": [],
            "tech_indicators": [],
            "has_navigation": False,
            "has_schema": False,
            "has_real_content": False,
            "html_length": 0,
        },
        "web_checked_at": _now_str(),
    }

    if not url and not domain:
        result["web_status"] = "NO_WEBSITE"
        result["web_confidence"] = 1.0
        result["web_reason"] = "No website URL or domain supplied"
        return result

    if not url and domain:
        url = "https://" + domain.strip()
        result["url"] = url

    if not url:
        result["web_status"] = "NO_WEBSITE"
        result["web_confidence"] = 1.0
        result["web_reason"] = "No website URL could be constructed"
        return result

    # The supplied "website" is only a social/listing profile - the business
    # owns no website. ACCEPT (user rule: "business has only Facebook/Instagram/
    # Yelp/Google Business/social profile but no actual website").
    if _host_of(url) in SOCIAL_PROFILE_HOSTS:
        result["web_status"] = "NO_WEBSITE"
        result["web_confidence"] = 1.0
        result["web_reason"] = "Only a social/listing profile ({0}) - no owned website".format(_host_of(url))
        result["signals"]["final_url"] = url
        return result

    status_code, html, error = _fetch_page(url)
    result["signals"]["status_code"] = status_code
    result["signals"]["error"] = error

    if error:
        # A domain that does not resolve at all is a DEAD domain -> accept as a
        # no-website lead (user rule: "domain is parked / completely dead").
        if _is_dead_domain(error):
            result["web_status"] = "BROKEN_WEBSITE"
            result["web_confidence"] = 0.85
            result["web_reason"] = "Domain does not resolve (dead/no website): {0}".format(error[:120])
            result["signals"]["tech_indicators"].append("dead_domain: {0}".format(error[:80]))
            return result
        if _is_tech_failure(error, status_code, html):
            result["web_status"] = "UNKNOWN"
            result["web_confidence"] = 0.3
            result["web_reason"] = "Technical failure ({0}): cannot determine web presence".format(error)
            result["signals"]["tech_indicators"].append("tech_error: {0}".format(error))
            return result

    if status_code is not None and status_code != 200:
        if status_code == 404:
            result["web_status"] = "BROKEN_WEBSITE"
            result["web_confidence"] = 0.9
            result["web_reason"] = "Domain returns HTTP 404 - dead/broken website"
        elif status_code == 403:
            result["web_status"] = "UNKNOWN"
            result["web_confidence"] = 0.3
            result["web_reason"] = "HTTP 403 Forbidden - ambiguous (could be real site with bot protection)"
            result["signals"]["tech_indicators"].append("HTTP_403")
        else:
            result["web_status"] = "BROKEN_WEBSITE"
            result["web_confidence"] = 0.85
            result["web_reason"] = "Domain returns HTTP {0} - broken/unreachable website".format(status_code)
        result["signals"]["final_url"] = url
        return result

    if not html or len(html.strip()) < 50:
        result["web_status"] = "BROKEN_WEBSITE"
        result["web_confidence"] = 0.85
        result["web_reason"] = "Server returned 200 but with empty/invalid content"
        result["signals"]["final_url"] = url
        return result

    result["signals"]["html_length"] = len(html)
    result["signals"]["final_url"] = url
    text = _extract_text_from_html(html)

    business_hits = _detect_real_business(html, text, url)
    result["signals"]["business_hits"] = business_hits

    contact_hits = _detect_contact_signals(html, text)
    result["signals"]["contact_hits"] = contact_hits

    has_nav = _has_element(html, r"<nav[\s>]") or _has_element(html, r"<nav\b")
    result["signals"]["has_navigation"] = bool(has_nav)

    has_schema = _has_element(html, SCHEMA_LOCALBUSINESS) or _has_element(html, SCHEMA_ORGANIZATION)
    result["signals"]["has_schema"] = bool(has_schema)

    # Placeholder detection is run on the VISIBLE page text ONLY (not the raw
    # HTML). Real business sites routinely embed "default page"/"it works"-style
    # strings inside JS bundles/templating, which previously flagged them as
    # placeholder and wrongly qualified them as no-website prospects.
    title_hits = _check_title_placeholder(html)
    body_ph = _detect_placeholder(text)
    placeholder_hits = list(body_ph) + list(title_hits or [])
    result["signals"]["placeholder_hits"] = placeholder_hits

    # A page is "real content" when it carries genuine business structure.
    real_content = bool(has_nav or has_schema or len(business_hits) >= 2
                        or len(contact_hits) >= 2)

    # A title that itself is a parked/coming-soon phrase is a holding page no
    # matter what else is inside it.
    if title_hits:
        result["web_status"] = "PLACEHOLDER_WEBSITE"
        result["web_confidence"] = min(0.95, 0.85 + len(title_hits) * 0.05)
        result["web_reason"] = "Placeholder/parking/under-construction page detected: {0}".format("; ".join(title_hits[:3]))
        return result

    # Placeholder body phrases only count when the page does NOT look like a
    # genuine business website. A stray phrase inside an otherwise real business
    # page must not downgrade a real site to a placeholder.
    if body_ph and not real_content:
        result["web_status"] = "PLACEHOLDER_WEBSITE"
        result["web_confidence"] = min(0.95, 0.8 + len(body_ph) * 0.05)
        result["web_reason"] = "Placeholder/parking/under-construction page detected: {0}".format("; ".join(body_ph[:3]))
        return result

    if real_content:
        result["web_status"] = "REAL_WEBSITE"
        result["web_confidence"] = min(0.95, 0.85 + (len(business_hits) + len(contact_hits)) * 0.02)
        result["web_reason"] = "Genuine business website detected: {0}".format("; ".join(business_hits[:3]) or "business content present")
        return result

    result["web_status"] = "UNKNOWN"
    result["web_confidence"] = 0.4
    result["web_reason"] = "Could not confidently classify - ambiguous content, treat as unknown"
    return result


def _fetch_page(url, timeout=12):
    import curl_cffi.requests as cr
    try:
        r = cr.get(url, impersonate="chrome136", timeout=timeout,
                    headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US"},
                    allow_redirects=True)
        return r.status_code, r.text, None
    except Exception as e:
        err_str = str(e).lower()
        return None, "", err_str


def _host_of(url):
    from urllib.parse import urlparse
    if not url:
        return ""
    u = url if url.lower().startswith("http") else "https://" + url
    try:
        h = urlparse(u).netloc.lower()
    except Exception:
        return ""
    if h.startswith("www."):
        h = h[4:]
    return h.split(":")[0]


def _is_dead_domain(error):
    if not error:
        return False
    err = error.lower()
    return any(p in err for p in DEAD_DOMAIN_PATTERNS)


def _is_tech_failure(error, status_code, html):
    if not error:
        return False
    err = error.lower()
    for ind in ["cloudflare", "incident", "under attack", "challenge",
                "captcha", "security check", "access denied",
                "timeout", "timed out",
                "connection refused", "connection reset",
                "ssl", "tls", "certificate",
                "dns", "name resolution", "blocked",
                "resolve", "could not resolve", "failed to perform",
                "network is unreachable", "no route to host",
                "connection timed out", "read operation timed out"]:
        if ind in err:
            return True
    if html and ("cloudflare" in html.lower() or "challenge" in html.lower()):
        return True
    return False


def _detect_placeholder(text):
    hits = []
    low = (text or "").lower()
    for pat in PLACEHOLDER_PATTERNS:
        if re.search(pat, low, re.IGNORECASE):
            hits.append(pat.replace(r"\s+", " ").strip()[:50])
            if len(hits) >= 3:
                break
    return hits


def _check_title_placeholder(html):
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if not title_match:
        return []
    title = title_match.group(1).lower().strip()
    hits = []
    for pat in PLACEHOLDER_TITLE_PATTERNS:
        if re.search(pat, title, re.IGNORECASE):
            hits.append("title: {0}".format(pat.replace(r"\s+", " ").strip()))
    return hits


def _detect_real_business(html, text, url):
    hits = []
    low = text.lower() if text else ""
    html_low = html.lower()

    for pat in BUSINESS_CONTENT_PATTERNS:
        if re.search(pat, low, re.IGNORECASE):
            hits.append("content: {0}".format(pat.replace(r"\s+", " ").strip()))
            if len(hits) >= 5:
                break

    for pat in CONTACT_SIGNALS:
        if re.search(pat, low):
            hits.append("contact: {0}".format(pat.replace(r"\s+", " ").strip()[:40]))
            if len(hits) >= 8:
                break

    if re.search(SCHEMA_LOCALBUSINESS, html_low) or re.search(SCHEMA_ORGANIZATION, html_low):
        hits.append("schema: LocalBusiness/Organization")

    all_hrefs = re.findall(r'href=["\']([^"\']+)', html, re.IGNORECASE)
    skip_domains = ['facebook', 'instagram', 'twitter', 'youtube', 'linkedin', 'tiktok', 'yelp', 'google.', 'pinterest']
    from urllib.parse import urlparse
    page_host = (urlparse(url).netloc or "").lower()
    if page_host.startswith("www."):
        page_host = page_host[4:]
    internal_links = []
    for l in all_hrefs:
        ll = l.strip().lower()
        if not ll or ll.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
            continue
        if ll.startswith(("http://", "https://")):
            if any(d in ll for d in skip_domains):
                continue
            try:
                lhost = urlparse(l).netloc.lower()
            except Exception:
                continue
            if lhost.startswith("www."):
                lhost = lhost[4:]
            if lhost == page_host:
                internal_links.append(l)
        elif not ll.startswith("//"):
            # relative link = internal page link
            internal_links.append(l)
    if len(internal_links) >= 5:
        hits.append("navigation: {0} internal links".format(len(internal_links)))

    return hits


def _detect_contact_signals(html, text):
    hits = []
    low = text.lower() if text else ""
    for pat in CONTACT_SIGNALS:
        if re.search(pat, low):
            hits.append(pat.replace(r"\s+", " ").strip()[:40])
            if len(hits) >= 3:
                break
    return hits


def web_gap_reason(domain, industry, city, result):
    web_status = result.get("web_status", "UNKNOWN")
    if web_status == "NO_WEBSITE":
        return "Operating {0} in {1} ({2}); no website at all".format(industry or "local business", city or "unknown", domain)
    if web_status == "BROKEN_WEBSITE":
        return "Operating {0} in {1} ({2}); broken/dead website".format(industry or "local business", city or "unknown", domain)
    if web_status == "PLACEHOLDER_WEBSITE":
        return "Operating {0} in {1} ({2}); placeholder/parking page only".format(industry or "local business", city or "unknown", domain)
    if web_status == "REAL_WEBSITE":
        return "Operating {0} in {1} ({2}); has real website - not a client".format(industry or "local business", city or "unknown", domain)
    return "Operating {0} in {1} ({2}); web presence unclear".format(industry or "local business", city or "unknown", domain)


def analyze_web_presence(url, domain):
    cls = classify(url, domain)
    status = cls["web_status"]
    if status == "REAL_WEBSITE":
        return {"accessible": True, "https": True,
                "signals": ["real website: {0}".format(cls["web_reason"])],
                "score": 0, "ok": False}
    if status == "NO_WEBSITE":
        return {"accessible": False, "https": False,
                "signals": ["no website/domain"],
                "score": 90, "ok": True}
    if status == "BROKEN_WEBSITE":
        return {"accessible": False, "https": False,
                "signals": ["broken/dead site"],
                "score": 80, "ok": True}
    if status == "PLACEHOLDER_WEBSITE":
        return {"accessible": True, "https": True,
                "signals": ["placeholder: {0}".format(cls["web_reason"])],
                "score": 70, "ok": True}
    return {"accessible": False, "https": False,
            "signals": ["ambiguous/unknown: {0}".format(cls["web_reason"])],
            "score": 50, "ok": False}
