"""Cold email sender via SMTP."""
import os
import re
import smtplib
import time
from email.message import EmailMessage
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

def reload_config():
    global SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, FROM_NAME, FROM_EMAIL
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=True)
    SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
    SMTP_USER = os.environ.get("SMTP_USER", "")
    SMTP_PASS = os.environ.get("SMTP_PASS", "")
    FROM_NAME = os.environ.get("FROM_NAME", "Surya")
    FROM_EMAIL = os.environ.get("FROM_EMAIL", SMTP_USER)


SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
FROM_NAME = os.environ.get("FROM_NAME", "Surya")
FROM_EMAIL = os.environ.get("FROM_EMAIL", SMTP_USER)


def _connect():
    host = os.environ.get("SMTP_HOST", SMTP_HOST)
    port = int(os.environ.get("SMTP_PORT", SMTP_PORT))
    user = os.environ.get("SMTP_USER", SMTP_USER)
    passwd = os.environ.get("SMTP_PASS", SMTP_PASS)
    if not user or not passwd:
        raise ConnectionError("SMTP_USER / SMTP_PASS not set. Go to Settings.")
    smtp = smtplib.SMTP(host, port, timeout=25)
    smtp.ehlo()
    smtp.starttls()
    smtp.login(user, passwd)
    return smtp


def _personalize(template, lead):
    text = re.sub(r"\{\{?\s*name\s*\}?\}", (lead.get("name") or "").strip() or "there", template)
    text = re.sub(r"\{\{?\s*company\s*\}?\}", (lead.get("company") or "").strip() or "your company", template)
    text = re.sub(r"\{\{?\s*headline\s*\}?\}", (lead.get("headline") or "").strip() or "", text)
    text = re.sub(r"\{\{?\s*country\s*\}?\}", (lead.get("country") or "").strip() or "", text)
    return text


def send_test_email(to_email):
    smtp = _connect()
    msg = EmailMessage()
    msg["Subject"] = "Test Email - OfflChat Lead System"
    msg["From"] = f"{FROM_NAME} <{FROM_EMAIL}>"
    msg["To"] = to_email
    msg.set_content(
        "This is a test email from your lead generation system.\n\n"
        "If you received this, SMTP is configured correctly."
    )
    smtp.send_message(msg)
    smtp.quit()
    return True


def send_email(to_email, subject, body):
    """Send a single cold email via the existing SMTP connection layer.

    Thin convenience wrapper reused by the AI email-automation layer so it calls
    send_email(to, subject, body). Reuses _connect() + the same EmailMessage
    pattern as send_campaign — no duplicate SMTP architecture.
    Returns True on success, raises on failure (caller decides handling).
    """
    if not to_email or "@" not in to_email:
        raise ValueError("Invalid recipient email")
    if not subject or not body:
        raise ValueError("Subject/body required")
    smtp = _connect()
    try:
        msg = EmailMessage()
        msg["Subject"] = subject.strip()
        msg["From"] = f"{FROM_NAME} <{FROM_EMAIL}>"
        msg["To"] = to_email.strip()
        msg.set_content(body)
        smtp.send_message(msg)
    finally:
        try:
            smtp.quit()
        except Exception:
            pass
    return True


def send_campaign(leads, subject, body, delay=5, dry_run=False, progress_cb=None):
    results = {"total": len(leads), "sent": 0, "failed": 0, "skipped": 0, "details": []}
    smtp = None
    for i, lead in enumerate(leads):
        email = (lead.get("email") or "").strip()
        if not email or "@" not in email:
            results["skipped"] += 1
            results["details"].append({"email": email, "status": "skipped", "reason": "no email"})
            continue
        subj = _personalize(subject, lead)
        body_text = _personalize(body, lead)
        if dry_run:
            results["sent"] += 1
            results["details"].append({"email": email, "status": "dry_run", "subject": subj})
            if progress_cb:
                progress_cb(i + 1, len(leads), email, "dry_run")
            continue
        msg = EmailMessage()
        msg["Subject"] = subj
        msg["From"] = f"{FROM_NAME} <{FROM_EMAIL}>"
        msg["To"] = email
        msg.set_content(body_text)
        try:
            if smtp is None:
                smtp = _connect()
            smtp.send_message(msg)
            results["sent"] += 1
            results["details"].append({"email": email, "status": "sent"})
            from storage import mark_sent
            mark_sent(email)
        except Exception as e:
            results["failed"] += 1
            results["details"].append({"email": email, "status": "failed", "reason": str(e)[:120]})
            smtp = None
        if progress_cb:
            progress_cb(i + 1, len(leads), email,
                        "sent" if results["details"][-1]["status"] == "sent" else "failed")
        if delay > 0:
            time.sleep(delay)
    if smtp:
        try:
            smtp.quit()
        except Exception:
            pass
    return results


def verify_smtp():
    try:
        smtp = _connect()
        smtp.quit()
        return True, "SMTP connected successfully"
    except Exception as e:
        return False, str(e)
