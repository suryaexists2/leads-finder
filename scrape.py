"""
LinkedIn Terminal Scraper - Uses real Edge browser session.

Usage:
    python scrape.py                          # scrape all keywords in data/keywords.txt
    python scrape.py "need web developer"     # scrape single keyword
    python scrape.py --top 5                  # scrape first 5 keywords only
"""
import sys
import time
import re
import os
import argparse
import pyautogui
import pyperclip
import pygetwindow as gw
import ctypes

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.1


def focus_edge():
    for w in gw.getAllWindows():
        if not w.visible or not w.title.strip():
            continue
        t = w.title.encode("ascii", "replace").decode()
        if "Microsoft" in t and "Edge" in t:
            try:
                w.activate()
            except Exception:
                ctypes.windll.user32.SetForegroundWindow(w._hWnd)
            time.sleep(0.4)
            return w
    return None


def navigate(url):
    pyautogui.hotkey("alt", "d")
    time.sleep(0.2)
    pyperclip.copy(url)
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.05)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.05)
    pyautogui.press("enter")
    time.sleep(5)


def copy_page():
    win = focus_edge()
    if not win:
        return ""
    pyautogui.click(win.left + win.width // 2, win.top + win.height // 2)
    time.sleep(0.3)
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.4)
    pyautogui.hotkey("ctrl", "c")
    time.sleep(0.8)
    return pyperclip.paste()


