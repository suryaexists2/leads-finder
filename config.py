"""
Central configuration & budgets for the no-login, active-intent lead engine.

Prices VERIFIED from live Apify API (FREE plan):
  maxMonthlyUsageUsd = 5.00 (wallet), 625 compute units, 50,000 proxy SERPs/mo.
  linkedin-company-search:        start $0.001 ; short-company $0.002/result
  linkedin-company-employees:     start $0.02  ; short-profile $0.004 (full $0.008, full+email $0.012)
  google-search-scraper:          ~$0.00105 / result
We budget conservatively so the engine never exhausts the free wallet mid-month.
"""
import os
import re

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# ── Daily budget guard (USD) ────────────────────────────────────────────────
# ~$5/month free wallet. Keep daily spend under ~$0.15 to never blow the month.
MAX_DAILY_APIFY_SPEND = 0.15

# Reserved (USD) always kept free for the Google Maps/Business channel — the
# PRIMARY no-website prospect source. Other Apify sources (SERP etc.) may only
# spend what remains ABOVE this reserve, so a SERP-heavy day can never starve
# Maps. Covers one Maps query (MAPS_DISCOVERY_RESULTS * ~0.0045 + 0.003 ≈ $0.03)
# plus a small margin. The hard cap MAX_DAILY_APIFY_SPEND still applies on top.
MAPS_BUDGET_RESERVE = 0.032

# ── Per-actor hard caps per day ─────────────────────────────────────────────
MAX_GOOGLE_SERP_RUNS_DAY = 15
MAX_COMPANY_SEARCH_COMPANIES_DAY = 40
MAX_EMPLOYEES_COMPANIES_DAY = 12
MAX_EMPLOYEES_PROFILES_PER_COMPANY = 3

# ── Paid contact-enrichment daily budget (Prospeo + Employees SHORT) ────────
# These cost money / external quota. The engine only spends on genuinely strong
# leads, up to this many per day, so cost stays bounded and cycles stay fast.
MAX_PAID_ENRICH_DAY = 3

# Automatic cycles run FREE paths ONLY (source email + website/domain email +
# MX validation). Set AUTO_PAID_ENRICH = True only to enable paid Employees /
# Prospeo enrichment automatically on strong leads. The Employees/Prospeo code
# stays available as an explicit high-value fallback regardless of this flag.
AUTO_PAID_ENRICH = False

# ── Verification levels ─────────────────────────────────────────────────────
EMAIL_VERIFY_SCHEME = "syntax+mx"   # validated by user: no external SMTP verify

# ── Discovery keyword / intent lists (from user spec) ───────────────────────
# HIGH-INTENT_CROSS: project/contract/freelance/agency work = closest to direct
# paid web work -> prioritized first in the rotation.
PROJECT_HIGH_INTENT_KEYWORDS = [
    "freelance web developer", "contract web developer",
    "website redesign", "build a website", "website project",
    "web developer contract", "website development project",
    "hire freelance web developer", "web project",
]

# Full-time / contract role hires (still genuine buyers, slightly lower priority).
JOB_KEYWORDS = [
    "web developer", "website developer", "frontend developer",
    "backend developer", "full stack developer",
    "react developer", "next.js developer", "web app developer",
    "website development", "web application developer",
    "wordpress developer", "shopify developer",
]
ALL_JOB_KEYWORDS = JOB_KEYWORDS + PROJECT_HIGH_INTENT_KEYWORDS

GOOGLE_INTENT_PHRASES = [
    '"looking for a web developer"',
    '"need a web developer"',
    '"web developer needed"',
    '"hire a web developer"',
    '"website developer needed"',
    '"need someone to build a website"',
    '"need a website built"',
    '"looking for someone to build a website"',
    '"web development project"',
    '"need a web app developer"',
    '"looking for a freelance web developer"',
    '"need a developer for client project"',
    '"white label web development"',
]

AGENCY_INTENT_PHRASES = [
    '"looking for freelance developers" agency',
    '"white label web development" agency',
    '"need developers for client projects"',
    '"outsource web development" agency',
]

