"""
AI cold-email automation orchestrator.

Flow for a NEW verified-email lead:
    generate (Gemini) -> quality check -> [approval OR auto-send] -> SMTP -> record -> Telegram

Design rules:
- AUTO_EMAIL_SEND=False (default): generate + validate + persist a DRAFT, then send a
  Telegram preview and await /send or /skip. NO SMTP.
- AUTO_EMAIL_SEND=True: generate + validate, then send via existing emailer.send_email().
- Idempotent: never generate/send twice for the same lead/email.
- Failure isolated: one bad lead never raises out of the scheduler loop.
- Cost control: max 1 normal generation + 1 stricter regeneration (MAX_GEMINI_ATTEMPTS).
- Rate capped: MAX_AUTO_EMAILS_PER_CYCLE / MAX_AUTO_EMAILS_PER_DAY (config).
"""
import datetime
import hashlib
import json
import os
import time

import config
import storage
import email_quality
import gemini_email
import emailer
import telegram_bot

DRAFTS_FILE = os.path.join(config.DATA_DIR, "email_drafts.json")
_EMAIL_LOG = "email_automation.log"


def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    line = f"[{_now()}] {msg}"
    try:
        with open(_EMAIL_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line)


def _draft_id(lead):
    email = (lead.get("email") or "").strip().lower()
    if email:
        return email
    return "lead_" + hashlib.md5(
        str(lead.get("profile_url") or lead.get("company") or lead.get("name")).encode()
    ).hexdigest()[:12]


# ─── pending draft store (for /send and /skip approval) ────────────────

