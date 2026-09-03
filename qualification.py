"""
Buying-intent detection + scoring.

Output per lead:
  lead_type     : one of config.QUALITY_ORDER
  intent_reason : matched phrase/signal (human readable)
  intent_score  : int (>=60 HOT, 30-59 WARM, <30 reject)
Rejects developers/agencies SELLING services (unless they need subcontractors),
plus junk/directories and obvious non-buyers.
"""
import re

import scraper  # reuse OFFERING_PHRASES / NEED_PHRASES
import config

OFFER_RE = re.compile(r"|".join(re.escape(p) for p in scraper.OFFERING_PHRASES), re.I)

# Explicit hiring fits the user's intent phrases
EXPLICIT_INTENT = [
    "looking for a web developer", "need a web developer", "web developer needed",
    "hire a web developer", "website developer needed", "need someone to build a website",
    "need a website built", "looking for someone to build a website",
    "web development project", "need a web app developer",
    "looking for a freelance web developer", "need a developer for client project",
    "white label web development",
]

NEEDS_WEBSITE_SIGNALS = [re.compile(p) for p in [
    r"need\s+(a|our|my|new)\s+websites?", r"website\s+(needed|required|project|design|redesign|rebuild)",
    r"redesign\s+(our|my|the)\s+website", r"build\s+(our|my|the|a)\s+website",
    r"new website", r"website for (our|my)", r"launch a website",
    r"web ?app\s+(for|development|needed|project)", r"need.*(website|web ?app|landing page)",
]]

AGENCY_NEED_SIGNALS = [re.compile(p) for p in [
    r"white ?label", r"agency.*(developer|freelance|subcontract)",
    r"subcontract", r"client project", r"outsource web", r"overflow work for .*developers?",
    r"looking for .*developers?", r"hire.*developers? for (our|client)",
]]

STARTUP_HELP_SIGNALS = [re.compile(p) for p in [
    r"startup.*(need|looking|hiring|developer)", r"co-founder.*developer",
    r"technical co-founder", r"mvp", r"build our product",
]]

SERVICE_SELLER_SIGNALS = [re.compile(p) for p in [
    r"\bwe (build|create|develop|design) (websites|web apps)\b",
    r"\bI (build|create|develop|design) (websites|web apps)\b",
    r"web design (agency|company|studio|services)", r"our (web|website) services",
    r"digital agency", r"\bfreelance web developer\b (offering|available|for hire|looking for client)",
    r"check out our portfolio", r"starting at \$",
    r"\bhiring\s+clients\b", r"\bservices include\b",
]]

JOB_SIGNALS = [re.compile(p) for p in [
    r"\bjob\b", r"\bhiring\b", r"\bposition\b", r"\brole\b", r"\bvacancy\b",
    r"\bfull[- ]time\b", r"\bsalary\b", r"\bapply\b", r"\bresponsibilities\b",
    r"\brequirements\b", r"\border/apply\b",
]]

JUNK_SIGNALS = [re.compile(p) for p in [
    r"buy (website|domain|audience|traffic|followers)", r"seo (reseller|panel|backlinks)",
    r"guest post", r"link building", r"instagram (followers|likes|views)",
    r"buy (followers|likes|subscribers)",
]]


def _text(lead):
    parts = [lead.get("post_text", ""), lead.get("title", ""),
             lead.get("company", ""), lead.get("headline", "")]
    return " ".join(p for p in parts if p)


def is_service_seller(text):
    """True if the text is a dev/agency SELLING services (not a buyer)."""
    if not text:
        return False
    for pat in SERVICE_SELLER_SIGNALS:
        if pat.search(text):
            return True
    return False


def is_junk(text):
    if not text:
        return True
    for pat in JUNK_SIGNALS:
        if pat.search(text.lower()):
            return True
    return False


