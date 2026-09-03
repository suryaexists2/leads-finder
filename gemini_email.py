"""
Gemini cold-email generation layer.

Produces a personalized cold email for a qualified lead using the Gemini REST
API. Reads the API key + model from .env via config (never hardcoded here).

Returns structured output:
    {
        "subject": str,
        "body": str,
        "model": str,
        "generation_status": "success" | "failed",
        "error": str | None,
    }

On failure it returns generation_status="failed" and NEVER returns an
empty/generic email for sending. Callers must not send when status != success.
"""
import json
import re
import time

import requests

import config


def _call_gemini(system_prompt, user_prompt, timeout=config.GEMINI_TIMEOUT_SECONDS,
                 max_calls=config.MAX_GEMINI_TOTAL_CALLS):
    """POST to Gemini REST API forcing JSON output.

    The spec is folded into the user prompt (NOT system_instruction) because
    combining a long system_instruction with responseMimeType=json triggers
    output truncation on flash models. Tries the primary model then fallbacks,
    retrying transient (429/503) errors, but never exceeds `max_calls` total
    HTTP requests (free tier is 20 req/min).

    Returns (text, model_used). Raises RuntimeError on failure."""
    key = config.get_gemini_api_key()
    if not key:
        raise RuntimeError("GEMINI_API_KEY not set in .env")
    last_err = None
    calls = 0
    # system_prompt is the collection of rules; user_prompt is the lead + task.
    # Combine them into one user turn to avoid the system_instruction truncation bug.
    user_text = f"{system_prompt}\n\n---\n\n{user_prompt}"
    for model in config.get_gemini_models():
        url = f"{config.GEMINI_BASE_URL}/models/{model}:generateContent"
        payload = {
            "contents": [{"role": "user", "parts": [{"text": user_text}]}],
            "generationConfig": {
                "temperature": 0.7,
                # Flash-class models burn a large share of the output budget on
                # hidden "thoughts" tokens. A 1200-token budget left ~35 tokens
                # for the visible JSON -> finishReason MAX_TOKENS -> truncated
                # output -> unparseable. 2200 gives thought+output room to FIT.
                "maxOutputTokens": 2200,
                "responseMimeType": "application/json",
            },
        }
        for attempt in range(config.GEMINI_MAX_TRANSIENT_RETRIES + 1):
            if calls >= max_calls:
                break
            calls += 1
            try:
                r = requests.post(url, params={"key": key}, json=payload, timeout=timeout)
            except requests.exceptions.RequestException as e:
                last_err = RuntimeError(f"request error on {model}: {e}")
                time.sleep(config.GEMINI_TRANSIENT_SLEEP)
                continue
            if r.status_code in (429, 500, 503):
                last_err = RuntimeError(f"Gemini transient error {r.status_code} on {model}")
                if attempt < config.GEMINI_MAX_TRANSIENT_RETRIES:
                    time.sleep(config.GEMINI_TRANSIENT_SLEEP)
                    continue
                break
            if r.status_code != 200:
                last_err = RuntimeError(f"Gemini API error {r.status_code} on {model}: {r.text[:200]}")
                break
            data = r.json()
            candidates = data.get("candidates") or []
            if not candidates:
                last_err = RuntimeError(f"Gemini returned no candidates on {model}")
                break
            finish = (candidates[0].get("finishReason") or "").upper()
            parts = (candidates[0].get("content") or {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts) or ""
            if not text.strip():
                last_err = RuntimeError(f"Gemini returned empty text on {model}")
                break
            if finish == "MAX_TOKENS":
                # The visible output was cut off before the JSON completed. This
                # model/call is unusable regardless of retrying the same prompt.
                usage = data.get("usageMetadata") or {}
                last_err = RuntimeError(
                    "Gemini output truncated (finishReason=MAX_TOKENS) on {0}: "
                    "candidates={1} thoughts={2}".format(
                        model, usage.get("candidatesTokenCount"), usage.get("thoughtsTokenCount")))
                break
            return text.strip(), model
    raise last_err if last_err else RuntimeError("Gemini all models failed")


def _safe_json(text):
    """Parse JSON from Gemini output (handles markdown fences and stray text)."""
    if not text:
        return None
    # strip code fences
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S | re.I)
    if fenced:
        text = fenced.group(1)
    try:
        return json.loads(text)
    except Exception:
        pass
    # try to find first { ... } block
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            pass
    return None


