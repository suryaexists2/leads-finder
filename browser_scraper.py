"""
Scrape LinkedIn leads using the real Edge browser.
Copies page text from the browser and parses it for post data.
No DevTools, no Selenium, no API - just real browser text.
"""
import time
import re
import os
import pyautogui
import pyperclip
import pygetwindow as gw
import ctypes
import storage

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.15


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
            time.sleep(0.5)
            return w
    return None


def navigate_to(url):
    pyautogui.hotkey("alt", "d")
    time.sleep(0.3)
    pyperclip.copy(url)
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.1)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.1)
    pyautogui.press("enter")
    time.sleep(5)


def copy_page_text():
    """Click page body, select all, copy."""
    win = focus_edge()
    if not win:
        return ""
    
    cx = win.left + win.width // 2
    cy = win.top + win.height // 2
    pyautogui.click(cx, cy)
    time.sleep(0.5)
    
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.5)
    pyautogui.hotkey("ctrl", "c")
    time.sleep(1)
    
    return pyperclip.paste()


def parse_linkedin_text(text, keyword=""):
    """Parse LinkedIn content search page text into leads.
    
    The text from Ctrl+A on LinkedIn search has this structure:
    - Navigation items (Home, My Network, etc.)
    - "Feed post" marks each post boundary
    - "View [Name]'s profile" identifies the author
    - Post content follows
    - Numbers like "46" are engagement counts
    """
    leads = []
    
    # Split by "Feed post" which marks each post
    # Clean the text first
    text = text.replace("\u200b", "")  # Remove zero-width spaces
    text = text.replace("\xa0", " ")    # Non-breaking space
    
    # Split into post blocks using "Feed post" as delimiter
    # But first, find where posts start (after "Posts" section)
    posts_start = text.find("Feed post")
    if posts_start == -1:
        # Try alternate markers
        posts_start = text.find("Sort by")
    
    if posts_start == -1:
        return leads
    
    post_text = text[posts_start:]
    blocks = re.split(r'\n\s*Feed post\s*\n', post_text)
    
    for block in blocks:
        if not block.strip() or len(block.strip()) < 20:
            continue
        
        lines = [l.strip() for l in block.split("\n") if l.strip()]
        if not lines:
            continue
        
        # Find author name - usually after "View X's profile" or before engagement markers
        author = ""
        profile_url = ""
        
        # Pattern 1: "View [Name]'s profile"
        for line in lines:
            m = re.search(r"View\s+(.+?)'s?\s+profile", line)
            if m:
                author = m.group(1).strip()
                break
        
        # Pattern 2: Look for name-like lines (short, title case, not navigation)
        if not author:
            skip_words = {"home", "my network", "jobs", "messaging", "notifications",
                         "more", "for business", "try premium", "posts", "past week",
                         "sort by", "content type", "from member", "all filters",
                         "reset", "follow", "join", "feed post", "hiring",
                         "view", "profile", "repost", "comment", "like",
                         "send", "analytics", "see"}
            for line in lines:
                low = line.lower().strip()
                if (3 < len(line) < 50
                        and line[0].isupper()
                        and low not in skip_words
                        and not low.startswith("view ")
                        and not low.startswith("http")
                        and not re.match(r'^\d+$', line)
                        and not re.match(r'^\d+[dhm]\b', line)
                        and "linkedin" not in low):
                    # Check if it looks like a person name (1-4 words, capitalized)
                    words = line.split()
                    if 1 <= len(words) <= 4 and all(w[0].isupper() for w in words if len(w) > 1):
                        author = line
                        break
        
        if not author:
            continue
        
        # Find profile URL from the block
        for line in lines:
            m = re.search(r'linkedin\.com/in/([a-zA-Z0-9_-]+)', line)
            if m:
                profile_url = f"https://www.linkedin.com/in/{m.group(1)}/"
                break
        
        if not profile_url:
            # Create from name
            slug = author.lower().replace(" ", "-").replace(".", "").replace("'", "")
            slug = re.sub(r'[^a-z0-9-]', '', slug)
            profile_url = f"https://www.linkedin.com/in/{slug}/"
        
        # Skip if this is just a comment/engagement line
        if author.lower() in {"like", "comment", "repost", "send"}:
            continue
        
        # Extract post content (skip author name, engagement markers)
        post_lines = []
        for line in lines:
            low = line.lower()
            if (line != author
                    and not re.match(r'^\d+$', line)
                    and not re.match(r'^\d+[dhm]\b', line)
                    and low not in {"like", "comment", "repost", "send", "follow", "join", "connect"}
                    and "view " not in low
                    and "profile" not in low
                    and not line.startswith("linkedin.com")
                    and len(line) > 5):
                post_lines.append(line)
        
        post_text_str = " ".join(post_lines[:3])[:300]
        
        leads.append({
            "name": author,
            "profile_url": profile_url,
            "post_text": post_text_str,
            "source_keyword": keyword,
            "search_type": "browser_text",
            "country": "",
            "company": "",
            "email": "",
            "activity_id": str(hash(author + post_text_str)),
        })
    
    return leads


def scrape_keyword(keyword, date_posted="past-week", scrolls=1):
    win = focus_edge()
    if not win:
        print("  ERROR: Edge not found!")
        return []
    
    url = f"https://www.linkedin.com/search/results/content/?keywords={keyword.replace(' ', '+')}&datePosted={date_posted}&sortBy=RELEVANCE"
    print(f"  Navigating: {keyword}")
    navigate_to(url)
    
    # Wait for page to load
    time.sleep(2)
    
    # Scroll once to load more
    for s in range(scrolls):
        pyautogui.hotkey("ctrl", "end")
        time.sleep(2)
    
    # Scroll back up
    pyautogui.hotkey("ctrl", "home")
    time.sleep(1)
    
    # Copy all text
    print("  Copying page text...")
    text = copy_page_text()
    
    if not text or len(text) < 200:
        print("  WARNING: Too little text!")
        return []
    
    print(f"  Copied {len(text)} chars")
    
    # Parse
    leads = parse_linkedin_text(text, keyword)
    
    # Deduplicate within this batch
    seen = set()
    unique = []
    for l in leads:
        key = l["name"].lower()
        if key not in seen:
            seen.add(key)
            unique.append(l)
    
    print(f"  Found {len(unique)} unique leads")
    for l in unique[:3]:
        safe_name = l["name"].encode("ascii", "replace").decode()
        safe_post = l["post_text"][:50].encode("ascii", "replace").decode()
        print(f"    {safe_name} | {safe_post}...")
    
    return unique


def main():
    print("=== LinkedIn Browser Scraper (Real Session) ===\n")
    storage.init()
    
    win = focus_edge()
    if not win:
        print("ERROR: Edge not found!")
        return
    
    title = win.title.encode("ascii", "replace").decode()[:60]
    print(f"Edge: {title}\n")
    
    with open("data/keywords.txt") as f:
        keywords = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    
    # Test with first 5 keywords
    test_kws = keywords[:5]
    all_leads = []
    
    for kw in test_kws:
        print(f"--- {kw} ---")
        leads = scrape_keyword(kw)
        all_leads.extend(leads)
        print()
        time.sleep(3)
    
    if all_leads:
        added = storage.add_leads(all_leads)
        print(f"=== DONE: {added} new leads saved! ===")
    else:
        print("=== No leads found ===")
    
    # Return to feed
    navigate_to("https://www.linkedin.com/feed/")


if __name__ == "__main__":
    main()
