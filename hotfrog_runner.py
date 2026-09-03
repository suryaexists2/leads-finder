"""
hotfrog_runner.py — CONTINUOUS Hotfrog lead sweeps in VOLUME mode, run from cmd
in its own window. Every listing whose Hotfrog detail page lists no website and
that carries a phone is processed: free email hunt -> saved -> Telegram card
(email found OR phone-only). Loop runs continuously, compact sleeps, persisted
notification dedup so the same business is never re-notified.

$0 throughout, nothing invented. Transparent: card marks web_status NO_WEBSITE
with the "no website listed" reason; a business that actually runs a site the
directory simply doesn't list may slip through — that is inherent to using the
directory's own "website listed?" signal as the definition.
"""
import json
import os
import random
import re
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import engine
import hotfrog_leads
import storage
import telegram_bot

try:
    import config
    config.MAPS_PUBLIC_PROFILE_DIR = os.path.join(
        config.DATA_DIR, "maps_public_edge_profile_hotfrog")
except Exception:
    pass

CATS = [
    ("hotels", "Hotels"), ("restaurants", "Restaurants"), ("cafes", "Cafes"),
    ("plumbers", "Plumbers"), ("electricians", "Electricians"),
    ("building-contractors", "Contractors"), ("roofing", "Roofing"), ("hvac", "HVAC"),
    ("cleaning-services", "Cleaning Services"), ("gardeners", "Landscaping"),
    ("pest-control", "Pest Control"), ("car-repairs", "Auto Repair"),
    ("car-detailing", "Car Detailing"), ("towing-services", "Towing"),
    ("barber-shops", "Barber Shops"), ("hair-salons", "Hair Salons"),
    ("beauty-parlours", "Beauty Salons"), ("fitness-centres", "Gyms"),
    ("personal-trainers", "Personal Trainers"), ("real-estate-agents", "Real Estate Agencies"),
    ("photographers", "Photographers"), ("event-planners", "Event Planners"),
    ("travel-agents", "Travel Agencies"), ("caterers", "Catering"), ("bakers", "Bakeries"),
]

CAT_VARIANTS = {
    "plumbers": ["plumbers", "plumber"],
    "electricians": ["electricians", "electrical-contractors"],
    "cleaning-services": ["cleaning-services", "house-cleaning"],
    "beauty-parlours": ["beauty-parlours", "beauty-salons"],
    "barber-shops": ["barber-shops", "barbers"],
    "car-repairs": ["car-repairs", "auto-repairs", "car-mechanics"],
    "hvac": ["hvac", "air-conditioning", "ac-repair"],
    "roofing": ["roofing", "roofers"],
    "landscaping": ["gardeners", "landscaping"],
    "catering": ["caterers", "catering-services"],
}

CITIES = [
    ("mumbai", "maharashtra"), ("delhi", "delhi"), ("bangalore", "karnataka"),
    ("chennai", "tamil-nadu"), ("pune", "maharashtra"), ("hyderabad", "telangana"),
    ("kolkata", "west-bengal"), ("lucknow", "uttar-pradesh"),
    ("ahmedabad", "gujarat"), ("jaipur", "rajasthan"),
    ("indore", "madhya-pradesh"), ("bhopal", "madhya-pradesh"),
    ("nagpur", "maharashtra"), ("thane", "maharashtra"), ("navi-mumbai", "maharashtra"),
    ("surat", "gujarat"), ("vadodara", "gujarat"), ("rajkot", "gujarat"),
    ("coimbatore", "tamil-nadu"), ("madurai", "tamil-nadu"),
    ("visakhapatnam", "andhra-pradesh"), ("vijayawada", "andhra-pradesh"),
    ("mysore", "karnataka"), ("hubli", "karnataka"),
    ("kochi", "kerala"), ("thiruvananthapuram", "kerala"), ("kollam", "kerala"),
    ("varanasi", "uttar-pradesh"), ("kanpur", "uttar-pradesh"),
    ("agra", "uttar-pradesh"), ("noida", "uttar-pradesh"), ("ghaziabad", "uttar-pradesh"),
    ("dehradun", "uttarakhand"), ("chandigarh", "chandigarh"),
    ("amritsar", "punjab"), ("jalandhar", "punjab"), ("ludhiana", "punjab"),
    ("panipat", "haryana"), ("gurgaon", "haryana"), ("faridabad", "haryana"),
    ("jodhpur", "rajasthan"), ("udaipur", "rajasthan"), ("ajmer", "rajasthan"),
    ("patna", "bihar"), ("ranchi", "jharkhand"), ("bhubaneswar", "odisha"),
    ("guwahati", "assam"), ("raipur", "chhattisgarh"), ("goa", "goa"),
    ("nashik", "maharashtra"), ("aurangabad", "maharashtra"), ("kolhapur", "maharashtra"),
]

