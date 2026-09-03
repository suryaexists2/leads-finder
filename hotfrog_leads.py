"""
hotfrog_leads.py — FREE lead list from Hotfrog directory (name + phone + address),
scoped to US local businesses whose website is NOT listed there.

Discovery path:  /search/{state}/{category}/{city}  (plain curl, SSR HTML, $0).
Every no-website candidate is CONFIRMED through the same authoritative guard the
Maps pipeline uses — the Google Maps place page (place_website) — because Hotfrog
listings can omit a website the business actually has (verified: e.g. Celebrity
Plumbers, 25-yr LA business, real site celebrityplumbers.com, Hotfrog shows none).
So "listed on Hotfrog as no website" alone is NOT proof of "has no website".

Stages per query:
  1. search page  -> raw listings (name, slug, phone, address)
  2. quality      -> phone present, valid address, city/state, no dup phone (batch)
  3. detail page  -> reject listings whose Hotfrog detail page DOES show a website
  4. maps verify  -> reject if Google Maps place page declares a website
  5. emit TARGETS -> genuine no-website leads (name, phone, city, state, addr)
"""
import html
import os
import re
import subprocess
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import maps_public

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "Chrome/120.0.0.0 Safari/537.36")

_PARTNER = ("hotfrog", "centralindex", "locafy", "newfold", "jooble", "here.com",
            "maxcdn", "bootstrapcdn", "flagma", "facebook", "instagram", "twitter",
            "linkedin", "youtube", "t.me", "wa.me", "trustpilot", "w3.org",
            "schema.org", "jsdelivr", "cloudflare", "gstatic", "googleapis",
            "yourwebsite", "site.com", "example", "g.page", "preview.google")

_TEL = re.compile(r'href="tel:([^"]+)"')
_NAME = re.compile(r'data-yext-click="name"[^>]*>\s*<strong>([^<]+)</strong>')
_SLUG = re.compile(r'<a href="(/company/[^"]+)"[^>]*data-yext-click="name"[^>]*>\s*<strong')
_ADDR = re.compile(r'<span class="small">\s*([^<]{4,140}?)</span>')
_SITE_HREF = re.compile(r'href="(https?://[^"]+)"')
_NAMEID = re.compile(r'data-id="([^"]+)"')

BASE = {"com": "https://www.hotfrog.com", "in": "https://www.hotfrog.in"}

_IN_STATE = {"andhra pradesh", "arunachal pradesh", "assam", "bihar",
             "chhattisgarh", "goa", "gujarat", "haryana", "himachal pradesh",
             "jammu and kashmir", "jharkhand", "karnataka", "kerala",
             "madhya pradesh", "maharashtra", "manipur", "meghalaya", "mizoram",
             "nagaland", "odisha", "punjab", "rajasthan", "sikkim",
             "tamil nadu", "telangana", "tripura", "uttar pradesh",
             "uttarakhand", "west bengal", "delhi", "chandigarh"}


def _fetch(url, tries=3, delay=1.5):
    for _ in range(tries):
        try:
            r = subprocess.run(
                ["curl", "-sSL", "-m", "25", "-A", UA,
                 "-H", "Accept-Language: en-US,en;q=0.9",
                 "-w", "\n@@RC:%{http_code}", url],
                capture_output=True, timeout=35)
            b = (r.stdout or b"").decode("utf-8", errors="replace")
            rc = b.rsplit("@@RC:", 1)[-1].strip() if "@@RC:" in b else ""
            body = b.split("@@RC:", 1)[0]
            if rc == "200" and len(body) > 10000:
                return body
        except Exception:
            pass
        time.sleep(delay)
    return ""


def _clean(s):
    return html.unescape(re.sub(r"\s+", " ", (s or "")).strip())


def _norm_phone(tel):
    n = re.sub(r"\D", "", (tel or ""))
    if len(n) >= 10:
        return n[-10:]
    return ""


def _addr_parts(addr):
    """Parse Hotfrog address into street(area), city, state, pin.

    Handles US "St, City, ST 62701" and India "St/Area, City, PIN6" with an
    optional "City, StateName, PIN6". City is the token right before the pin
    (or last token if no pin)."""
    toks = [t.strip() for t in _clean(addr).split(",") if t.strip()]
    if not toks:
        return None
    pin = ""
    m = re.search(r"(\d{5,6})$", toks[-1])
    if m:
        pin = m.group(1)
        toks[-1] = toks[-1][:m.start()].strip()
        if not toks[-1]:
            toks.pop()
    state = ""
    if toks and re.fullmatch(r"[A-Z]{2}", toks[-1]):
        state = toks[-1]
        toks.pop()
    elif toks and toks[-1].lower() in _IN_STATE:
        state = toks[-1]
        toks.pop()
    if not toks:
        return None
    city = toks.pop()
    street = ", ".join(toks)
    return {"street": street, "city": city, "state": state, "zip": pin}