INDUSTRY_FALLBACK_IDS = {
    "real_estate": [44],
    "hotels": [2194, 31],
    "restaurants": [32],
    "dental": [2045, 14],
    "construction": [48],
    "auto_dealer": [1292],
}

# ── Business-client discovery (SME potential web/app/automation clients) ─────
# These are NOT job posters: they are operating local businesses that are the
# *potential clients* for web/app/automation services. Scored separately via
# business_client_score (evidence-based), NOT via fake web-dev buying intent.
BUSINESS_QUERIES = [
    ("Real Estate Agency", ["real estate agency", "real estate broker", "realtor"]),
    ("Dental Clinic", ["dentist", "dental clinic", "dental care"]),
    ("Restaurant", ["restaurant", "cafe", "bistro"]),
    ("Home Improvement", ["roofing company", "hvac company", "plumber", "electrician", "landscaping", "general contractor"]),
    ("Hotel", ["hotel", "resort", "motel"]),
    ("Automotive", ["car dealer", "auto repair", "car detailing", "car dealership"]),
    ("Legal", ["law firm", "attorney", "law office"]),
    ("Fitness", ["gym", "fitness studio", "personal training"]),
    ("Beauty & Wellness", ["salon", "spa", "barber shop", "nail salon"]),
    ("Medical", ["chiropractor", "physical therapy", "optometrist", "urgent care", "dental clinic"]),
    ("Home Services", ["cleaning company", "moving company", "pest control"]),
    ("Construction", ["contractor", "construction company", "landscaping", "builder"]),
    ("Professional", ["accountant", "cpa", "architect", "interior designer", "event planner", "wedding planner", "travel agency"]),
]

# ── TARGET MARKETS (Google Business/Maps discovery) ─────────────────────────
# India is a FIRST-CLASS market for the business/maps track (not a fallback).
# Country -> cities. Used by build_business_queries + build_maps_queries; the
# maps rotation balances across ALL these countries each cycle.
COUNTRY_CITIES = {
    "US": [
        "Miami FL", "Houston TX", "Chicago IL", "Dallas TX", "Phoenix AZ",
        "Los Angeles CA", "Denver CO", "Austin TX", "San Diego CA", "Tampa FL",
        "Orlando FL", "Las Vegas NV", "Seattle WA", "Portland OR", "Atlanta GA",
        "Nashville TN", "Charlotte NC", "Columbus OH", "San Antonio TX", "New York NY",
    ],
    "UK": [
        "London UK", "Manchester UK", "Birmingham UK", "Leeds UK", "Glasgow UK",
        "Liverpool UK", "Bristol UK", "Sheffield UK", "Edinburgh UK", "Cardiff UK",
    ],
    "UAE": [
        "Dubai UAE", "Abu Dhabi UAE", "Sharjah UAE",
    ],
    "INDIA": [
        "Mumbai", "Delhi", "Bangalore", "Hyderabad", "Chennai", "Pune",
        "Kolkata", "Ahmedabad", "Jaipur", "Lucknow", "Kanpur", "Indore",
        "Surat", "Nagpur", "Bhopal", "Chandigarh", "Patna", "Gorakhpur",
        "Kochi", "Coimbatore", "Noida", "Gurgaon",
    ],
    "CANADA": [
        "Toronto ON", "Vancouver BC", "Calgary AB", "Edmonton AB", "Ottawa ON",
        "Mississauga ON", "Montreal QC", "Winnipeg MB", "Hamilton ON",
    ],
    "AUSTRALIA": [
        "Sydney NSW", "Melbourne VIC", "Brisbane QLD", "Perth WA", "Adelaide SA",
        "Gold Coast QLD", "Canberra ACT", "Hobart TAS",
    ],
}

