"""Flask web app for LinkedIn lead generation."""
import os
import threading
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify

import scraper
import auth
import storage
import emailer
import engine

app = Flask(__name__)
app.secret_key = os.urandom(24)

SCRAPE_JOB = {"state": "idle", "message": "", "found": 0, "saved": 0, "saved_no_email": 0, "rejected": 0, "leads": [], "progress": "", "email_sources": {}}
ENGINE_JOB = {"state": "idle", "message": "", "stats": {}}


def _get_env_path():
    return os.path.join(os.path.dirname(__file__), ".env")


def _load_env():
    env = {}
    path = _get_env_path()
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                if "=" in line and not line.strip().startswith("#"):
                    k, v = line.strip().split("=", 1)
                    env[k] = v
    return env


def _get_li_credentials():
    env = _load_env()
    return env.get("LINKEDIN_EMAIL", ""), env.get("LINKEDIN_PASSWORD", "")


@app.route("/")
def index():
    stats = storage.get_stats()
    daily = storage.get_daily_stats()
    return render_template("index.html", stats=stats, job=SCRAPE_JOB, daily=daily, engine_job=ENGINE_JOB)


@app.route("/scrape", methods=["GET", "POST"])
def scrape():
    if request.method == "POST":
        if SCRAPE_JOB["state"] == "running":
            flash("Scrape already running!", "warning")
            return redirect(url_for("scrape"))

        keywords_raw = (request.form.get("keywords") or "").strip()
        needed = int(request.form.get("needed") or 10)
        needed = max(1, min(100, needed))
        date_posted = (request.form.get("date_posted") or "past-week").strip()

        if not keywords_raw:
            flash("Enter at least one keyword.", "error")
            return redirect(url_for("scrape"))

        li_email, li_pass = _get_li_credentials()
        if not li_email or not li_pass:
            flash("LinkedIn credentials not set. Go to Settings.", "error")
            return redirect(url_for("scrape"))

        use_all = request.form.get("use_all_keywords") == "on"
        if use_all:
            kw_path = os.path.join(os.path.dirname(__file__), "data", "keywords.txt")
            if os.path.exists(kw_path):
                with open(kw_path, encoding="utf-8") as f:
                    all_kws = [k.strip() for k in f.read().splitlines() if k.strip()]
                import random as _rnd
                keywords = _rnd.sample(all_kws, min(20, len(all_kws)))
            else:
                keywords = [k.strip() for k in keywords_raw.split(",") if k.strip()]
        else:
            keywords = [k.strip() for k in keywords_raw.split(",") if k.strip()]

        def worker():
            SCRAPE_JOB.update({"state": "running", "message": "Auto-login to LinkedIn...", "found": 0, "saved": 0, "saved_no_email": 0, "rejected": 0, "leads": [], "progress": "", "email_sources": {}})

            def progress_cb(msg):
                SCRAPE_JOB["progress"] = msg

            try:
                SCRAPE_JOB["message"] = "Authenticating..."
                session, msg = scraper.get_auto_session(li_email, li_pass)
                if not session:
                    SCRAPE_JOB.update({"state": "error", "message": f"Login failed: {msg}"})
                    return

                with_email, without_email, scrape_stats = scraper.deep_scrape(
                    session, keywords,
                    count_per_keyword=needed,
                    callback=progress_cb,
                )

                SCRAPE_JOB["message"] = f"Saving {len(with_email)} leads with email, {len(without_email)} without... (dedup: {scrape_stats.get('filtered', 0)} filtered)"
                saved_email, saved_no_email, rejected = storage.add_leads(with_email, without_email)
                SCRAPE_JOB.update({
                    "state": "done",
                    "leads": with_email[:20],
                    "message": f"Done! {saved_email} saved with email, {saved_no_email} without email. {rejected} rejected (already seen). {scrape_stats.get('emails_found', 0)} emails found via: {scrape_stats.get('sources', {})}",
                    "saved": saved_email,
                    "saved_no_email": saved_no_email,
                    "rejected": rejected,
                    "found": len(with_email) + len(without_email) + scrape_stats.get("filtered", 0),
                    "email_sources": scrape_stats.get("sources", {}),
                })
            except Exception as e:
                SCRAPE_JOB.update({"state": "error", "message": str(e)[:200]})

        SCRAPE_JOB.update({"state": "running", "message": "Starting...", "found": 0, "saved": 0, "leads": []})
        threading.Thread(target=worker, daemon=True).start()
        flash(f"Scraping {len(keywords)} keywords ({needed} each, {date_posted})...", "success")
        return redirect(url_for("scrape"))

    kw_path = os.path.join(os.path.dirname(__file__), "data", "keywords.txt")
    keywords_text = ""
    if os.path.exists(kw_path):
        with open(kw_path, encoding="utf-8") as f:
            keywords_text = f.read()
    return render_template("scrape.html", job=SCRAPE_JOB, keywords_text=keywords_text)