def classify(lead):
    """
    Return (lead_type, intent_reason, intent_score).
    Fills 'inferred' classification for qualification; does NOT mutate lead.
    """
    text = _text(lead).lower()
    raw = _text(lead)

    if is_junk(text):
        return "business_fallback", "junk signal", 0

    # Highest intent: explicit "I need a web developer"/job posting
    for phrase in EXPLICIT_INTENT:
        if phrase in text:
            if OFFER_RE.search(raw):
                # Developer replying/offering within an explicit-need thread is still
                # a genuine buyer (the post itself) -> keep as explicit_search.
                pass
            return "explicit_search", f"explicit intent: \"{phrase}\"", 85

    # Active job/project posting (companies hiring web devs = buyers)
    job_hits = sum(1 for p in JOB_SIGNALS if p.search(text))
    has_web = any(t in text for t in ["web", "frontend", "website", "wordpress",
                                      "shopify", "react", "full stack", "full-stack"])
    if has_web and job_hits >= 2:
        return "job_posting", "active web-dev role/project posting", 80

    # Agency needing subcontractors / white-label
    for pat in AGENCY_NEED_SIGNALS:
        if pat.search(text):
            return "agency_need", f"agency needs dev support: {pat}", 72

    # Needs a website / redesign / web app
    for pat in NEEDS_WEBSITE_SIGNALS:
        if pat.search(text):
            return "needs_website", f"needs website/web-app: {pat}", 65

    # Startup needing dev help / co-founder
    for pat in STARTUP_HELP_SIGNALS:
        if pat.search(text):
            return "startup_help", f"startup needs dev help: {pat}", 60

    # General buying language (fallback)
    need_hits = sum(1 for p in scraper.NEED_PHRASES if p.lower() in text)
    if need_hits >= 2:
        return "business_fallback", f"general need signals ({need_hits})", 40

    # Seller of dev services (not a buyer) -> reject
    if OFFER_RE.search(raw):
        return "business_fallback", "selling dev services (not buyer)", 5

    return "business_fallback", "no clear buying intent", 15


def qualify(lead):
    """Mutate lead with lead_type/intent_reason/intent_score. Returns lead."""
    lead["_matched_lead_type"], reason, score = classify(lead)
    lead["intent_reason"] = reason
    lead["intent_score"] = score
    actual_type = lead.get("lead_type") or lead["_matched_lead_type"]
    lead["lead_type"] = actual_type
    return lead


def _is_foreign(lead):
    """Global-market rule. The Google Business/Maps track (business_client)
    treats India as a FIRST-CLASS target market (user: India must be a tier-1
    market, never excluded). Only the legacy re-enabled hire-a-developer
    sources (job boards / intent SERPs) stay foreign-only."""
    if lead.get("lead_type") == "business_client":
        return True
    loc = " ".join([str(lead.get("location", "")), str(lead.get("company", "")),
                    str(lead.get("post_text", ""))])
    return not config.INDIA_TOKEN_RE.search(loc)


def is_business_client(lead):
    """True if this is a business-client lead that qualifies as a potential
    web/app/automation client.

    Hard safety rules based on web_status from the 5-state classifier:
      REAL_WEBSITE  -> always reject (business already has a usable website)
      UNKNOWN       -> don't qualify as no-website lead (ambiguous)
      NO_WEBSITE    -> qualifies on status alone (no web_gap_signals required)
      BROKEN/PLACEHOLDER -> normal qualification with score + supporting signals
    Additionally, dev/agency/SEO merchants found in the business track
    (a "web design agency" is a seller, not a buyer) are hard-rejected.
    """
    if lead.get("lead_type") != "business_client":
        return False

    web_status = lead.get("web_status")

    # REAL_WEBSITE: hard reject regardless of score
    if web_status == "REAL_WEBSITE":
        return False

    # UNKNOWN: ambiguous technical result - don't qualify as a no-website lead
    if web_status == "UNKNOWN":
        return False

    # Do-NOT-target guard: a web-dev/agency/SEO business is a SELLER of our
    # service, never the no-website business we want to pitch to.
    if is_web_merchant(lead):
        return False

    # NO_WEBSITE / BROKEN_WEBSITE / PLACEHOLDER_WEBSITE: proceed with score
    bscore = lead.get("business_client_score") or 0
    if bscore < getattr(config, "BUSINESS_CLIENT_QUALIFY_SCORE", 50):
        return False

    # NO_WEBSITE status itself is a strong enough signal; no mandatory
    # web_gap_signals required. Others still need supporting evidence.
    if web_status != "NO_WEBSITE":
        signals = lead.get("web_gap_signals") or []
        if not signals:
            return False

    return True