# High-value/commercial categories get a priority-score boost (established enough
# to pay for professional development — user's India-priority list + global).
HIGH_VALUE_CATEGORIES = {
    "hotel", "resort", "restaurant", "cafe", "bistro", "dining", "pub", "bar",
    "clinic", "dental", "dentist", "hospital", "medical", "physiotherapy",
    "law firm", "attorney", "legal", "accountant", "cpa", "tax",
    "real estate", "realtor", "property", "builder", "construction", "contractor",
    "interior design", "architect", "engineering",
    "automotive", "auto repair", "car dealer", "car detailing", "garage",
    "plumber", "plumbing", "electrician", "electrical", "hvac", "roofing",
    "cleaning", "moving", "pest control", "home services",
    "salon", "spa", "barber", "nail", "beauty",
    "gym", "fitness", "yoga", "personal training",
    "photographer", "photography", "wedding", "event", "catering",
    "travel agency", "tour operator", "coaching", "institute", "training",
    "manufacturer", "wholesaler", "exporter", "trade", "workshop",
    "school", "training center", "college", "academy",
}

# Google Maps discovery categories (searched as "<term> in <city>").
MAPS_CATEGORIES = [
    "plumbers", "electricians", "hvac companies", "roofing companies",
    "general contractors", "construction companies", "landscaping companies",
    "cleaning companies", "moving companies", "pest control",
    "auto repair shops", "auto detailing", "car dealerships",
    "dentists", "dental clinics", "doctors clinics", "physiotherapy clinics",
    "law firms", "accountants", "real estate agencies", "interior designers",
    "architects", "builders",
    "restaurants", "cafes", "hotels", "resorts",
    "salons", "spas", "barber shops", "gyms", "fitness studios",
    "photographers", "wedding photographers", "event planners", "wedding planners",
    "travel agencies", "tour operators",
    "home services", "local services",
]

# Combined city list for the SERP business source (all target markets).
BUSINESS_CITIES = [c for cities in COUNTRY_CITIES.values() for c in cities]


def _country_of_city(city):
    """Resolve the target-market country for a city string ('' if unknown)."""
    for cc, cities in COUNTRY_CITIES.items():
        if city in cities:
            return cc
    low = (city or "").lower()
    if "india" in low or low in ("mumbai", "delhi", "bangalore", "bengaluru"):
        return "INDIA"
    if "uae" in low or "dubai" in low or "abu dhabi" in low or "sharjah" in low:
        return "UAE"
    if "uk" in low or "london" in low or "manchester" in low:
        return "UK"
    return ""


def build_business_queries(countries=None):
    """Industry x location queries for local-business discovery. Used by both
    the Apify (Google) and Chocodata (Bing) business sources so every engine
    hunts the SAME profile: real operating businesses across US/UK/UAE/India/
    Canada/Australia. `countries` optionally restricts to a market subset."""
    cities = BUSINESS_CITIES
    if countries:
        cities = [c for cc in countries for c in COUNTRY_CITIES.get(cc, [])]
    return [q for _, terms in BUSINESS_QUERIES
            for t in terms for c in cities
            for q in ['"{0}" "{1}"'.format(t, c)]]


def build_maps_queries(countries=None, city_count=None, categories=None):
    """Google Maps discovery queries: '<category> in <city>' across target
    markets. `countries` restricts to a market subset; `city_count` limits how
    many cities per country (used for country-balanced rotation)."""
    cats = categories or MAPS_CATEGORIES
    if countries:
        cities = [c for cc in countries for c in COUNTRY_CITIES.get(cc, [])]
    else:
        cities = BUSINESS_CITIES
    if city_count:
        per = {}
        for c in cities:
            per.setdefault(_country_of_city(c) or "XX", []).append(c)
        cities = [x for cc in sorted(per) for x in (per[cc][:city_count])]
    return [("{0} in {1}".format(t, c), c) for t in cats for c in cities]
# Known aggregators/portals & low-value domains to filter out of SERP results.
BUSINESS_NOISE_DOMAINS = {
    "yelp.com", "yellowpages.com", "tripadvisor.com", "zillow.com", "realtor.com",
    "redfin.com", "angieslist.com", "angi.com", "houzz.com", "bbb.org",
    "mapquest.com", "superpages.com", "whitepages.com", "thryv.com", "bizapedia.com",
    "clutch.co", "g2.com", "trustpilot.com", "glassdoor.com", "linkedin.com",
    "facebook.com", "instagram.com", "google.com", "youtube.com", "wikipedia.org",
    "yelp.ca", "kudzu.com", "dexknows.com", "hotfrog.com", "cylex.us.com",
}
BUSINESS_NOISE_PATHS = (
    "/find-a-", "/find-", "/search", "/directory", "/reviews", "/agent",
    "/brokers", "/locations", "/category/", "maps.google.com", "google.com/maps",
)
# Minimum evidence-based score for a business lead to be accepted.
BUSINESS_CLIENT_QUALIFY_SCORE = 50

