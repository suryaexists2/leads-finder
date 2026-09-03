"""CSV storage + all-time JSON dedup history."""
import csv
import json
import os
import re
from datetime import datetime

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
LEADS_CSV = os.path.join(DATA_DIR, "leads.csv")
NO_EMAIL_CSV = os.path.join(DATA_DIR, "leads_no_email.csv")
SENT_CSV = os.path.join(DATA_DIR, "sent.csv")
NOTIFIED_CSV = os.path.join(DATA_DIR, "notified.csv")
HISTORY_JSON = os.path.join(DATA_DIR, "lead_history.json")

FIELDNAMES = [
    "name", "post_text", "post_url", "email",
    "profile_url", "source_keyword", "source",
    "role", "email_source", "website", "source_url",
    "lead_type", "industry", "intent_reason", "intent_score", "location",
    "company", "company_slug", "company_website", "email_verified",
    "phone", "whatsapp",
    "status", "added_at", "sent_at",
    "email_generation_status", "email_generation_attempts",
    "generated_subject", "generated_body", "email_model",
    "email_send_status", "email_sent_at", "email_send_error",
]


def _ensure_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def _ensure_file(path, fieldnames):
    _ensure_dir()
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=fieldnames).writeheader()


def init():
    _ensure_file(LEADS_CSV, FIELDNAMES)
    _ensure_file(NO_EMAIL_CSV, FIELDNAMES)
    _ensure_file(SENT_CSV, ["email", "name", "status", "sent_at"])
    _ensure_file(NOTIFIED_CSV, ["email", "name", "status", "notified_at"])
    if not os.path.exists(HISTORY_JSON):
        _save_history({"slugs": [], "names": [], "emails": [], "companies": [],
                       "domains": [], "source_urls": [], "count": 0})
    _migrate_headers([LEADS_CSV, NO_EMAIL_CSV])


def _migrate_headers(paths):
    """Rewrite existing CSVs to the full FIELDNAMES header if they differ."""
    for path in paths:
        _ensure_file(path, FIELDNAMES)
        try:
            with open(path, "r", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
        except Exception:
            continue
        if not rows:
            # empty but maybe has old header only
            pass
        if os.path.getsize(path) == 0:
            continue
        # write back full header (rows keep their dicts; missing cols -> '')
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDNAMES)
            w.writeheader()
            for r in rows:
                w.writerow({fn: r.get(fn, "") for fn in FIELDNAMES})


def _norm_name(name):
    return " ".join(name.lower().strip().split())


def _slug_from_url(url):
    """Extract the meaningful slug from a LinkedIn person (/in/) or company
    (/company/) URL. Returns '' when no recognizable LinkedIn slug exists."""
    if not url:
        return ""
    u = url.strip().lower().rstrip("/")
    for marker in ("/in/", "/company/"):
        if marker in u:
            tail = u.split(marker)[-1].strip("/")
            if tail:
                return tail
    return ""


def _same_person(url1, url2):
    if not url1 or not url2:
        return False
    s1 = _slug_from_url(url1)
    s2 = _slug_from_url(url2)
    # Never treat two distinct company pages (or an empty slug) as a match.
    if not s1 or not s2 or ("/company/" in url1 and "/company/" in url2 and s1 != s2):
        return False
    if s1 == s2:
        return True
    parts1 = set(s1.replace("-", " ").split())
    parts2 = set(s2.replace("-", " ").split())
    common = parts1 & parts2
    if len(common) >= 2:
        return True
    if s1.startswith(s2) or s2.startswith(s1):
        return True
    return False


# ─── All-time history (JSON) ───

def _load_history():
    if not os.path.exists(HISTORY_JSON):
        return {"slugs": [], "names": [], "emails": [], "companies": [],
                "domains": [], "source_urls": [], "count": 0}
    try:
        with open(HISTORY_JSON, "r", encoding="utf-8") as f:
            h = json.load(f)
            h.setdefault("companies", [])
            h.setdefault("domains", [])
            h.setdefault("source_urls", [])
            return h
    except Exception:
        return {"slugs": [], "names": [], "emails": [], "companies": [],
                "domains": [], "source_urls": [], "count": 0}