def _lead_context(lead):
    """Build a compact dict of just the fields that actually exist on the lead."""
    keys = ["name", "company", "role", "email", "website", "company_website",
            "location", "country", "category", "address", "rating", "reviews",
            "lead_type", "source", "source_keyword",
            "source_url", "post_url", "profile_url", "business_client_score",
            "intent_score", "intent_type",
            "intent_reason", "intent_evidence", "title", "job_title", "post_text",
            "web_status", "web_reason", "web_confidence", "opportunity_reason"]
    ctx = {}
    for k in keys:
        v = lead.get(k)
        if v not in (None, ""):
            ctx[k] = v if isinstance(v, str) else str(v)
    return ctx


_SYSTEM_PROMPT = """You are a professional that helps write short, natural,
human-sounding cold emails for a web developer named Surya who is reaching out
to companies that have shown genuine buying intent for web-development work.

Every email MUST follow this EXACT structure:

Subject: <short, directly tied to the lead's actual requirement>

Hi <Company/Person Name>,

I came across your <exact requirement/job/project> and wanted to reach out.

I'm a web developer and can help with <ONLY the skills/services relevant to this
specific lead>.

You can check my work here:

LinkedIn: https://www.linkedin.com/in/surya-kant-pandey-2a54b0298
GitHub: https://github.com/suryaexists2

If you're still looking for a developer, please reply to this email. I'd be happy
to discuss the requirements.

Best,
Surya

HARD RULES:
- Keep it around 80-150 words. Short, simple English, professional, friendly,
  direct, natural, personalized.
- The first paragraph MUST be based on the actual lead intent/requirement that is
  present in the supplied lead data. Use the exact job/project title when available.
- NEVER invent facts. NEVER claim we spoke to them. NEVER claim we saw something
  not present in the supplied data. NEVER fabricate a person's name, company info,
  project details, budget, tech stack, timeline, testimonials, case studies, clients,
  results, or portfolio claims.
- Select ONLY 3-5 skills relevant to the LEAD (e.g. WordPress lead -> WordPress,
  Elementor, Figma-to-WordPress, JavaScript; React lead -> React, Next.js,
  JavaScript, APIs; website lead -> website development, responsive UI, JavaScript,
  APIs; full-stack lead -> frontend, backend, APIs, databases, deployment). Do NOT
  list every skill.
- DO NOT mention AI, Gemini, or that this email was generated. No emojis. No
  "Dear Sir/Madam". No "I hope this email finds you well". No "I am writing to
  express my interest". No "We are a leading...". No aggressive CTAs like "Book a
  call now".
- Always end with the exact reply-based CTA and exact signature and exact
  LinkedIn/GitHub links shown above (never shorten or alter the links).
- Do not invent a recipient's personal name unless it is present in the data; use
  the company name as the greeting otherwise.

Return your answer as JSON ONLY with two keys: "subject" and "body". Do not add
any commentary outside the JSON."""


