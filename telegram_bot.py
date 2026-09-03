"""Telegram bot — sends fresh lead cards to user's Telegram."""
import os
import json
import time
import requests

TOKEN_KEY = "TELEGRAM_BOT_TOKEN"
USER_KEY = "TELEGRAM_USER_ID"


def _load_env():
    env = {}
    path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                if "=" in line and not line.strip().startswith("#"):
                    k, v = line.strip().split("=", 1)
                    env[k] = v
    return env


def get_config():
    env = _load_env()
    return env.get(TOKEN_KEY, ""), env.get(USER_KEY, "")


def _send_text(text, parse_mode="Markdown"):
    token, chat_id = get_config()
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    for attempt in range(3):
        try:
            r = requests.post(url, json=data, timeout=20)
            if r.status_code == 200:
                return True
            # Rate limit: wait and retry
            if r.status_code == 429:
                time.sleep(5)
                continue
            return False
        except Exception:
            time.sleep(3)
    return False


def _escape_md(text):
    """Escape special Markdown chars that break Telegram parse."""
    if not text:
        return ""
    for ch in ["_", "*", "[", "]", "`"]:
        text = text.replace(ch, "\\" + ch)
    return text


def send_lead(lead):
    """Send a single formatted lead card to Telegram.

    For business-client (Google Business/Maps) leads the card shows the full
    prospect picture: business, category, rating/reviews, Website: NONE,
    phone, email, lead score, why-it's-a-prospect and source.
    Returns True only if the message was delivered.
    """
    name = (lead.get("name") or "Unknown").strip()
    company = (lead.get("company") or "").strip()
    role = (lead.get("role") or "").strip()
    email = (lead.get("email") or "").strip()
    phone = (lead.get("phone") or "").strip()
    whatsapp = (lead.get("whatsapp") or "").strip()
    location = (lead.get("location") or "").strip()
    category = (lead.get("category") or "").strip()
    rating = lead.get("rating")
    reviews = lead.get("reviews")
    lead_type = (lead.get("lead_type") or "").strip()
    web_status = (lead.get("web_status") or "").strip()
    web_reason = (lead.get("web_reason") or "").strip()
    intent_reason = (lead.get("intent_reason") or "").strip()
    opportunity = (lead.get("opportunity_reason") or "").strip()
    intent_score = lead.get("intent_score")
    biz_score = lead.get("business_client_score")
    post_text = (lead.get("post_text") or "").strip()[:300]
    source = (lead.get("source") or lead.get("email_source") or lead.get("source_keyword") or "").strip()
    source_url = (lead.get("source_url") or lead.get("post_url") or lead.get("profile_url") or "").strip()

    lines = []
    lines.append("🔹 **NEW LEAD FOUND**")
    lines.append(f"🏢 Business: {_escape_md(company or name)}")
    if name and company and name.lower() != (company or "").lower():
        lines.append(f"🏷 Name: {_escape_md(name)}")
    if category:
        lines.append(f"🏷 Category: {_escape_md(category)}")
    if location:
        lines.append(f"📍 Location: {_escape_md(location)}")
    if rating is not None:
        rline = f"⭐ {rating}"
        if reviews:
            rline += f" ({reviews} reviews)"
        lines.append(rline)
    if web_status == "NO_WEBSITE":
        wline = "🌐 Website: **No website**"
        if lead.get("domain"):
            wline += f" ({_escape_md(lead.get('domain'))})"
        lines.append(wline)
    elif lead.get("domain"):
        label = ""
        if web_status == "PLACEHOLDER_WEBSITE":
            label = " · placeholder/parking"
        elif web_status == "BROKEN_WEBSITE":
            label = " · broken/dead"
        elif web_status == "UNKNOWN":
            label = " · status unknown"
        lines.append(f"🌐 Website: {_escape_md(lead.get('domain'))}{label}")
    if phone:
        lines.append(f"📞 Phone: {_escape_md(phone)}")
    if whatsapp and whatsapp.lower() in ("yes", "1", "true"):
        lines.append("💬 WhatsApp: available")
    if email:
        lines.append(f"📧 Email: {_escape_md(email)}")
    else:
        lines.append("📧 Email: *not found*")
    if biz_score is not None:
        lines.append(f"🎯 Lead score: {biz_score}")
    elif intent_score is not None:
        lines.append(f"🎯 Intent score: {intent_score}")
    why = opportunity or intent_reason or web_reason
    if why:
        lines.append(f"💡 Why: {_escape_md(why)}")
    if lead_type:
        lines.append(f"🎯 Type: {_escape_md(lead_type.replace('_', ' ').title())}")
    if post_text:
        lines.append(f"📝 Post: {_escape_md(post_text)}")
    if source:
        lines.append(f"🏷 Source: {_escape_md(source)}")
    if source_url:
        lines.append(f"🔗 [Source URL]({_escape_md(source_url)})")

    msg = "\n".join(lines)
    return _send_text(msg, parse_mode="")