def _expand_cats():
    out = []
    for slug, label in CATS:
        out.append((slug, label))
        for v in CAT_VARIANTS.get(slug, []):
            if v != slug:
                out.append((v, label))
    return out

_CATS_ALL = _expand_cats()

QUERIES = [(st, slug, ci, "in", label)
           for ci, st in CITIES for slug, label in _CATS_ALL]

CYCLE_SLEEP_S = 30.0
QUERY_SLEEP_RANGE = (1.0, 2.0)
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hotfrog_runner.log")
NOTIF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hotfrog_notified.json")


class _L:
    def __init__(self, path):
        self.f = open(path, "a", encoding="utf-8")
        self.f.write("\n==== Hotfrog volume runner START %s ====\n"
                     % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        self.f.flush()

    def write(self, line):
        try:
            self.f.write("[%s] %s\n" % (datetime.now().strftime("%H:%M:%S"), line))
            self.f.flush()
        except Exception:
            pass


log = _L(LOG)


def _load_keys():
    try:
        if os.path.exists(NOTIF):
            with open(NOTIF, encoding="utf-8") as f:
                return set(json.load(f))
    except Exception:
        pass
    return set()


def _save_keys(keys):
    try:
        with open(NOTIF, "w", encoding="utf-8") as f:
            json.dump(sorted(keys), f)
    except Exception:
        pass


def _notify_phone(lead, keys):
    p10 = hotfrog_leads._norm_phone(lead.get("phone", ""))
    nname = re.sub(r"[^a-z0-9]", "", (lead.get("name", "") or "").lower())
    key = "hf:" + p10 + ":" + nname
    if key in keys:
        log.write("   SKIP(dup) %s | %s" % (lead["name"], lead.get("phone", "")))
        return
    try:
        if telegram_bot.send_lead(lead):
            keys.add(key)
            _save_keys(keys)
            log.write("   TELEGRAM(phone) %s %s | %s"
                      % (lead["name"], lead.get("phone", ""), lead.get("location", "")))
        else:
            log.write("   TGSEND_FAIL %s %s" % (lead["name"], lead.get("phone", "")))
    except Exception as e:
        log.write("   TGERR %s: %s" % (type(e).__name__, str(e)[:120]))


def main():
    keys = _load_keys()
    pass_idx = 0
    while True:
        pass_idx += 1
        log.write("=== PASS %d start (%d queries) ===" % (pass_idx, len(QUERIES)))
        for i, q in enumerate(QUERIES, 1):
            try:
                summary, saved, em_leads, noem_leads, incon = hotfrog_leads.run_batch(
                    q[0], q[1], q[2], save=True, volume=True, country=q[3],
                    category_label=q[4])
                for l in em_leads:
                    engine._notify_new_lead(l)
                for l in noem_leads:
                    _notify_phone(l, keys)
                log.write(
                    "P%02d/%02d [%s] %s|%s|%s @%-15s raw=%d qualify=%d cand=%d email=%d "
                    "saved=(email %d, no-email %d, duprej %d) tg=%d%s"
                    % (pass_idx, i, q[3].upper(), q[0][:10], q[1][:16], q[2][:12], q[4],
                       summary.get("raw", 0), summary.get("qualified", 0),
                       summary.get("candidates", 0), summary.get("email_found", 0),
                       saved[0], saved[1], saved[2],
                       len(em_leads) + len([x for x in noem_leads]),
                       ("  EMAIL:" + em_leads[0]["name"] + " <" + em_leads[0]["email"] + ">")
                       if em_leads else ""))
            except Exception as e:
                log.write("P%d/%02d %s|%s|%s ERROR %s: %s"
                          % (pass_idx, i, q[0], q[1], q[2],
                             type(e).__name__, str(e)[:200]))
            time.sleep(random.uniform(*QUERY_SLEEP_RANGE))
        log.write("=== PASS %d done; sleep %ds ===" % (pass_idx, int(CYCLE_SLEEP_S)))
        time.sleep(CYCLE_SLEEP_S)


if __name__ == "__main__":
    main()