# ── Business-discovery supply (local SME potential clients) ────────────────
# These are the real local businesses that could become web/app/automation
# clients. Give this priority source a dedicated, larger quota per cycle than
# the tiny shared per-source cap, plus more SERP results per query so enough
# own-domain results survive the aggregator/portal noise filter.
BUSINESS_DISCOVERY_MAX_PER_CYCLE = 12
BUSINESS_DISCOVERY_RESULTS = 8

# Google Maps (primary channel) quotas. Maps listings with an EMPTY website
# field are the highest-quality new-website prospects, so this primary channel
# gets a dedicated budget share per cycle (on top of the SERP business source,
# both share the same daily Apify cap).
MAPS_DISCOVERY_MAX_PER_CYCLE = 12
MAPS_DISCOVERY_RESULTS = 6   # localResults items returned per Maps query
MAPS_COUNTRIES_PER_CYCLE = 3    # countries rotated each cycle (country-balanced)
MAPS_CITIES_PER_COUNTRY = 2     # cities sampled per country per query round

# ── Google Maps discovery MODE ──────────────────────────────────────────────
#   "apify"  : paid compass/crawler-google-places actor. Cost comes OUT of the
#              $0.15 daily Apify cap and needs the Maps reserve; when the cap is
#              exhausted Maps is skipped ("MAPS SKIPPED: reserved budget...").
#   "public" : KEYLESS headless-Edge browser (maps_public.py). Cost is EXACTLY
#              $0, uses NO Apify budget and NO reserve, so Google Maps is ALWAYS
#              attempted every cycle even after the Apify budget fully exhausts.
MAPS_DISCOVERY_MODE = "public"

# Public-mode knobs (only meaningful when MAPS_DISCOVERY_MODE == "public").
MAPS_PUBLIC_EDGE_PATH = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
MAPS_PUBLIC_PROFILE_DIR = os.path.join(DATA_DIR, "maps_public_edge_profile")
MAPS_PUBLIC_VIRTUAL_TIME_MS = 9000   # JS render time budget for the Headless dump
MAPS_PUBLIC_TIMEOUT_S = 75           # hard wall-clock cap per query run
MAPS_PUBLIC_RETRIES = 1              # fresh-session retries per query (429/empty)
MAPS_PUBLIC_RETRY_DELAY_S = 2        # backoff between retries

# Place-page website verification (public mode): a search card with no visible
# Website button does NOT mean the business has no website — Google renders the
# button with changing classes and sometimes only on the listing's PLACE PAGE
# (regression: LIFT Gym Wangara). When enabled, every site-less public lead gets
# ONE extra headless-Edge dump of its place page before NO_WEBSITE is declared.
# $0, no Apify. Businesses whose panel declares a website go through normal
# web-presence classification (REAL dropped / BROKEN kept with URL) instead.
MAPS_PUBLIC_VERIFY_WEBSITE = True

# Free email-discovery fallback (email_waterfall): when the normal curl-based
# website scrape finds nothing (WAF blocks, or a no-website business that only
# publishes a contact email on a listing page), we do a FREE DuckDuckGo Lite
# search for the business name + city and fetch the top listing pages with the
# SAME headless-Edge dump technique maps_public uses (real Chromium bypasses
# Cloudflare/anti-bot that plain curl cannot). Only discovered emails that pass
# MX verification are ever returned - never guessed/invented.
EMAIL_SERP_FALLBACK = True          # master switch for the free SERP hunt
EMAIL_SERP_MAX_RESULTS = 6          # max SERP results to consider
EMAIL_SERP_MAX_FETCH = 2            # max listing pages fetched per lead (Edge)
EMAIL_EDGE_PROFILE_DIR = os.path.join(DATA_DIR, "email_edge_profile")
EMAIL_EDGE_BUDGET_MS = 9000         # JS render budget for Edge dump (same as maps)
EMAIL_EDGE_TIMEOUT_S = 75           # hard wall-clock cap per Edge fetch