def _save_history(data):
    _ensure_dir()
    with open(HISTORY_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def is_seen_before(profile_url=None, name=None, email=None, company=None,
                   company_slug=None, website=None, source_url=None):
    """Check all-time history — returns True if lead was ever seen."""
    h = _load_history()
    seen_slugs = set(h.get("slugs", []))
    seen_names = set(h.get("names", []))
    seen_emails = set(h.get("emails", []))
    seen_companies = {c.strip().lower() for c in h.get("companies", []) if c}
    seen_domains = {d.strip().lower() for d in h.get("domains", []) if d}
    seen_sources = {s.strip().lower() for s in h.get("source_urls", []) if s}

    slug = _slug_from_url(profile_url)
    nname = _norm_name(name) if name else ""
    eemail = email.strip().lower() if email else ""

    if slug and slug in seen_slugs:
        return True
    if nname and nname in seen_names:
        return True
    if eemail and eemail in seen_emails and eemail:
        return True
    if company and company.strip().lower() in seen_companies:
        return True
    if company_slug and company_slug.strip().lower() in seen_companies:
        return True
    if website:
        dom = website.strip().lower().rstrip("/")
        if dom in seen_domains:
            return True
        no_w = dom.replace("https://", "").replace("http://", "").replace("www.", "")
        if no_w in seen_domains:
            return True
    if source_url and source_url.strip().lower() in seen_sources:
        return True

    for existing_name in seen_names:
        if nname and existing_name and len(nname) > 3 and len(existing_name) > 3:
            n1 = set(existing_name.split())
            n2 = set(nname.split())
            common = n1 & n2
            if len(common) >= 2:
                return True

    if slug:
        for existing_slug in seen_slugs:
            if _same_person(f"https://linkedin.com/in/{slug}", f"https://linkedin.com/in/{existing_slug}"):
                return True

    return False


def mark_seen(profile_url=None, name=None, email=None, company=None,
              company_slug=None, website=None, source_url=None):
    """Add to all-time history."""
    h = _load_history()
    slug = _slug_from_url(profile_url)
    nname = _norm_name(name) if name else ""
    eemail = email.strip().lower() if email else ""

    if slug and slug not in h["slugs"]:
        h["slugs"].append(slug)
    if nname and nname not in h["names"]:
        h["names"].append(nname)
    if eemail and eemail not in h["emails"]:
        h["emails"].append(eemail)

    for cand in (company, company_slug):
        if cand:
            c = cand.strip().lower()
            if c and c not in h["companies"]:
                h["companies"].append(c)
    if website:
        d = website.strip().lower().replace("https://", "").replace("http://", "").replace("www.", "").rstrip("/")
        if d and d not in h["domains"]:
            h["domains"].append(d)
    if source_url and source_url.strip().lower() not in h["source_urls"]:
        h["source_urls"].append(source_url.strip().lower())

    h["count"] = len(h["slugs"])
    _save_history(h)


def get_history_count():
    h = _load_history()
    return h.get("count", 0)


# ─── CSV dedup (within-session) ───

def _build_dedup_index(csv_path):
    _ensure_file(csv_path, FIELDNAMES)
    urls = set()
    names = set()
    try:
        with open(csv_path, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                u = (row.get("profile_url") or "").strip().lower()
                if u:
                    urls.add(u)
                n = _norm_name(row.get("name", ""))
                if n:
                    names.add(n)
    except Exception:
        pass
    return urls, names


def _is_dup(name, url, seen_urls, seen_names):
    n = _norm_name(name)
    u = (url or "").strip().lower()
    if n and n in seen_names:
        return True
    if u:
        for su in seen_urls:
            if _same_person(u, su):
                return True
    return False


def add_leads(leads_with_email, leads_without_email=None):
    """Add leads to CSVs with all-time dedup. Returns (saved_email, saved_no_email, rejected)."""
    all_urls, all_names = _build_dedup_index(LEADS_CSV)
    no_urls, no_names = _build_dedup_index(NO_EMAIL_CSV)
    all_urls |= no_urls
    all_names |= no_names

    saved_email = 0
    saved_no_email = 0
    rejected = 0
    _ensure_file(LEADS_CSV, FIELDNAMES)
    _ensure_file(NO_EMAIL_CSV, FIELDNAMES)

    with open(LEADS_CSV, "a", newline="", encoding="utf-8") as f1, \
         open(NO_EMAIL_CSV, "a", newline="", encoding="utf-8") as f2:
        w1 = csv.DictWriter(f1, fieldnames=FIELDNAMES)
        w2 = csv.DictWriter(f2, fieldnames=FIELDNAMES)
        for lead in leads_with_email:
            name = lead.get("name", "")
            url = lead.get("profile_url", "")
            email = lead.get("email", "")
            company = lead.get("company", "") or lead.get("name", "")
            company_slug = lead.get("company_slug", "")
            website = lead.get("company_website", "") or lead.get("website", "")
            source_url = lead.get("source_url", "") or lead.get("post_url", "") or lead.get("profile_url", "")
            if _is_dup(name, url, all_urls, all_names):
                rejected += 1
                continue
            if is_seen_before(url, name, email, company=company,
                              company_slug=company_slug, website=website,
                              source_url=source_url):
                rejected += 1
                continue
            row = {fn: lead.get(fn, "") for fn in FIELDNAMES}
            row["added_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            row["status"] = "new"
            w1.writerow(row)
            n = _norm_name(name)
            u = url.strip().lower()
            if u:
                all_urls.add(u)
                mark_seen(profile_url=url)
            if n:
                all_names.add(n)
                mark_seen(name=name)
            if email:
                mark_seen(email=email)
            if company or company_slug:
                mark_seen(company=company, company_slug=company_slug)
            if website:
                mark_seen(website=website)
            if source_url:
                mark_seen(source_url=source_url)
            saved_email += 1

        if leads_without_email:
            for lead in leads_without_email:
                name = lead.get("name", "")
                url = lead.get("profile_url", "")
                company = lead.get("company", "") or lead.get("name", "")
                company_slug = lead.get("company_slug", "")
                website = lead.get("company_website", "") or lead.get("website", "")
                source_url = lead.get("source_url", "") or lead.get("post_url", "") or lead.get("profile_url", "")
                if _is_dup(name, url, all_urls, all_names):
                    rejected += 1
                    continue
                if is_seen_before(url, name, company=company,
                                  company_slug=company_slug, website=website,
                                  source_url=source_url):
                    rejected += 1
                    continue
                row = {fn: lead.get(fn, "") for fn in FIELDNAMES}
                row["added_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                row["status"] = "no_email"
                w2.writerow(row)
                n = _norm_name(name)
                u = url.strip().lower()
                if u:
                    all_urls.add(u)
                    mark_seen(profile_url=url)
                if n:
                    all_names.add(n)
                    mark_seen(name=name)
                if company or company_slug:
                    mark_seen(company=company, company_slug=company_slug)
                if website:
                    mark_seen(website=website)
                if source_url:
                    mark_seen(source_url=source_url)
                saved_no_email += 1

    return saved_email, saved_no_email, rejected


# ─── CSV readers ───

def get_all_leads():
    _ensure_file(LEADS_CSV, FIELDNAMES)
    leads = []
    try:
        with open(LEADS_CSV, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                leads.append(row)
    except Exception:
        pass
    return leads


def get_no_email_leads():
    _ensure_file(NO_EMAIL_CSV, FIELDNAMES)
    leads = []
    try:
        with open(NO_EMAIL_CSV, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                leads.append(row)
    except Exception:
        pass
    return leads


def get_stats():
    leads = get_all_leads()
    no_email = get_no_email_leads()
    keywords = {}
    statuses = {}
    for l in leads:
        k = l.get("source_keyword", "") or "unknown"
        keywords[k] = keywords.get(k, 0) + 1
        st = l.get("status", "new")
        statuses[st] = statuses.get(st, 0) + 1
    return {
        "total": len(leads),
        "with_email": len(leads),
        "no_email_count": len(no_email),
        "keywords": keywords,
        "statuses": statuses,
        "history_count": get_history_count(),
    }


def get_leads_with_email():
    return get_all_leads()


def get_sent_emails():
    _ensure_file(SENT_CSV, ["email", "name", "status", "sent_at"])
    sent = set()
    try:
        with open(SENT_CSV, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                e = (row.get("email") or "").strip().lower()
                if e:
                    sent.add(e)
    except Exception:
        pass
    return sent


def mark_sent(email):
    _ensure_file(SENT_CSV, ["email", "name", "status", "sent_at"])
    with open(SENT_CSV, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=["email", "name", "status", "sent_at"]).writerow({
            "email": email, "name": "", "status": "sent",
            "sent_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        })


def get_notified_emails():
    _ensure_file(NOTIFIED_CSV, ["email", "name", "status", "notified_at"])
    notified = set()
    try:
        with open(NOTIFIED_CSV, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                e = (row.get("email") or "").strip().lower()
                if e:
                    notified.add(e)
    except Exception:
        pass
    return notified


def mark_notified(email):
    _ensure_file(NOTIFIED_CSV, ["email", "name", "status", "notified_at"])
    with open(NOTIFIED_CSV, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=["email", "name", "status", "notified_at"]).writerow({
            "email": email, "name": "", "status": "notified",
            "notified_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        })


def update_lead_email(profile_url, email):
    leads = get_all_leads()
    updated = False
    for l in leads:
        if l.get("profile_url", "").lower() == profile_url.lower():
            l["email"] = email
            updated = True
    if updated:
        with open(LEADS_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            writer.writeheader()
            writer.writerows(leads)
    return updated


def move_to_email(profile_url, email):
    no_email = get_no_email_leads()
    target = None
    remaining = []
    for l in no_email:
        if l.get("profile_url", "").lower() == profile_url.lower():
            target = l
        else:
            remaining.append(l)
    if not target:
        return False
    target["email"] = email
    target["status"] = "new"
    target["added_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(NO_EMAIL_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(remaining)
    added, _, _ = add_leads([target], [])
    return True


def update_lead_status(profile_url, status):
    leads = get_all_leads()
    updated = False
    for l in leads:
        if l.get("profile_url", "").lower() == profile_url.lower():
            l["status"] = status
            updated = True
    if updated:
        with open(LEADS_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            writer.writeheader()
            writer.writerows(leads)
    return updated


def delete_lead(profile_url):
    leads = get_all_leads()
    before = len(leads)
    leads = [l for l in leads if l.get("profile_url", "").lower() != profile_url.lower()]
    if len(leads) < before:
        with open(LEADS_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            writer.writeheader()
            writer.writerows(leads)
        return True
    return False


def get_lead_by_email(email):
    """Return the saved lead row matching an email (case-insensitive), or None."""
    email = (email or "").strip().lower()
    if not email:
        return None
    for l in get_all_leads():
        if (l.get("email") or "").strip().lower() == email:
            return l
    return None


def update_email_status(email, **fields):
    """Persist email generation/send fields on a saved lead, keyed by email.

    Accepts statuses like generation_pending/generation_success/
    generation_failed/awaiting_approval/send_pending/sent/send_failed/skipped.
    Backward compatible: fields not in FIELDNAMES are ignored; existing lead
    data and all other fields are preserved.
    """
    email = (email or "").strip().lower()
    if not email:
        return False
    leads = get_all_leads()
    found = False
    for l in leads:
        if (l.get("email") or "").strip().lower() == email:
            for k, v in fields.items():
                if k in FIELDNAMES:
                    l[k] = str(v)
                    found = True
    if found:
        # Sanitize rows before rewriting: only ever emit FIELDNAMES columns so a
        # stray extra/blank column in the source CSV (parsed by dictreader as a
        # ``None`` key) can never crash csv.DictWriter with "dict contains fields
        # not in fieldnames". Preserves the schema; never adds random columns.
        clean = [{fn: l.get(fn, "") for fn in FIELDNAMES} for l in leads]
        with open(LEADS_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            writer.writeheader()
            writer.writerows(clean)
    return found


def get_daily_stats():
    """Load the engine daily counter for the dashboard."""
    path = os.path.join(DATA_DIR, "daily_state.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}