def send_summary(count, total_saved, rejected):
    """Send hourly summary card."""
    msg = (
        "✅ **Hourly Lead Report**\n"
        f"🆕 Fresh leads this hour: **{count}**\n"
        f"💾 Total saved (email): **{total_saved}**\n"
        f"🚫 Already-seen rejected: **{rejected}**\n"
        f"🕒 Next round: ~1 hour"
    )
    return _send_text(msg)


def send_status(msg):
    """Send status/error notification."""
    return _send_text(f"⚙️ {_escape_md(str(msg))}")


def send_email_draft(draft):
    """Send a Telegram DRAFT preview for human approval (no SMTP yet)."""
    draft_id = draft.get("id") or draft.get("email")
    company = (draft.get("company") or draft.get("name") or "").strip()
    to = (draft.get("email") or "").strip()
    subject = (draft.get("subject") or "").strip()
    body = (draft.get("body") or "").strip()
    lines = []
    lines.append("📧 **EMAIL DRAFT**")
    lines.append(f"To: {to}")
    if company:
        lines.append(f"Company: {_escape_md(company)}")
    lines.append(f"Subject: {_escape_md(subject)}")
    lines.append("")
    lines.append(body)
    lines.append("")
    lines.append("Approve/send or skip:")
    lines.append(f"`/send {draft_id}`  ·  `/skip {draft_id}`")
    msg = "\n".join(lines)
    return _send_text(msg, parse_mode="HTML")


def send_email_sent(lead, subject, sent_at):
    """Notify that a cold email was successfully sent via SMTP."""
    company = (lead.get("company") or lead.get("name") or "").strip()
    to = (lead.get("email") or "").strip()
    intent = (lead.get("intent_reason") or lead.get("intent_type") or "").strip()
    score = lead.get("intent_score")
    lead_type = (lead.get("lead_type") or "").strip()
    source = (lead.get("source") or lead.get("email_source") or lead.get("source_keyword") or "").strip()
    lines = []
    lines.append("✅ **Cold Email SENT**")
    if company:
        lines.append(f"Company: {_escape_md(company)}")
    lines.append(f"To: {to}")
    lines.append(f"Subject: {_escape_md(subject)}")
    if intent:
        lines.append(f"Intent: {_escape_md(intent)}")
    if score is not None:
        lines.append(f"Score: {score}")
    if lead_type:
        lines.append(f"Source: {_escape_md(lead_type)}")
    lines.append("Status: SENT")
    if sent_at:
        lines.append(f"Time: {sent_at}")
    return _send_text("\n".join(lines))


def send_email_failed(company, reason):
    """Notify that a cold email failed to send via SMTP."""
    lines = ["❌ **Email sending failed**"]
    if company:
        lines.append(f"Company: {_escape_md(company)}")
    if reason:
        lines.append(f"Reason: {_escape_md(str(reason))}")
    return _send_text("\n".join(lines))


def send_generation_failed(company, reason):
    """Notify that Gemini cold-email generation failed."""
    lines = ["❌ **Cold email generation failed**"]
    if company:
        lines.append(f"Company: {_escape_md(company)}")
    if reason:
        lines.append(f"Reason: {_escape_md(str(reason))}")
    return _send_text("\n".join(lines))