# LinkedIn company-search actor (short mode) does not reliably return
# website/domain and returned 0 usable leads in the controlled test. Disabled
# until it is fixed and independently proven — we won't spend production Apify
# budget on it in the meantime.
ENABLE_LINKEDIN_COMPANY = False

# ── Discovery source switches (client profile: local business + NO website) ──
# PRIMARY = real operating businesses WITHOUT a proper website (US/UK/UAE).
#   google_business  : Google SERP businesses (Apify API)        - PRIMARY
#   chocodata_bing   : independent Bing engine, local-business    - SECONDARY
# The other sources hunt "companies hiring a web developer" (Leadsource job
# boards, Reddit hiring posts, explicit "looking for a developer" SERPs).
# Those are NOT the client (user: STOP treating "looking for a developer" as
# primary; do-not-target = freelancers/devs/agencies/job boards/freelance
# marketplaces). They are DISABLED by default and only re-enabled deliberately.
ENABLE_GOOGLE_BUSINESS = True
ENABLE_CHOCODATA_BING = True
ENABLE_CHOCODATA_JOB = False
ENABLE_CHOCODATA_REDDIT = False
ENABLE_CHOCODATA_INDEED = False
ENABLE_GOOGLE_INTENT = False

# Conservative bigness/non-SME filter. Reject clearly non-local-SME
# organizations (global enterprises, major nonprofits, universities, gov.)
# so the client list stays genuine local businesses.
BUSINESS_BIGNESS_DOMAINS = {
    "ymca.org", "ymcahouston.org", "redcross.org", "goodwill.org",
    "salvationarmy.org", "unitedway.org", "usa.gov", "law.com", "lw.com",
}
# Well-known global / mega enterprises and institutions that are clearly not
# realistic local-business clients (conservative: only famous, unambiguous orgs).
BUSINESS_BIGNESS_NAMES = [
    "latham & watkins", "deloitte", "pwc", "kpmg", "ey ", "accenture",
    "mckinsey", "bain", "boston consulting", "jpmorgan", "goldman sachs",
    "morgan stanley", "bank of america", "wells fargo", "coca-cola", "microsoft",
    "google llc", "amazon", "apple inc", "meta ", "facebook inc", "nike",
    "wal-mart", "walmart", "mcdonald's", "starbucks", "hilton", "marriott",
    "four seasons", "hyatt",
]
BUSINESS_BIGNESS_PATTERNS = [
    r"university", r"college", r"\.edu\b", r"school district",
    r"board of education", r"\.gov\b", r"government", r"county of",
    r"state of", r"united states", r"white house", r"federal",
    r"ymca", r"red cross", r"goodwill", r"salvation army", r"united way",
    r"chamber of commerce", r"hospital\b",
]

# A local business only qualifies as a client when it has a REAL, observable
# web-presence gap (weak/no site) — someone with a complete modern site is not
# a lead. web_presence.classify() assigns web_status (NO_WEBSITE / BROKEN /
# PLACEHOLDER / REAL / UNKNOWN); REAL_WEBSITE and UNKNOWN are hard-rejected.
# Thresholds are kept at default for the PoC run; tune after measuring
# false positives / false negatives.
WEB_GAP_QUALIFY_SCORE = 40


DECISION_TITLES = [
    "founder", "co-founder", "owner", "ceo", "cto", "director", "president",
    "managing director", "principal",
]

# ── Quality / intent ordering ───────────────────────────────────────────────
QUALITY_ORDER = [
    "job_posting",
    "explicit_search",
    "needs_website",
    "agency_need",
    "startup_help",
    "business_fallback",
]

DAILY_TARGET_VERIFIED_LEADS = 50

