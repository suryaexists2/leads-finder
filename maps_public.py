"""
maps_public.py — KEYLESS, $0 Google Maps discovery via a REAL headless Edge.

Why a real browser: from this host Google serves an anti-bot payload (tiny
non-JSON blob / consent shell / empty SSR) to every KEYLESS HTTP client — both
plain curl_cffi probes and the MIT reverse-engineered "/search?tbm=map&pb=..."
implementation (promisingcoder/GoogleMapsCollector, 2026-01-23 + its ")]}'"
protocol) returned ZERO businesses. Only a REAL browser build executes the JS,
passes the checks, and renders the actual Maps result cards.

Implementation: the installed Edge, headless, as a one-shot DOM dump:
    msedge --headless=new --virtual-time-budget=N --dump-dom URL
`--virtual-time-budget` fast-forwards JS timers so the async-loaded results are
rendered before the DOM is dumped (~450 KB, ~2-6 s/query, no GUI, no focus
stealing). Edge binary is already installed; NO new Python dependency, NO API
key, NO Apify — maps cost is exactly $0 and the $0.15 daily Apify budget is
never touched or reserved, so Google Maps runs even after that budget exhausts.

Records come out in the EXACT keys apify_client._norm_maps_record produces
(title, url, category, address, phone, rating, reviews, website, domain,
maps_link), so the downstream pipeline — per-query dedup, web-presence
classification, qualification, email waterfall, Telegram — is unchanged.

DOM card format (current Google Maps search results):
  <div role="article" class="Nv2PK ...">                     one card/business
    <a class="hfpxzc" aria-label="NAME"
       href=".../maps/place/NAME/data=!4m7!3m6!1s0xHEX:0xHEX...
             !8m2!3dLAT!4dLNG!16s%2Fg%2FFTID!19sChIJ...">     place ids + coords
    <span class="xxVWCe">NAME</span>
    rating <span role="img" aria-label="4.8 stars"><span class="MW4etd">4.8</span>
    detail <span><span>CATEGORY</span> ... <span>ADDRESS</span></span>
    phone  <span class="UsdlK">+1 305-440-0878</span>         present only if any
    website <a class="lcr4fd ..." href="https://real/site">     present ONLY when
                                                              the listing has a
                                                              website -> ABSENCE =
                                                              the NO_WEBSITE
                                                              signal, same as the
                                                              Apify listing field
Actually the field order in the emitted DOM is:
    <a class="hfpxzc" aria-label="NAME" href="...place...">...
Specific selectors are kept deliberately simple (class-based) so a small Google
layout change degrades to EMPTY/SUCCESS-zero rather than a hard crash.

Status vocabulary mirrors apify_client actor states so the engine/health
handling treats public mode identically:
  SUCCESS / START_FAILED / HTTP_4XX / TIMEOUT / ACTOR_FAILED / BUDGET / NO_KEY
"""
import html
import os
import re
import subprocess
import time
import urllib.parse

import config

STATUS_OK = "SUCCESS"
STATUS_START_FAILED = "START_FAILED"
STATUS_HTTP_4XX = "HTTP_4XX"
STATUS_TIMEOUT = "TIMEOUT"
STATUS_FAILED = "ACTOR_FAILED"

EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)

_CARD_START = '<div role="article" class="Nv2PK'
_CARD_WEBSITE_PAT = re.compile(r"<a\b[^>]*>")
_SOCIAL_HOSTS = ("facebook.com", "instagram.com", "twitter.com", "x.com",
                 "linkedin.com", "youtube.com", "youtu.be", "t.me", "wa.me",
                 "maps.google.com", "googleusercontent.com")


def _edge_binary():
    p = (getattr(config, "MAPS_PUBLIC_EDGE_PATH", "") or "").strip()
    if p and os.path.exists(p):
        return p
    for c in EDGE_CANDIDATES:
        if os.path.exists(c):
            return c
    return ""