WEB_MERCHANT_RE = re.compile(r"\b(web design (agency|company|studio|firm|co\.?|services|solutions)|"
                              r"web development (agency|company|studio)|"
                              r"website design (agency|company|studio)|website development (agency|company)|"
                              r"digital (marketing|media) agency|seo (agency|company|firm|services)|"
                              r"freelance web developer|freelancer|freelance marketplace|"
                              r"website building (service|company)|web developer\b|web design services|"
                              r"hire (a )?web developer)\b")


def is_web_merchant(lead):
    """True when the business-track lead is actually a web-dev/SEO/agency SELLER
    (do-not-target), not the no-website business we pitch to.
    Checks both the business name (WEB_MERCHANT_RE) and dev-service selling
    phrases in the description (a "we design websites" company is a seller)."""
    frag = " ".join(str(lead.get(k, "")) for k in (
        "company", "name", "title", "description", "post_text")).lower()
    if not frag:
        return False
    if WEB_MERCHANT_RE.search(frag):
        return True
    return is_service_seller(frag)


def _is_bigness_org(lead):
    """Conservative non-SME filter. True when the business is clearly NOT a
    realistic local/SME client: global enterprise, major nonprofit, university,
    government body, etc. Evidence-based (real name/domain), never intent."""
    domain = (lead.get("domain") or lead.get("company_website") or "").lower()
    frag = " ".join(str(lead.get(k, "")) for k in (
        "company", "name", "title", "post_text", "description")).lower()
    for d in getattr(config, "BUSINESS_BIGNESS_DOMAINS", set()):
        if d in domain:
            return True
    for nm in getattr(config, "BUSINESS_BIGNESS_NAMES", []):
        if nm in frag:
            return True
    for pat in getattr(config, "BUSINESS_BIGNESS_PATTERNS", []):
        try:
            if re.search(pat, frag):
                return True
        except Exception:
            pass
    return False


def is_qualified(lead):
    """A lead counts if it is a genuine buyer (intent_score >= 50) OR a genuine
    business-client potential (business_client_score >= threshold, evidence-based).
    India-based leads are always rejected (foreign-only). Business-client leads
    that are clearly large/non-SME organizations or have REAL_WEBSITE/UNKNOWN
    web_status are rejected too."""
    if not _is_foreign(lead):
        return False
    if is_business_client(lead):
        if _is_bigness_org(lead):
            return False
        return True
    # Business_client leads with REAL_WEBSITE or UNKNOWN are already
    # rejected by is_business_client(); reject any remaining UNKNOWN
    # business_client leads explicitly.
    if lead.get("lead_type") == "business_client" and lead.get("web_status") == "UNKNOWN":
        return False
    if (lead.get("intent_score") or 0) < 50:
        return False
    return True


def reject_reason(lead):
    """Human-readable reason a lead fails qualification. Every reject has one."""
    if not _is_foreign(lead):
        return "INDIA-based (foreign-only rule)"
    if is_business_client(lead):
        if _is_bigness_org(lead):
            return "Non-SME/large org (bigness filter)"
        return "unknown rejection cause (business_client)"
    if lead.get("lead_type") == "business_client":
        ws = lead.get("web_status")
        if ws == "REAL_WEBSITE":
            return "Real usable website already live (hard reject)"
        if ws == "UNKNOWN":
            return "Website status ambiguous (UNKNOWN) - not a trustable no-website lead"
        if is_web_merchant(lead):
            return "Web-dev/agency/SEO merchant (do-not-target seller)"
    if (lead.get("intent_score") or 0) < 50:
        return lead.get("intent_reason") or f"intent_score<50 ({lead.get('intent_score')})"
    return "unknown rejection cause"