# Max qualified leads pulled through email extraction each cycle. Raised so that
# priority local-business (google_business) leads aren't cut by the old tiny
# slice before their email is even attempted. Still bounded: per-source final
# cap (MAX_FINAL_PER_SOURCE), the daily target, and the Apify budget guard all
# remain in force — this only widens the email-extraction window, not the output.
MAX_FINAL_PROCESS_PER_CYCLE = 25

# ── India handling (per user: India = FIRST-CLASS target market) ──────────────
# The Google Business/Maps track (business_client) treats India as a tier-1
# market — NO exclusion (qualification.is_business_client/global). This regex is
# used ONLY for the legacy re-enabled hire-a-developer sources (job boards /
# intent SERPs), which stay foreign-only. business_client leads are never
# rejected on INDIA_TOKEN_RE.
INDIA_TOKEN_RE = re.compile(r"\bindia\b|\bindian\b|\bdelhi\b|\bmumbai\b|\bbangalore\b|\bbengaluru\b|\bhyderabad\b|\bchennai\b|\bkolkata\b|\bpune\b|\bjaipur\b|\bgurgaon\b|\bgurugram\b|\bnoida\b|\bchandigarh\b|\bgoa\b|\bkochi\b|\bcoimbatore\b|\bpunjab\b|\bgujarat\b|\brajasthan\b|\bkarnataka\b|\btamil nadu\b|\bachra\b|\bindore\b|\bnagpur\b|\bvisakhapatnam\b|\bsurat\b|\bkanpur\b|\blocknow\b|\bvadodara\b|\bthane\b|\bnashik\b|\bmeerut\b|\bajmer\b|\bkerala\b|\bbihar\b|\bup\b", re.I)

# Rotated foreign discovery locations (broad, non-India, English-dominant high-buying regions)
FOREIGN_LOCATIONS = [
    "United States", "United Kingdom", "Canada", "Australia",
    "Germany", "Netherlands", "United Arab Emirates", "Singapore",
    "New Zealand", "Ireland", "Switzerland", "Norway", "Sweden",
    "Denmark", "Finland", "Belgium", "Austria", "France", "Spain",
]

# Phone / WhatsApp heuristics (loose match; digit-count validation done in code)
PHONE_RE = re.compile(r"\+?\d{1,4}[\s\-.]?(?:\(?\d{1,4}\)?[\s\-.]?)?\d{1,4}[\s\-.]?\d{1,4}[\s\-.]?\d{1,4}")
US_CANADA_RE = re.compile(r"^(\+1[\s\-.]?)?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4}$")
UK_RE = re.compile(r"^(\+44\s?7\d{3}|\+44\s?2\d{2}|07\d{3}|02\d{2})[\s\-.]?\d{3,4}[\s\-.]?\d{3,4}$")
AU_RE = re.compile(r"^(\+61\s?4|04)[\s\-.]?\d{2}[\s\-.]?\d{3}[\s\-.]?\d{3}$")
DE_RE = re.compile(r"^\+49\s?\d{2,4}[\s\-.]?\d{3,8}$")

# Generic junk domains / tokens to drop from scraped emails
SKIP_DOMAINS = {
    "example.com", "example.org", "example.net", "sentry.io", "w3.org",
    "schema.org", "adobe.org", "googleapis.com", "gstatic.com", "jquery.com",
    "cloudflare.com", "youtube.com", "facebook.com", "twitter.com", "x.com",
    "instagram.com", "pinterest.com", "spotify.com", "apple.com",
    "microsoft.com", "github.io", "githubassets.com", "wixpress.com",
    "linkedin.com", "hubspot.com", "hsforms.com", "akamai.com",
    "buildwith.com", "placeholder.com", "domain.com", "yourdomain.com",
    "email.com", "mail.com", "gmail.com",
}