@app.route("/api/scrape/status")
def scrape_status():
    return jsonify(SCRAPE_JOB)


@app.route("/engine", methods=["POST"])
def engine_run():
    if ENGINE_JOB["state"] == "running":
        flash("Engine cycle already running!", "warning")
        return redirect(url_for("index"))

    def worker():
        ENGINE_JOB.update({"state": "running", "message": "Running no-login engine cycle...", "stats": {}})
        try:
            final, stats = engine.run_cycle()
            ENGINE_JOB.update({"state": "done", "message": f"Cycle done: {final} final leads saved.", "stats": stats})
        except Exception as e:
            ENGINE_JOB.update({"state": "error", "message": str(e)[:300], "stats": {}})

    ENGINE_JOB.update({"state": "running", "message": "Starting...", "stats": {}})
    threading.Thread(target=worker, daemon=True).start()
    flash("No-login engine cycle started (budget-guarded).", "success")
    return redirect(url_for("index"))


@app.route("/api/engine/status")
def engine_status():
    return jsonify(ENGINE_JOB)


@app.route("/leads")
def leads():
    all_leads = storage.get_all_leads()
    no_email_leads = storage.get_no_email_leads()
    kw_filter = request.args.get("keyword", "")
    tab = request.args.get("tab", "email")
    if kw_filter:
        all_leads = [l for l in all_leads if l.get("source_keyword", "").lower() == kw_filter.lower()]
        no_email_leads = [l for l in no_email_leads if l.get("source_keyword", "").lower() == kw_filter.lower()]
    stats = storage.get_stats()
    keywords = sorted(stats.get("keywords", {}).keys())
    display_leads = all_leads if tab == "email" else no_email_leads
    return render_template("leads.html", leads=display_leads, no_email_leads=no_email_leads, stats=stats, keywords=keywords, kw_filter=kw_filter, tab=tab)


@app.route("/leads/add-email", methods=["POST"])
def add_email():
    profile_url = request.form.get("profile_url", "")
    email = request.form.get("email", "").strip()
    if profile_url and email and "@" in email:
        moved = storage.move_to_email(profile_url, email)
        if moved:
            flash("Email saved — moved to email leads.", "success")
        else:
            storage.update_lead_email(profile_url, email)
            flash("Email saved.", "success")
    tab = request.form.get("tab", "no_email")
    return redirect(url_for("leads", tab=tab))


@app.route("/leads/delete", methods=["POST"])
def delete_lead():
    profile_url = request.form.get("profile_url", "")
    if profile_url:
        storage.delete_lead(profile_url)
        flash("Lead deleted.", "success")
    return redirect(url_for("leads"))