_NO_WEBSITE_SYSTEM_PROMPT = """You write short, natural, human-sounding cold
outreach for a web developer named Surya reaching out to REAL LOCAL BUSINESSES
that do NOT currently have a proper website (typically found via their Google
Business / Maps listing or their social profile) and could buy a brand-new
website / web application built from scratch.

This is the ONLY thing we sell: a NEW website / NEW web application. We NEVER
pitch website maintenance, redesign, SEO, speed/performance optimization,
WordPress maintenance, SSL fixes, or any kind of improvement of an existing
website.

Every email MUST follow this EXACT structure:

Subject: <short, honest, tied to the business and the fact it has no website>

Hi <Company Name>,

I came across <Company>, a <category> in <city>, and noticed it doesn't appear
to have a dedicated website that shows customers what you offer or makes it
easy for them to reach you.

I'm a web developer and I build professional websites and web applications for
businesses like yours - a clean, modern site that looks great on phones, explains
what you do, and helps customers find and contact you.

You can check my work here:

LinkedIn: https://www.linkedin.com/in/surya-kant-pandey-2a54b0298
GitHub: https://github.com/suryaexists2

If you'd be open to seeing how a brand-new website could help <Company>, just
reply to this email and I'll walk you through it.

Best,
Surya

HARD RULES:
- Keep it around 80-150 words. Short, simple English, professional, friendly,
  direct, natural.
- Base everything ONLY on the supplied lead data (company, category/industry,
  city, web_status). Use the supplied category and city when present. NEVER
  invent facts: no invented revenue, reviews, customers, staff, location
  details, awards, or results.
- The opening line wants the "I came across <Company>, a <category> in <city>,
  and noticed it doesn't appear to have a dedicated website..." form. NEVER use
  or imply: "I came across your website", "your website is", "while browsing
  your site", "on your website". There IS no website to have come across.
- The greeting uses the COMPANY name. Never fabricate a person's name unless it
  is present in the supplied data.
- NEVER mention: "maintenance", "redesign", "SEO", "search engine optimization",
  "speed or performance optimization", "SSL", "WordPress fixes", "I can
  optimize/improve/update your existing website". This is a NEW-website pitch.
- If the business has no website, acknowledge that naturally and positively -
  never mock or insult. Never claim we visited a website that does not exist.
- DO NOT mention AI, Gemini, or that this email was generated. No emojis. No
  "Dear Sir/Madam". No "I hope this email finds you well". No aggressive CTAs
  like "Book a call now".
- Always end with the exact reply-based CTA, the exact signature, and the exact
  LinkedIn/GitHub links shown above (never shorten or alter the links).

Return your answer as JSON ONLY with two keys: "subject" and "body". Do not add
any commentary outside the JSON."""


def _pick_system_prompt(lead):
    """A qualified business-client lead always gets the NEW-website pitch. Any
    re-enabled intent lead (job/hire post) keeps the legacy intent prompt."""
    if lead.get("lead_type") == "business_client":
        return _NO_WEBSITE_SYSTEM_PROMPT
    ws = lead.get("web_status") or ""
    if ws in ("NO_WEBSITE", "BROKEN_WEBSITE", "PLACEHOLDER_WEBSITE"):
        return _NO_WEBSITE_SYSTEM_PROMPT
    return _SYSTEM_PROMPT


def generate_cold_email(lead, timeout=config.GEMINI_TIMEOUT_SECONDS):
    """Generate a personalized cold email for a lead.

    Returns dict {subject, body, model, generation_status, error}.
    generation_status is "success" or "failed". Callers must NOT send unless
    status == "success".
    """
    start = time.time()
    ctx = _lead_context(lead)
    user_prompt = (
        "Lead data (only include what is genuinely present and relevant):\n"
        + json.dumps(ctx, ensure_ascii=False, indent=2)
        + "\n\nWrite the cold email now."
    )
    try:
        raw, used_model = _call_gemini(_pick_system_prompt(lead), user_prompt, timeout=timeout)
        parsed = _safe_json(raw)
        if not parsed or not parsed.get("subject") or not parsed.get("body"):
            raise RuntimeError("Gemini output missing subject/body (could not parse JSON)")
        return {
            "subject": str(parsed["subject"]).strip(),
            "body": str(parsed["body"]).strip(),
            "model": used_model,
            "generation_status": "success",
            "generation_time": round(time.time() - start, 2),
            "error": None,
        }
    except Exception as e:
        return {
            "subject": "",
            "body": "",
            "model": config.GEMINI_MODEL,
            "generation_status": "failed",
            "generation_time": round(time.time() - start, 2),
            "error": str(e),
        }
