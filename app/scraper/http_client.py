import httpx
from app.scraper.stealth import get_random_headers

PINCODE_COOKIE = "400076"


def create_client(timeout: int = 15) -> httpx.AsyncClient:
    headers = get_random_headers()
    cookies = {
        "i18n-prefs": "INR",
        "lc-acbin": "en_IN",
        "sp-cdn": '"L5Z9:IN"',
    }
    return httpx.AsyncClient(
        headers=headers,
        cookies=cookies,
        timeout=httpx.Timeout(timeout, connect=10),
        follow_redirects=True,
        limits=httpx.Limits(max_connections=15, max_keepalive_connections=10),
    )