def _profile_dir():
    p = getattr(config, "MAPS_PUBLIC_PROFILE_DIR", "") or ""
    if not p:
        p = os.path.join(config.DATA_DIR, "maps_public_edge_profile")
    return p


def _search_url(query, hl="en"):
    q = urllib.parse.quote(query, safe="+")
    return "https://www.google.com/maps/search/{0}/?hl={1}".format(q, hl)


def _run_dump(query, timeout_s=None, vt_ms=None, direct_url=None):
    """One headless-Edge DOM dump for a maps search query (or a direct URL
    when `direct_url` is given).

    Returns (status, html_text, error). status aligns with apify_client.so
    No budget, no spend, no cache writes — purely a local browser.
    """
    binary = _edge_binary()
    if not binary:
        return STATUS_FAILED, "", "maps-public: Edge binary not found (checked config.MAPS_PUBLIC_EDGE_PATH then standard install paths)"
    if timeout_s is None:
        timeout_s = float(getattr(config, "MAPS_PUBLIC_TIMEOUT_S", 75))
    if vt_ms is None:
        vt_ms = int(getattr(config, "MAPS_PUBLIC_VIRTUAL_TIME_MS", 9000))
    prof = _profile_dir()
    try:
        os.makedirs(prof, exist_ok=True)
    except Exception as e:
        return STATUS_FAILED, "", "maps-public: profile dir error: {0}".format(e)
    url = direct_url or _search_url(query or "")
    cmd = [binary, "--headless=new", "--disable-gpu", "--mute-audio",
           "--no-first-run", "--no-default-browser-check",
           "--disable-component-update", "--disable-background-networking",
           "--user-data-dir={0}".format(prof),
           "--virtual-time-budget={0}".format(vt_ms),
           "--dump-dom", url]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return STATUS_TIMEOUT, "", "maps-public: headless Edge timed out after {0}s".format(int(timeout_s))
    except Exception as e:
        return STATUS_START_FAILED, "", "maps-public: Edge launch failed: {0}".format(e)
    if r.returncode != 0:
        tail = (r.stderr or b"").decode("utf-8", errors="replace")[-300:]
        return STATUS_START_FAILED, "", "maps-public: Edge exit code {0}: {1}".format(r.returncode, tail.strip() or "no stderr")
    html_text = (r.stdout or b"").decode("utf-8", errors="replace")
    low = html_text.lower()
    if _CARD_START not in html_text:
        if any(k in low for k in ("unusual traffic", "verify you're a real person",
                                  "verify you are", "captcha", "enable javascript")):
            return STATUS_FAILED, html_text, "maps-public: Google demands verification/captcha for this query"
        if html_text and ("maps" in low or "initialization" in low):
            return STATUS_OK, html_text, ""   # real page, legitimately 0 cards
        return STATUS_FAILED, html_text, "maps-public: empty/non-Maps response from Google (len {0})".format(len(html_text))
    return STATUS_OK, html_text, ""


def _strip_google_track(url):
    """Keep the real business URL, drop Google's utm/tracking query params."""
    if not url:
        return ""
    url = html.unescape(url)
    try:
        u = urllib.parse.urlparse(url)
    except Exception:
        return url
    drop = ("utm_", "g_", "yclid", "fbclid", "gclid", "gad", "rwg_token")
    q = [(k, v) for k, v in urllib.parse.parse_qsl(u.query) if not k.startswith(drop)]
    u = u._replace(query=urllib.parse.urlencode(q))
    return urllib.parse.urlunparse(u)


def _hostname(url):
    if not url:
        return ""
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


def _is_external_site(url):
    """True only for a genuine business http(s) URL. Excludes Google-owned,
    social profiles, and paid ad/click-through links (Maps ads render
    `/aclk?...gclid=` links that are NOT the business's own website)."""
    if not url:
        return False
    low = url.lower()
    if "/aclk" in low or "gclid" in low or "googleadservices" in low:
        return False
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return False
    if host.startswith("www."):
        host = host[4:]
    if not host or host in _SOCIAL_HOSTS:
        return False
    if host.endswith(".google.com") or host.endswith("google.co"):
        return False
    return True