def parse_posts(text, keyword):
    text = text.replace("\u200b", "").replace("\xa0", " ")
    posts_start = text.find("Feed post")
    if posts_start == -1:
        posts_start = text.find("Sort by")
    if posts_start == -1:
        return []

    post_section = text[posts_start:]
    blocks = re.split(r"\n\s*Feed post\s*\n", post_section)
    leads = []

    skip_words = {
        "home", "my network", "jobs", "messaging", "notifications",
        "more", "for business", "try premium", "posts", "past week",
        "sort by", "content type", "from member", "all filters",
        "reset", "follow", "join", "like", "comment", "repost",
        "send", "see more", "view profile", "connect", "message",
    }

    for block in blocks:
        lines = [l.strip() for l in block.split("\n") if l.strip()]
        if len(lines) < 2:
            continue

        author = ""
        for line in lines:
            m = re.search(r"View\s+(.+?)'s?\s+profile", line)
            if m:
                author = m.group(1).strip()
                break

        if not author:
            for line in lines:
                low = line.lower()
                if (3 < len(line) < 50 and line[0].isupper()
                        and low not in skip_words
                        and not low.startswith("view ")
                        and not low.startswith("http")
                        and not re.match(r"^\d+$", line)
                        and not re.match(r"^\d+[dhm]\b", line)
                        and "linkedin" not in low):
                    words = line.split()
                    if 1 <= len(words) <= 4 and all(w[0].isupper() for w in words if len(w) > 1):
                        author = line
                        break

        if not author or author.lower() in skip_words:
            continue

        slug = re.sub(r"[^a-z0-9-]", "", author.lower().replace(" ", "-").replace(".", "").replace("'", ""))
        profile_url = f"https://www.linkedin.com/in/{slug}/"

        post_lines = []
        for line in lines:
            low = line.lower()
            if (line != author
                    and not re.match(r"^\d+$", line)
                    and not re.match(r"^\d+[dhm]\b", line)
                    and low not in skip_words
                    and not line.startswith("linkedin.com")
                    and len(line) > 5):
                post_lines.append(line)

        post_text = " ".join(post_lines[:3])[:300]

        # Detect emails in post text
        emails = re.findall(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", post_text)
        skip_domains = {"example.com", "email.com", "test.com", "linkedin.com", "sentry.io"}
        email = ""
        for e in emails:
            if e.split("@")[1].lower() not in skip_domains:
                email = e
                break

        leads.append({
            "name": author,
            "profile_url": profile_url,
            "post_text": post_text,
            "email": email,
            "source_keyword": keyword,
        })

    return leads


def scrape_keyword(keyword, scrolls=1):
    win = focus_edge()
    if not win:
        print("  [!] Edge not found")
        return []

    url = f"https://www.linkedin.com/search/results/content/?keywords={keyword.replace(' ', '+')}&datePosted=past-week&sortBy=RELEVANCE"
    navigate(url)

    for _ in range(scrolls):
        pyautogui.hotkey("ctrl", "end")
        time.sleep(2)
    pyautogui.hotkey("ctrl", "home")
    time.sleep(1)

    text = copy_page()
    if len(text) < 200:
        print(f"  [!] Too little text ({len(text)} chars)")
        return []

    leads = parse_posts(text, keyword)
    seen = set()
    unique = []
    for l in leads:
        key = l["name"].lower()
        if key not in seen:
            seen.add(key)
            unique.append(l)
    return unique


def load_keywords():
    path = os.path.join(os.path.dirname(__file__), "data", "keywords.txt")
    if os.path.exists(path):
        with open(path) as f:
            return [l.strip() for l in f if l.strip() and not l.startswith("#")]
    return ["need web developer"]


def save_leads(leads):
    import csv
    from datetime import datetime
    path = os.path.join(os.path.dirname(__file__), "data", "leads.csv")
    os.makedirs(os.path.dirname(path), exist_ok=True)

    existing = set()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                url = (row.get("profile_url") or "").strip().lower()
                if url:
                    existing.add(url)

    fields = ["name", "email", "profile_url", "post_text", "source_keyword", "added_at"]
    write_header = not os.path.exists(path) or os.path.getsize(path) == 0

    added = 0
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if write_header:
            w.writeheader()
        for lead in leads:
            url = lead.get("profile_url", "").lower()
            if url in existing:
                continue
            existing.add(url)
            w.writerow({
                "name": lead["name"],
                "email": lead.get("email", ""),
                "profile_url": lead["profile_url"],
                "post_text": lead.get("post_text", ""),
                "source_keyword": lead.get("source_keyword", ""),
                "added_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            })
            added += 1
    return added


def main():
    parser = argparse.ArgumentParser(description="LinkedIn Terminal Scraper")
    parser.add_argument("keyword", nargs="?", help="Single keyword to search")
    parser.add_argument("--top", type=int, default=0, help="Scrape first N keywords")
    parser.add_argument("--scrolls", type=int, default=1, help="Number of scrolls per keyword")
    args = parser.parse_args()

    print("=" * 50)
    print("  LinkedIn Terminal Scraper")
    print("  Using real Edge browser session")
    print("=" * 50)

    win = focus_edge()
    if not win:
        print("\n[!] Edge browser not found! Open Edge with LinkedIn.")
        sys.exit(1)

    if args.keyword:
        keywords = [args.keyword]
    else:
        keywords = load_keywords()
        if args.top > 0:
            keywords = keywords[:args.top]

    print(f"\n[*] Keywords: {len(keywords)}")
    print(f"[*] Scrolls per page: {args.scrolls}\n")

    all_leads = []
    for i, kw in enumerate(keywords, 1):
        print(f"[{i}/{len(keywords)}] {kw}")
        leads = scrape_keyword(kw, scrolls=args.scrolls)
        all_leads.extend(leads)
        print(f"  -> {len(leads)} leads found")
        for l in leads[:2]:
            name = l["name"].encode("ascii", "replace").decode()
            post = l["post_text"][:50].encode("ascii", "replace").decode()
            print(f"     {name} | {post}...")
        if i < len(keywords):
            time.sleep(2)

    added = save_leads(all_leads)
    total = len(all_leads)

    print(f"\n{'=' * 50}")
    print(f"  Results: {total} found, {added} new saved")
    print(f"  View: python leads.py")
    print(f"  Web:  http://127.0.0.1:5002/leads")
    print(f"{'=' * 50}")


if __name__ == "__main__":
    main()