@app.route("/leads/export")
def export_leads():
    import csv
    tab = request.args.get("tab", "email")
    if tab == "no_email":
        all_leads = storage.get_no_email_leads()
    else:
        all_leads = storage.get_all_leads()
    path = os.path.join(os.path.dirname(__file__), "data", "leads_export.csv")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fields = ["name", "email", "profile_url", "post_text", "post_url", "source_keyword", "status", "added_at"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(all_leads)
    return (
        open(path, "r", encoding="utf-8").read(),
        200,
        {"Content-Type": "text/csv", "Content-Disposition": f"attachment; filename=leads_{'no_email' if tab == 'no_email' else 'email'}.csv"}
    )


@app.route("/send", methods=["GET", "POST"])
def send():
    if request.method == "POST":
        subject = (request.form.get("subject") or "").strip()
        body = (request.form.get("body") or "").strip()
        delay = int(request.form.get("delay") or 5)
        dry_run = request.form.get("dry_run") == "on"

        leads = storage.get_leads_with_email()
        sent_emails = storage.get_sent_emails()
        leads = [l for l in leads if (l.get("email") or "").strip().lower() not in sent_emails]

        if not leads:
            flash("No unsent leads with emails.", "warning")
            return redirect(url_for("send"))

        report = emailer.send_campaign(leads, subject, body, delay=delay, dry_run=dry_run)
        mode = "DRY RUN" if dry_run else "LIVE"
        flash(f"{mode}: {report['sent']} sent, {report['failed']} failed, {report['skipped']} skipped", "success")
        return redirect(url_for("send"))

    leads_with_email = storage.get_leads_with_email()
    sent_emails = storage.get_sent_emails()
    unsent = [l for l in leads_with_email if (l.get("email") or "").strip().lower() not in sent_emails]
    return render_template("send.html", total_with_email=len(leads_with_email), unsent_count=len(unsent))


@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        action = request.form.get("action")

        if action == "save_linkedin":
            li_email = (request.form.get("li_email") or "").strip()
            li_pass = (request.form.get("li_pass") or "").strip()
            if li_email and li_pass:
                _save_to_env({"LINKEDIN_EMAIL": li_email, "LINKEDIN_PASSWORD": li_pass})
                flash("LinkedIn credentials saved.", "success")
            else:
                flash("Enter both email and password.", "warning")

        elif action == "test_linkedin":
            env = _load_env()
            li_email = env.get("LINKEDIN_EMAIL", "")
            li_pass = env.get("LINKEDIN_PASSWORD", "")
            if not li_email or not li_pass:
                flash("No LinkedIn credentials saved yet.", "error")
            else:
                ok, msg = auth.test_auth(li_email, li_pass)
                flash(msg, "success" if ok else "error")

        elif action == "save_smtp":
            _save_to_env({
                "SMTP_HOST": request.form.get("smtp_host", "smtp.gmail.com"),
                "SMTP_PORT": request.form.get("smtp_port", "587"),
                "SMTP_USER": request.form.get("smtp_user", ""),
                "SMTP_PASS": request.form.get("smtp_pass", ""),
                "FROM_NAME": request.form.get("from_name", "Surya"),
                "FROM_EMAIL": request.form.get("from_email", ""),
            })
            emailer.reload_config()
            flash("SMTP settings saved.", "success")

        elif action == "test_smtp":
            ok, msg = emailer.verify_smtp()
            flash(msg, "success" if ok else "error")

        elif action == "save_keywords":
            kw_text = (request.form.get("keywords") or "").strip()
            kw_path = os.path.join(os.path.dirname(__file__), "data", "keywords.txt")
            os.makedirs(os.path.dirname(kw_path), exist_ok=True)
            with open(kw_path, "w", encoding="utf-8") as f:
                f.write(kw_text)
            flash("Keywords saved.", "success")

        return redirect(url_for("settings"))

    env = _load_env()
    li_email = env.get("LINKEDIN_EMAIL", "")
    li_pass_masked = "****" if env.get("LINKEDIN_PASSWORD") else ""
    smtp = {k: v for k, v in env.items() if k.startswith("SMTP") or k == "FROM_NAME" or k == "FROM_EMAIL"}

    kw_path = os.path.join(os.path.dirname(__file__), "data", "keywords.txt")
    keywords_text = ""
    if os.path.exists(kw_path):
        with open(kw_path, encoding="utf-8") as f:
            keywords_text = f.read()

    return render_template("settings.html", smtp=smtp, keywords_text=keywords_text,
                           li_email=li_email, li_pass_masked=li_pass_masked)


def _save_to_env(updates):
    env_path = _get_env_path()
    lines = []
    env_keys = set()
    if os.path.exists(env_path):
        with open(env_path) as f:
            lines = f.readlines()
    new_lines = []
    for line in lines:
        key = line.split("=")[0].strip()
        if key in updates:
            new_lines.append(f"{key}={updates[key]}\n")
            env_keys.add(key)
        else:
            new_lines.append(line)
    for k, v in updates.items():
        if k not in env_keys:
            new_lines.append(f"{k}={v}\n")
    with open(env_path, "w") as f:
        f.writelines(new_lines)


if __name__ == "__main__":
    storage.init()
    print("LinkedIn Leads -> http://127.0.0.1:5002")
    app.run(host="127.0.0.1", port=5002, debug=False)
