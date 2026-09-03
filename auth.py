"""
LinkedIn auto-login using linkedin-api (tomquirk).
Authenticates via username/password, caches li_at cookie to disk.
No manual cookie paste needed — session auto-refreshes.
"""
import os
import logging
from linkedin_api import Linkedin

logger = logging.getLogger(__name__)

COOKIE_CACHE_DIR = os.path.join(os.path.dirname(__file__), ".linkedin_cookies")


def get_client(email, password, refresh=False):
    """Get authenticated Linkedin client. Cookies cached to disk."""
    os.makedirs(COOKIE_CACHE_DIR, exist_ok=True)
    return Linkedin(
        email,
        password,
        authenticate=True,
        refresh_cookies=refresh,
        cookies_dir=COOKIE_CACHE_DIR,
    )


def get_li_at_cookie(email, password, refresh=False):
    """Authenticate and return li_at cookie string for use with curl_cffi."""
    try:
        client = get_client(email, password, refresh=refresh)
        cookies = client.client.session.cookies
        li_at = cookies.get("li_at", "")
        jsessionid = cookies.get("JSESSIONID", "")
        if not li_at:
            return None, "No li_at cookie obtained. Check credentials."
        cookie_str = f"li_at={li_at}; JSESSIONID={jsessionid}"
        return cookie_str, "OK"
    except Exception as e:
        return None, str(e)[:200]


def test_auth(email, password):
    """Test login and return status message."""
    try:
        cookie_str, msg = get_li_at_cookie(email, password)
        if cookie_str:
            return True, f"Login successful. {msg}"
        return False, msg
    except Exception as e:
        return False, str(e)[:200]