def _card_website(card):
    """Card-level website button. Primary selector is the class `lcr4fd`
    (the Website anchor on organic cards); Google sometimes changes classes or
    renders the site via a different button, so also accept any card anchor
    labelled Website/Open website whose href is a genuine external URL.
    Ad/social/Google/click-through links are excluded by _is_external_site."""
    for m in _CARD_WEBSITE_PAT.finditer(card):
        seg = card[m.start():m.start() + 1400]
        cls_m = re.search(r'\bclass="([^"]*)"', seg)
        aria_m = re.search(r'aria-label="([^"]*)"', seg)
        cls_s = (cls_m.group(1) if cls_m else "") or ""
        aria_l = ((aria_m.group(1) if aria_m else "") or "").lower()
        if "lcr4fd" not in cls_s and not (aria_l.startswith("website") or aria_l.startswith("open website")):
            continue
        if any(k in aria_l for k in ("call phone", "direction", "share", "save", "plus code")):
            continue
        href_m = re.search(r'\bhref="([^"]+)"', seg)
        if href_m:
            u = _strip_google_track(href_m.group(1))
            if _is_external_site(u):
                return u
    return ""


def extract_website_from_dom(html_text):
    """Extract the declared website from a Maps PLACE PAGE (detail panel).
    Google renders the Website button today as either:
      <a class="lcr4fd ..." aria-label="Open website" href="https://real/">
      <a class="CsEnBe ..." aria-label="Website: example.com" href="https://example.com/">
    Phone/Share/Save/Call buttons reuse lcr4fd (tel:/google links) — filtered
    via _is_external_site + aria-label negatives. Returns '' (the authoritative
    NO_WEBSITE signal) when the panel shows no website or the page is unparsable.
    """
    if not html_text:
        return ""
    for m in _CARD_WEBSITE_PAT.finditer(html_text):
        seg = html_text[m.start():m.start() + 1400]
        cls_m = re.search(r'\bclass="([^"]*)"', seg)
        aria_m = re.search(r'aria-label="([^"]*)"', seg)
        cls_s = (cls_m.group(1) if cls_m else "") or ""
        aria_l = ((aria_m.group(1) if aria_m else "") or "").lower()
        if "lcr4fd" not in cls_s and not (aria_l.startswith("website") or aria_l.startswith("open website")):
            continue
        if any(k in aria_l for k in ("call phone", "direction", "address:", "phone:", "plus code", "share", "save")):
            continue
        href_m = re.search(r'\bhref="([^"]+)"', seg)
        if href_m:
            u = _strip_google_track(href_m.group(1))
            if _is_external_site(u):
                return u
        if aria_l.startswith("website:"):
            dom = aria_l[len("website:"):].strip()
            dom = re.split(r"[\s,]", dom, 1)[0].strip().rstrip("/")
            if dom and "." in dom and " " not in dom:
                return "https://" + dom
    return ""


def place_website(url, timeout_s=None, vt_ms=None):
    """Authoritative website check for ONE listing via its PLACE PAGE — the same
    detail panel whose "Website" button the user sees. Returns the declared
    website URL, or '' if the panel shows none (or the page cannot be parsed).

    Status is deliberately ignored: whatever rendered, we parse. One extra Edge
    dump per website-less lead in public mode; $0, no Apify, never an email.
    """
    if not url:
        return ""
    _st, htm, _err = _run_dump("", timeout_s=timeout_s, vt_ms=vt_ms, direct_url=url)
    return extract_website_from_dom(htm)


def parse_maps_dom(html_text):
    """Parse business cards from a dumped Maps search DOM -> list of records in
    apify_client._norm_maps_record key shape (title/url/category/address/phone/
    rating/reviews/website/domain/maps_link)."""
    out = []
    if not html_text or _CARD_START not in html_text:
        return out
    for card in html_text.split(_CARD_START)[1:]:
        try:
            rec = _parse_one_card("<div" + card)
        except Exception:
            continue
        if rec and rec.get("title"):
            out.append(rec)
        if len(out) >= int(getattr(config, "MAPS_DISCOVERY_RESULTS", 6)):
            break
    return out