def _detail_website(detail):
    for m in _SITE_HREF.finditer(detail):
        u = m.group(1)
        if "//" not in u:
            continue
        host = re.sub(r"^www\.", "", u.split("//", 1)[1].split("/", 1)[0]).lower()
        if not host or any(p in host for p in _PARTNER) \
           or host.endswith(".css") or ".ico" in host or "cdn" in host or "assets" in host:
            continue
        return u
    return ""


def _fmt_phone(d10, country="com"):
    if not d10:
        return ""
    if country == "in":
        return "+91 " + d10[:5] + " " + d10[5:]
    return "+1 (" + d10[:3] + ") " + d10[3:6] + "-" + d10[6:]


def search_listings(state, category, city, country="com"):
    """Stage 1: raw listings from the Hotfrog search page, parsed per row."""
    h = _fetch("%s/search/%s/%s/%s"
               % (BASE[country], urllib.parse.quote(state),
                  urllib.parse.quote(category), urllib.parse.quote(city)))
    if not h:
        return []
    out = []
    seen = set()
    for block in h.split('<div class="row">')[1:]:
        nm = _NAME.search(block)
        if not nm:
            continue
        data_id = ""
        mn = _NAMEID.search(nm.group(0))
        if mn:
            data_id = mn.group(1)
        if data_id in seen:
            continue
        seen.add(data_id)
        rec = {"name": _clean(re.sub(r"\s*\|\s*.*$", "", nm.group(1))),
               "slug": _SLUG.search(block).group(1) if _SLUG.search(block) else ""}
        tm = _TEL.search(block)
        if tm:
            rec["phone"] = _clean(tm.group(1)).rstrip("\\").rstrip('" ')
            rec["_phone10"] = _norm_phone(rec["phone"])
        am = _ADDR.search(block)
        if am:
            parts = _addr_parts(am.group(1))
            if parts:
                rec.update(parts)
        out.append(rec)
    return out


def _stage(name, recs, keep):
    return recs, [r for r in recs if keep(r)]


def qualify(rcs, state=None, city=None):
    """Stage 2: phone+address present, batch-unique phone, optional city/state."""
    seen = {}
    for r in rcs:
        p = _norm_phone(r.get("phone", ""))
        r["_phone10"] = p
    qu = [r for r in rcs
          if r.get("_phone10")
          and r.get("city")
          and r.get("state")]
    if state:
        qu = [r for r in qu if r["state"].upper() == state.upper()]
    if city:
        c0 = re.sub(r"[-_]+", " ", city).strip().lower()
        qu = [r for r in qu if re.sub(r"[-_]+", " ", r["city"]).strip().lower() == c0]
    return qu


def detail_keep(rc, country="com"):
    """Stage 3: reject listings whose Hotfrog detail page declares a website.

    If the detail page can't be fetched (broken/404 slug), do NOT drop the lead —
    the Google Maps place check (stage 4) stays the authoritative decision."""
    d = _fetch(BASE[country] + rc["slug"], tries=2)
    if not d:
        rc["_detail_unverified"] = True
        return True
    rc["_detail_site"] = _detail_website(d)
    return not rc["_detail_site"]


_STOP = set("plumbers plumber plumbing service services company company llc inc the a an of and repair repairs).").split() if False else {
    "plumbers", "plumber", "plumbing", "service", "services", "company", "llc",
    "inc", "the", "a", "an", "of", "and", "repair", "repairs", "hvac", "air"} | {
    "ltd", "co", "corp", "group", "systems", "solutions", "technologies"}


def _tokens(s):
    return [t for t in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(t) > 2 and t not in _STOP]