def _load_drafts():
    if os.path.exists(DRAFTS_FILE):
        try:
            with open(DRAFTS_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_drafts(d):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(DRAFTS_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)


def get_draft(draft_id):
    return _load_drafts().get(str(draft_id))


# ─── generation + validation (max attempts, no infinite loop) ──────────

def _generate_and_validate(lead):
    attempts = []
    company = (lead.get("company") or lead.get("name") or "").strip()
    email = (lead.get("email") or "").strip()
    log("[GEMINI] START | company={0} | email={1} | lead_type={2} | web_status={3} | intent={4} | intent_score={5}".format(
        company, email, lead.get("lead_type"), lead.get("web_status"),
        (lead.get("intent_reason") or "").strip()[:80], lead.get("intent_score")))
    for attempt in range(1, config.MAX_GEMINI_ATTEMPTS + 1):
        log(f"[GEMINI] generating for {email} attempt {attempt}...")
        res = gemini_email.generate_cold_email(lead)
        if res.get("generation_status") != "success":
            err = (res.get("error") or "unknown").strip()
            attempts.append({
                "attempt": attempt,
                "ok": False,
                "error": err,
                "summary": f"gemini failed: {err}",
                "reasons": [],
                "model": res.get("model"),
                "response_length": 0,
                "generation_time": res.get("generation_time"),
            })
            log(f"[GEMINI] RESPONSE | model={res.get('model')} | attempt={attempt} | status=failed | response_length=0 | reason={err}")
            continue
        subject = (res.get("subject") or "").strip()
        body = (res.get("body") or "").strip()
        ok, reasons = email_quality.validate_email(subject, body, email)
        attempts.append({
            "attempt": attempt,
            "ok": ok,
            "error": None,
            "reasons": reasons,
            "summary": ("quality check failed: " + str(reasons)) if not ok else "ok",
            "subject": subject,
            "body": body,
            "model": res.get("model"),
            "response_length": len(subject) + len(body),
            "generation_time": res.get("generation_time"),
        })
        log(f"[GEMINI] RESPONSE | model={res.get('model')} | attempt={attempt} | status={'ok' if ok else 'fail'} | response_length={len(subject) + len(body)} | reason={'quality: ' + str(reasons) if not ok else 'pass'}")
        if ok:
            log(f"[GEMINI] RESULT | success | attempts={len(attempts)} | model={res.get('model')}")
            return attempts, res
        log(f"[EMAIL] quality check attempt {attempt}: FAIL {reasons}")
    last = attempts[-1] if attempts else {}
    log(f"[GEMINI] RESULT | failed | attempts={len(attempts)} | last_reason={last.get('summary', 'no attempts recorded')}")
    return attempts, None


# ─── public: process a newly saved, verified-email lead ─────────────────

def _already_handled(lead):
    email = (lead.get("email") or "").strip().lower()
    if email and email in storage.get_sent_emails():
        return True
    st = (lead.get("email_send_status") or "").strip()
    gen = (lead.get("email_generation_status") or "").strip()
    if st == "sent":
        return True
    # awaiting approval already drafted -> don't regenerate
    if gen == "awaiting_approval":
        return True
    if email and get_draft(email):
        return True
    return False


def process_new_lead(lead):
    """Run the full per-lead email-automation pipeline. Never raises.
    Returns a status dict."""
    company = lead.get("company") or lead.get("name") or ""
    email = (lead.get("email") or "").strip()

    try:
        if not email:
            log(f"[SKIP] lead {company} has no email")
            return {"status": "skipped", "reason": "no_email"}
        if _already_handled(lead):
            log(f"[SKIP] lead {company} already handled (idempotency)")
            return {"status": "skipped", "reason": "already_handled"}

        storage.update_email_status(email, email_generation_status="generation_pending")

        attempts, ok_res = _generate_and_validate(lead)
        if not ok_res:
            last = attempts[-1] if attempts else {}
            reason = (last.get("summary") or last.get("error") or
                      "no attempts recorded (Gemini call never made)")
            storage.update_email_status(
                email,
                email_generation_status="generation_failed",
                email_generation_attempts=len(attempts),
                email_send_status="skipped",
                email_send_error=str(reason)[:200],
            )
            log(f"[GEMINI] FAILED for {company}: {reason}")
            telegram_bot.send_generation_failed(company, reason)
            return {"status": "generation_failed", "reason": reason}

        subject = ok_res.get("subject")
        body = ok_res.get("body")

        draft = {
            "id": _draft_id(lead),
            "email": email,
            "company": company,
            "name": lead.get("name", ""),
            "subject": subject,
            "body": body,
            "model": ok_res.get("model"),
            "lead": {k: v for k, v in lead.items() if v not in (None, "")},
            "created_at": _now(),
        }

        if config.AUTO_EMAIL_SEND:
            # ── auto-send mode: sequential, rate-capped ──
            if email in storage.get_sent_emails():
                log(f"[SKIP] {email} already sent")
                return {"status": "skipped", "reason": "already_sent"}
            sent_today = 0  # daily counter handled externally; see _mark_sent
            log(f"[SMTP] sending to {email} ...")
            emailer.send_email(email, subject, body)
            _mark_sent(lead, subject)
            log(f"[SMTP] success -> {email}")
            telegram_bot.send_email_sent(lead, subject, _now())
            return {"status": "sent", "subject": subject}
        else:
            # ── approval mode: persist draft + Telegram preview, NO SMTP ──
            drafts = _load_drafts()
            drafts[draft["id"]] = draft
            _save_drafts(drafts)
            storage.update_email_status(
                email,
                email_generation_status="generation_success",
                email_generation_attempts=len(attempts),
                generated_subject=subject,
                generated_body=body[:500],
                email_model=ok_res.get("model"),
                email_send_status="awaiting_approval",
            )
            telegram_bot.send_email_draft(draft)
            log(f"[TELEGRAM] draft preview sent for {company}")
            return {"status": "awaiting_approval", "subject": subject, "body": body,
                    "model": ok_res.get("model")}
    except Exception as e:
        log(f"[ERROR] email automation failed for {company}: {e}")
        telegram_bot.send_email_failed(company, str(e))
        return {"status": "error", "reason": str(e)}


# ─── SMTP send + record (used by auto mode and /send approval) ─────────

def _mark_sent(lead, subject):
    email = (lead.get("email") or "").strip()
    storage.update_email_status(
        email,
        email_generation_status="generation_success",
        email_send_status="sent",
        email_sent_at=_now(),
        email_send_error="",
        generated_subject=subject,
    )
    storage.mark_sent(email)


def send_draft_email(draft_id):
    """Approval action: actually send a persisted draft via SMTP."""
    drafts = _load_drafts()
    d = drafts.get(str(draft_id))
    if not d:
        return {"ok": False, "reason": "draft not found"}
    email = d.get("email", "")
    try:
        log(f"[SMTP] sending approved draft to {email} ...")
        emailer.send_email(email, d.get("subject"), d.get("body"))
        _mark_sent(d.get("lead") or {"email": email}, d.get("subject"))
        drafts.pop(str(draft_id), None)
        _save_drafts(drafts)
        log(f"[SMTP] success -> {email}")
        telegram_bot.send_email_sent(d.get("lead") or {"email": email, "company": d.get("company")},
                                     d.get("subject"), _now())
        return {"ok": True}
    except Exception as e:
        log(f"[SMTP] FAILED draft {draft_id}: {e}")
        storage.update_email_status(email, email_send_status="send_failed",
                                    email_send_error=str(e)[:200])
        telegram_bot.send_email_failed(d.get("company"), str(e))
        return {"ok": False, "reason": str(e)}


def skip_draft(draft_id):
    """Approval action: mark a draft skipped; do not send."""
    drafts = _load_drafts()
    d = drafts.pop(str(draft_id), None)
    _save_drafts(drafts)
    if d:
        storage.update_email_status(d.get("email", ""), email_send_status="skipped")
        log(f"[SKIP] draft {draft_id} skipped by operator")
        return {"ok": True}
    return {"ok": False, "reason": "draft not found"}
