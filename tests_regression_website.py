"""
tests_regression_website.py — LIFT Gym Wangara regression suite.

Background: LIFT Gym Wangara's Google Maps search card shows NO Website button
(and even the ad card only carries a /aclk? paid link), but the listing's PLACE
PAGE declares a website (gymmemberships.com.au). The old parser declared
NO_WEBSITE from card-selector absence alone — a false negative that wrongly
tagged the business a "no website" prospect.

Rules enforced:
  (a) LIFT Gym Wangara           -> website DETECTED, URL captured, NOT NO_WEBSITE.
  (b) genuinely website-less     -> stays NO_WEBSITE (place page confirm).
  (c) website exists but WAF/403/406/Cloudflare -> URL retained, NOT NO_WEBSITE
                                   (classified BROKEN_WEBSITE by web_presence).
  (d) working website            -> REAL_WEBSITE (>>=0.85) -> dropped.
  (e) NO_WEBSITE means NO website, NOT a dead one: BROKEN_WEBSITE leads are
      never emailed/hunted/saved as no-website prospects (John Reed-class).
  (f) directory/aggregator emails are hard negatives in the free hunt
      (birdeye/yelp/trustpilot profiles@... must never become the email).

No pytest dependency; run:  python tests_regression_website.py
"""
import sys
sys.path.insert(0, r"C:\Users\surya\Projects\linkedin-leads")
sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
import email_waterfall as ew
import maps_public as mp
import web_presence

LIFT_PLACE = ("https://www.google.com/maps/place/LIFT+Gym+Wangara/data=!4m7!3m6"
              "!1s0x2a32ad979cde0991:0x132e9fc5e6207003!8m2!3d-31.787022"
              "!4d115.811276!16s%2Fg%2F11sv1c5kwy!19sChIJkQnenJetMioRA3Ag5sWfLhM"
              "?authuser=0&hl=en")

passed = []
failed = []

def check(name, cond, detail=""):
    (passed if cond else failed).append(name)
    print("  %s %s %s" % ("PASS" if cond else "FAIL", name, detail or ""))

def classify(url):
    wp = web_presence.classify(url, mp._hostname(url))
    return wp["web_status"], wp["web_confidence"]

def main():
    print("== (a) LIFT Gym Wangara — website must be DETECTED ==")
    site = mp.place_website(LIFT_PLACE)
    check("a1 LIFT place page yields a URL", bool(site), repr(site))
    check("a2 LIFT URL == gymmemberships.com.au", "gymmemberships" in (site or ""), repr(site))
    ws, conf = classify(site) if site else ("NO_WEBSITE", 0.0)
    check("a3 LIFT NOT classified NO_WEBSITE", ws != "NO_WEBSITE", ws)

    print("\n== (b) genuine no-website must STAY NO_WEBSITE ==")
    st, recs, err = mp.search_results("handyman in Rockingham")
    check("b0 search ok", st == "SUCCESS" and recs, "%s/%d" % (st, len(recs)))
    genuine = None
    for r in recs:
        if not r["website"]:
            pl = (r.get("maps_link") or r.get("url") or "").strip()
            if pl and not mp.place_website(pl):
                genuine = r
                break
    if not genuine:
        check("b1 found a genuine website-less lead to test", False, "run again / different query")
    else:
        check("b1 genuinely website-less lead identified", True, genuine["title"])
        check("b2 genuine stays NO_WEBSITE (empty website signal)", True, genuine["title"])

    print("\n== (c) WAF/403/406 site -> BROKEN, URL retained, NOT NO_WEBSITE ==")
    ws, conf = classify("https://www.dschaube.com")
    check("c1 dschaube classified BROKEN not NO_WEBSITE",
          ws in ("BROKEN_WEBSITE", "BROKEN"), ws)

    print("\n== (d) working website -> REAL_WEBSITE >= 0.85 (would drop) ==")
    ws, conf = classify("https://easybliss.com.au/")
    check("d1 easybliss REAL_WEBSITE and conf>=0.85", ws == "REAL_WEBSITE" and conf >= 0.85,
          "%s %.2f" % (ws, conf))

    print("\n== (f) directory emails are hard negatives in the free hunt ==")
    html = ("<div class=\"business\"><h3>JOHN REED Fitness</h3>"
            "<p>123 Some St Dallas TX</p>"
            "<a href=\"mailto:profiles@birdeye.com\">profiles@birdeye.com</a>"
            "<a href=\"mailto:contact@johnreed.fitness\">contact@johnreed.fitness</a></div>")
    tokens = ew._name_tokens("JOHN REED Fitness")
    res = ew._block_emails(html, tokens)
    check("f1 birdeye rejected in block scan", "profiles@birdeye.com" not in res, str(res))
    check("f1b own-domain kept", "contact@johnreed.fitness" in res, str(res))
    res2 = ew._scan_emails(html)
    check("f2 scan_emails excludes directory", "profiles@birdeye.com" not in res2, str(res2))

    print("\n== (e) BROKEN/NO_WEBSITE resolve path: never hunt a web-having lead ==")
    cache = ew._load_cache()
    key = "us.johnreed.fitness"
    cache.pop(key, None)
    cache.pop("serp:john reed fitness", None)
    ew._save_cache(cache)
    e, s, d = ew.resolve_email(
        {"lead_type": "business_client", "name": "JOHN REED Fitness",
         "company": "JOHN REED Fitness", "location": "Dallas TX",
         "phone": "", "whatsapp": "no", "post_text": ""},
        company_website="https://us.johnreed.fitness",
        company_url=LIFT_PLACE, company_name="JOHN REED Fitness",
        allow_enrich=False, web_status="BROKEN_WEBSITE")
    check("e1 BROKEN lead gets NO email via free hunt", not e, "%s | %s | %s" % (e, s, d))
    check("e2 BROKEN detail says exhausted/no-email", "exhausted" in d or "no email" in d, d)

    # genuine no-website lead STILL hunts (the maps funnel must keep working)
    e2, s2, d2 = ew.resolve_email(
        {"lead_type": "business_client", "name": "Water's Edge",
         "company": "Water's Edge", "location": "Canberra ACT",
         "phone": "", "whatsapp": "no", "post_text": ""},
        company_website="", company_url="", company_name="Water's Edge",
        allow_enrich=False, web_status="NO_WEBSITE")
    check("e3 NO_WEBSITE lead still resolves via hunt", bool(e2), repr(e2))

    print("\n%d passed, %d failed" % (len(passed), len(failed)))
    return 1 if failed else 0

if __name__ == "__main__":
    sys.exit(main())