def _identity(have, want):
    """True when the maps card name plausibly IS the candidate business."""
    have, want = _tokens(have), _tokens(want)
    if not want:
        return False
    inter = set(have) & set(want)
    if inter:
        return len(inter) >= max(1, len(want) // 2)
    for t in want:
        if t in " ".join(have):
            return True
    return False


def maps_verify(rc, timeout_s=None, vt_ms=None):
    """Stage 4 (3-state): does the Google Maps place for THIS business declare a
    website?

    Returns:
      False        -> identity-confirmed place DOES have a website  (REJECT)
      None         -> no identity-confirming place found (INCONCLUSIVE -> manual)
      True         -> identity-confirmed place shows no website      (TARGET)
    """
    q = " ".join(x for x in (rc["name"], rc.get("street", ""), rc.get("city", ""), rc.get("state", "")) if x)
    url = "https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote(q)
    _st, htm, _err = maps_public._run_dump("", timeout_s=timeout_s, vt_ms=vt_ms, direct_url=url)
    recs = maps_public.parse_maps_dom(htm)
    matched = [r for r in recs if _identity(r.get("title", ""), rc["name"])]
    if not matched:
        rc["_maps_identity"] = [r.get("title", "")[:40] for r in recs[:3]]
        rc["_maps_conclusive"] = False
        return None
    site = maps_public.place_website(matched[0].get("url", ""), timeout_s=timeout_s, vt_ms=vt_ms)
    rc["_maps_verified"] = site
    rc["_maps_conclusive"] = True
    return not site


def build_targets(state, category, city, maps_confirm=True, city_any=False):
    """Full chain -> list of genuine no-website targets."""
    raw = search_listings(state, category, city)
    q = qualify(raw, state=state, city=None if city_any else city)
    targets = []
    inconclusive = []
    for rc in q:
        if not detail_keep(rc):
            continue
        if maps_confirm:
            v = maps_verify(rc)
            if v is False:
                continue
            if v is None:
                inconclusive.append(rc)
                continue
        targets.append({"name": rc["name"],
                        "slug": rc.get("slug", ""),
                        "phone": "+1 " + re.sub(
                            r"(\d{3})(\d{3})(\d{4})", r"(\1) \2-\3", rc["_phone10"]),
                        "location": "%s, %s" % (rc["city"], rc["state"]),
                        "address": "%s, %s, %s %s" % (rc["street"], rc["city"], rc["state"], rc["zip"]).strip()})
    return raw, q, targets, inconclusive


def _lead_from_target(t, state, category, city, country="com", category_label=None):
    name = t["name"]
    return {
        "name": name,
        "company": name,
        "company_slug": re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-"),
        "company_website": "",
        "website": "",
        "phone": t.get("phone", ""),
        "location": t.get("location", ""),
        "category": category_label or category,
        "lead_type": "business_client",
        "web_status": "NO_WEBSITE",
        "web_reason": "Point of contact found on %s listing; no website listed" % (category_label or category),
        "source": "hotfrog",
        "source_keyword": category,
        "profile_url": "hotfrog:" + t.get("slug", name),
        "source_url": "%s/search/%s/%s/%s"
                     % (BASE[country], state, category, city),
        "post_text": "",
    }


def _email_lead(lead):
    import email_waterfall
    try:
        em, src, det = email_waterfall.resolve_email(
            lead, web_status="NO_WEBSITE", company_website="")
    except Exception:
        em, src, det = "", "", "error"
    if em:
        lead["email"] = em
        lead["email_source"] = src
        lead["email_verified"] = "Verified"
    return lead


def run_batch(state, category, city, maps_confirm=True, save=True, city_any=False,
              volume=False, country="com", category_label=None):
    """Discover candidates for one Hotfrog query (country: com=US, in=India).

    volume=True  -> MAXIMUM FLOW: qualify by phone only, drop the identity Maps
                    guard, keep every candidate whose Hotfrog detail page shows
                    no website (the user's "website listed na ho" definition),
                    still run the free email hunt, save, and return leads for
                    notification (email found OR phone-only). Transparent flag
                    stays: web_status NO_WEBSITE / "no website listed".

    volume=False -> strict verified path: city match + identity-confirmed Maps
                    place check; only confirmed no-website targets survive.

    Returns (summary, saved, email_leads, noemail_leads, inconclusive).
    """
    import storage

    raw = search_listings(state, category, city, country=country)
    if volume:
        qu = [r for r in raw if r.get("_phone10")]
    else:
        qu = qualify(raw, state=state,
                     city=None if city_any else city)

    candidates = []
    inconclusive = []
    if volume:
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(detail_keep, rc, country): rc for rc in qu}
            for fu in as_completed(futs):
                rc = futs[fu]
                try:
                    if fu.result():
                        candidates.append(rc)
                except Exception:
                    pass
    else:
        for rc in qu:
            if not detail_keep(rc, country):
                continue
            v = maps_verify(rc)
            if v is False:
                continue
            if v is None:
                inconclusive.append(rc)
                continue
            candidates.append(rc)

    targets = []
    for rc in candidates:
        loc = ", ".join(x for x in (rc.get("street"), rc.get("city"),
                                    rc.get("state")) if x)
        targets.append({"name": rc["name"],
                        "slug": rc.get("slug", ""),
                        "phone": _fmt_phone(rc.get("_phone10", ""), country),
                        "location": loc,
                        "address": ", ".join(
                            x for x in (rc.get("street"), rc.get("city"),
                                        rc.get("state"), rc.get("zip")) if x)})

    leads = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for t in targets:
            lead = _lead_from_target(t, state, category, city,
                                     country=country, category_label=category_label)
            leads.append(ex.submit(_email_lead, lead))
        leads = [fu.result() for fu in as_completed(leads)]

    email_leads = [l for l in leads if l.get("email")]
    noemail_leads = [l for l in leads if not l.get("email")]

    summary = {"raw": len(raw), "qualified": len(qu),
               "candidates": len(candidates), "inconclusive": len(inconclusive),
               "email_found": len(email_leads)}
    saved_progress = (0, 0, 0)
    if save and (email_leads or noemail_leads):
        try:
            saved_progress = storage.add_leads(email_leads, noemail_leads)
        except Exception as e:
            summary["save_error"] = "%s: %s" % (type(e).__name__, str(e)[:200])
    return summary, saved_progress, email_leads, noemail_leads, inconclusive


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "test"
    if mode == "run":
        if len(sys.argv) < 5:
            print("usage: python hotfrog_leads.py run <state> <category> <city> [--volume] [--city-any] [--in] [--com]")
            sys.exit(2)
        st, cat, ci = sys.argv[2], sys.argv[3], sys.argv[4]
        volume = "--volume" in sys.argv[5:]
        city_any = "--city-any" in sys.argv[5:]
        country = "in" if "--in" in sys.argv[5:] else "com"
        print("== Hotfrog batch: %s/%s/%s%s%s" % (st, cat, ci,
              "  (volume)" if volume else "", "  (city-any)" if city_any else ""))
        summary, progress, em_leads, noem_leads, incon = run_batch(
            st, cat, ci, volume=volume, city_any=city_any, country=country)
        print("summary:", summary)
        print("saved (email, no_email, rejected_dups):", progress)
        try:
            import engine
            for l in em_leads:
                engine._notify_new_lead(l)
        except Exception:
            pass
        for l in em_leads:
            print("   EMAIL %-28s %-30s %s" % (l["name"][:28], l.get("email", ""), l.get("phone", "")))
            print("         addr: %s" % l.get("location", ""))
        for l in noem_leads:
            print("   NO_EMAIL %-28s %s" % (l["name"][:28], l.get("phone", "")))
            print("         addr: %s" % l.get("location", ""))
        if incon:
            print("   INCONCLUSIVE (manual check — NOT saved):")
            for r in incon:
                print("   ? %-28s %s, %s  cards=%s" % (r["name"][:28], r.get("city", ""), r.get("state", ""), r.get("_maps_identity") or []))
        sys.exit(0)
    elif mode == "test":
        queries = [("ca", "plumbers", "los-angeles"),
                   ("fl", "plumbers", "delray-beach")]
        for state, category, city in queries:
            print("=" * 100)
            print("== Hotfrog chain: %s/%s/%s" % (state, category, city))
            raw = search_listings(state, category, city)
            print("stage1 raw listings:", len(raw), "(by row, deduped)")
            for r in raw:
                print("   %-26s %-14s %s, %s" % (r["name"][:26], r.get("phone", ""), r.get("city", ""), r.get("state", "")))
            qu = qualify(raw, state=state, city=city)
            print("stage2 qualified (phone + city/state match):", len(qu))
            for r in qu:
                print("   %-26s %-14s %s, %s%s" % (r["name"][:26], "+1 " + re.sub(r"(\d{3})(\d{3})(\d{4})", r"(\1) \2-\3", r["_phone10"]), r["city"], r["state"], ((" | " + r["street"]) if r.get("street") else "")))
            det = [r for r in qu if detail_keep(r)]
            print("stage3 survived detail (no website on detail / unverifiable):", len(det), ", rejected w/ detail-site:",
                  [(r["name"], r.get("_detail_site")) for r in qu if r.get("_detail_site")])
            for r in det:
                print("   %-26s detail-site=%s%s" % (r["name"][:26], r.get("_detail_site") or "-", " (UNVERIFIED)" if r.get("_detail_unverified") else ""))
            mv, inc = [], []
            for r in det:
                v = maps_verify(r)
                if v is None:
                    inc.append(r)
                    print("   maps-verify %-26s -> INCONCLUSIVE (cards: %s)" % (r["name"][:26], r.get("_maps_identity") or []))
                elif v:
                    mv.append(r)
                    print("   maps-verify %-26s -> NO website  (TARGET)" % r["name"][:26])
                else:
                    print("   maps-verify %-26s -> HAS website -> REJECT (%s)" % (r["name"][:26], r.get("_maps_verified")))
            print("stage4 maps-confirmed-no-website TARGETS:", len(mv),
                  "| inconclusive (manual check):", [r["name"] for r in inc])
            for r in mv:
                print("   TARGET %-26s phone=+1 %s  addr=%s, %s, %s %s" % (r["name"][:26], r["_phone10"], r.get("street", ""), r["city"], r["state"], r.get("zip", "")))
            sys.stdout.flush()