def _parse_one_card(card):
    am = re.search(r'class="hfpxzc"[^>]*aria-label="([^"]+)"[^>]*href="([^"]+)"', card)
    if am:
        name, href = am.group(1), am.group(2)
    else:
        am = re.search(r'href="([^"]+)"[^>]*aria-label="([^"]+)"', card)
        if not am:
            return None
        name, href = am.group(2), am.group(1)
    title = re.sub(r"\s+", " ", html.unescape(name).strip())
    website = _card_website(card)
    phone = ""
    pm = re.search(r'class="UsdlK">([^<>]{3,40})<', card)
    if pm:
        phone = html.unescape(pm.group(1)).strip()
    rating = None
    rm = re.search(r'class="MW4etd"[^>]*>([\d.,]+)<', card)
    if rm:
        try:
            rating = float(rm.group(1).replace(",", "."))
        except Exception:
            rating = None
    cat = ""
    addr = ""
    cm = re.search(r'<div class="W4Efsd"><span><span>([^<>]{1,80})</span></span>', card)
    if cm:
        cat = html.unescape(cm.group(1)).strip()
    dm = re.search(r'<span aria-hidden="true">[^<]*</span>\s*<span>([^<>]{1,160})</span>', card)
    if dm:
        addr = html.unescape(dm.group(1)).strip()
    hex_id = ""
    hm = re.search(r"!1s0x([0-9a-f]{4,20}):0x([0-9a-f]{4,20})", href)
    if hm:
        hex_id = "0x{0}:0x{1}".format(hm.group(1), hm.group(2))
    ftid = ""
    fm = re.search(r"!16s%2Fg%2F([A-Za-z0-9_-]+)", href)
    if fm:
        ftid = fm.group(1)
    chij = ""
    cm = re.search(r"!19s(ChIJ[0-9a-zA-Z_-]*)", href)
    if cm:
        chij = cm.group(1)
    lat = lng = None
    lm = re.search(r"!8m2!3d([-\d.]+)!4d([-\d.]+)", href)
    if lm:
        try:
            lat, lng = float(lm.group(1)), float(lm.group(2))
        except Exception:
            lat = lng = None
    if not website and chij:
        pass
    return {
        "title": title,
        "url": html.unescape(href),
        "category": cat,
        "address": addr,
        "phone": phone,
        "rating": rating,
        "reviews": 0,
        "website": website,
        "domain": _hostname(website),
        "maps_link": html.unescape(href),
        "_hex_id": hex_id,
        "_ftid": ftid,
        "_chij": chij,
        "_lat": lat,
        "_lng": lng,
    }


def search_results(query, num_results=None):
    """Public-mode equivalent of apify_client.google_maps_search.

    Runs ONE "<category> in <city>" query via headless Edge, returns
    (status, records, error). $0, no Apify, no budget reserve.
    """
    retries = max(0, int(getattr(config, "MAPS_PUBLIC_RETRIES", 1)))
    delay = float(getattr(config, "MAPS_PUBLIC_RETRY_DELAY_S", 2))
    last_status, last_html, last_err = STATUS_FAILED, "", ""
    for attempt in range(retries + 1):
        st, htm, err = _run_dump(query)
        last_status, last_html, last_err = st, htm, err
        if st != STATUS_OK:
            if attempt < retries:
                time.sleep(delay)
            continue
        recs = parse_maps_dom(htm)
        if recs:
            return STATUS_OK, recs, ""
        # Zero cards: refresh cookies once (fresh Edge session), then report
        # SUCCESS-zero only if Google actually rendered a Maps page.
        if attempt < retries:
            time.sleep(delay)
            continue
        return STATUS_OK, [], ""
    if any(k in (last_err or "") for k in ("verification", "captcha", "unusual")):
        return STATUS_FAILED, [], last_err
    return last_status, [], last_err or "maps-public: query failed after retries"