# Listing/reputation aggregators whose OWN email addresses (e.g. a directory's
# "profiles@<platform>.com" support box) are NEVER the business's inbox, even
# when the platform's page also mentions the business name. The SERP/edge
# hunt must treat these as hard negatives: an address on these domains is only
# usable if it is ALSO the lead's own domain (checked upstream at the call
# site by requiring domain match). Blocks misattribution like the John Reed /
# profiles@birdeye.com false positive (ranked directory page beat the real
# site in the DuckDuckGo results).
DIRECTORY_DOMAINS = {
    "birdeye.com", "yelp.com", "yelp.ca", "yellowpages.com", "yell.com",
    "trustpilot.com", "hotfrog.com", "cylex.us", "cylex.com", "dexknows.com",
    "mysite.com", "dnb.com", "dnb.ca", "hoofman", "manta.com", "infobel.com",
    "tupalo.com", "fonecta.fi", "twoo.com", "brownbook.net", "chamberofcommerce.com",
    "ezlocal.com", "superpages.com", "merchantcircle.com", "citysearch.com",
    "insiderpages.com", "bizjournals.com", "hotfrog.co", "find-us-here.com",
}
EMAIL_BAD_TOKENS = ["noreply", "no-reply", "example", "test@", "your@",
                    "name@", "email@", "sentry", "wixpress"]


def env_value(key, default=""):
    env = {}
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                if "=" in line and not line.strip().startswith("#"):
                    k, v = line.strip().split("=", 1)
                    env[k] = v
    return env.get(key, default)


def get_api_keys():
    return {
        "APIFY": env_value("APIFY_API_TOKEN"),
        "CHOCODATA": env_value("CHOCODATA_API_KEY"),
        "PROSPEO": env_value("PROSPEO_API_KEY"),
        "APOLLO": env_value("APOLLO_API_KEY"),
    }


EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

MX_HINT_DOMAINS = {"gmail.com", "outlook.com", "yahoo.com", "hotmail.com",
                   "icloud.com", "aol.com", "protonmail.com", "zoho.com"}

# ── AI cold-email automation layer ─────────────────────────────────────
# Gemini API key + model are read from .env (never hardcode in source).
# Model is configurable via GEMINI_MODEL in .env; keep it a currently
# supported generation model (verified working: gemini-3.5-flash).
GEMINI_MODEL = env_value("GEMINI_MODEL", "gemini-3.5-flash")
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_TIMEOUT_SECONDS = 30
# Fallback models if the primary is temporarily unavailable (e.g. 503 spikes).
GEMINI_FALLBACK_MODELS = ["gemini-3.1-flash-lite", "gemini-flash-lite-latest"]
GEMINI_MAX_TRANSIENT_RETRIES = 2   # per-model retry on 503/429
GEMINI_TRANSIENT_SLEEP = 2         # seconds between transient retries
# Hard cap on total Gemini HTTP calls for one lead's generation. Free tier is
# 20 req/min (output_tokens + request counts), so we must NOT spin through all
# models x retries (could blow the whole quota on a single lead).
MAX_GEMINI_TOTAL_CALLS = 4

# Master switch: when False (DEFAULT), a lead gets Gemini generation +
# quality check + a Telegram DRAFT preview ONLY — no SMTP send.
# Set TRUE only after you review draft quality and approve automated sending.
AUTO_EMAIL_SEND = False

# Conservative send budgets so we never blast all leads at once.
MAX_AUTO_EMAILS_PER_CYCLE = 2
MAX_AUTO_EMAILS_PER_DAY = 10

# Cost / effort control: max 1 normal generation + 1 regeneration if check fails.
MAX_GEMINI_ATTEMPTS = 2
# Small pacing delay between consecutive SMTP sends (seconds).
EMAIL_DELAY_SECONDS = 5

# Outbound Surya identity (must match the email system signature config).
SEND_FROM_NAME = env_value("FROM_NAME", "Surya")
SEND_LINKEDIN = "https://www.linkedin.com/in/surya-kant-pandey-2a54b0298"
SEND_GITHUB = "https://github.com/suryaexists2"


def get_gemini_api_key():
    return env_value("GEMINI_API_KEY").strip()


def get_gemini_models():
    """Ordered list of models to try: primary then configured fallbacks."""
    fallback = [m for m in GEMINI_FALLBACK_MODELS if m != GEMINI_MODEL]
    return [GEMINI_MODEL] + fallback
