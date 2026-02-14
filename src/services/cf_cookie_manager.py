"""Cloudflare cookie manager module

Uses Playwright to solve CF managed challenges and caches the resulting
cookies (cf_clearance, etc.) for reuse by curl_cffi requests.
"""
import asyncio
import time
from typing import Optional, Dict, Tuple

try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


# Default User-Agent that matches what Playwright/Chromium sends
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


class CfCookieManager:
    """Manages Cloudflare clearance cookies obtained via Playwright.

    Cookies are cached per proxy URL and refreshed when they expire
    (default TTL: 10 minutes).
    """

    # Cache TTL in seconds
    CACHE_TTL = 600  # 10 minutes

    def __init__(self):
        # Cache structure: { proxy_key: (cookies_dict, user_agent, timestamp) }
        self._cache: Dict[str, Tuple[Dict[str, str], str, float]] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

    def _proxy_key(self, proxy_url: Optional[str]) -> str:
        return proxy_url or "__no_proxy__"

    async def _get_lock(self, key: str) -> asyncio.Lock:
        async with self._global_lock:
            if key not in self._locks:
                self._locks[key] = asyncio.Lock()
            return self._locks[key]

    def _is_valid(self, key: str) -> bool:
        if key not in self._cache:
            return False
        _, _, ts = self._cache[key]
        return (time.time() - ts) < self.CACHE_TTL

    async def get_cookies(
        self, proxy_url: Optional[str] = None, force_refresh: bool = False
    ) -> Optional[Tuple[Dict[str, str], str]]:
        """Get CF clearance cookies for the given proxy.

        Returns:
            Tuple of (cookies_dict, user_agent) or None on failure.
            cookies_dict maps cookie name -> cookie value.
        """
        if not PLAYWRIGHT_AVAILABLE:
            print("[CF Cookie] Playwright not available, skipping CF cookie fetch")
            return None

        key = self._proxy_key(proxy_url)

        # Return cached if valid
        if not force_refresh and self._is_valid(key):
            cookies, ua, _ = self._cache[key]
            print(f"[CF Cookie] Using cached cookies (proxy={key})")
            return cookies, ua

        # Acquire per-proxy lock to avoid concurrent browser launches
        lock = await self._get_lock(key)
        async with lock:
            # Double-check after acquiring lock
            if not force_refresh and self._is_valid(key):
                cookies, ua, _ = self._cache[key]
                return cookies, ua

            print(f"[CF Cookie] Fetching new CF cookies via Playwright (proxy={key})...")
            result = await self._fetch_cookies_via_browser(proxy_url)
            if result:
                cookies, ua = result
                self._cache[key] = (cookies, ua, time.time())
                print(f"[CF Cookie] Cached {len(cookies)} cookies, TTL={self.CACHE_TTL}s")
                return cookies, ua
            else:
                print(f"[CF Cookie] Failed to obtain CF cookies")
                return None

    async def _fetch_cookies_via_browser(
        self, proxy_url: Optional[str] = None
    ) -> Optional[Tuple[Dict[str, str], str]]:
        """Launch Playwright, navigate to sora.chatgpt.com, wait for CF
        challenge to resolve, and extract cookies."""
        pw = None
        browser = None
        try:
            pw = await async_playwright().start()

            launch_args = {
                "headless": True,
                "args": [
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            }
            if proxy_url:
                launch_args["proxy"] = {"server": proxy_url}

            browser = await pw.chromium.launch(**launch_args)
            context = await browser.new_context(
                user_agent=_DEFAULT_UA,
                viewport={"width": 1280, "height": 720},
            )
            page = await context.new_page()

            print("[CF Cookie] Navigating to sora.chatgpt.com ...")
            # Navigate and let CF challenge run
            try:
                await page.goto(
                    "https://sora.chatgpt.com/",
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
            except Exception as nav_err:
                # Even if navigation "fails" (e.g. timeout), CF may have
                # set cookies already. Continue to check.
                print(f"[CF Cookie] Navigation note: {nav_err}")

            # Wait for CF challenge to complete.
            # CF typically redirects or sets cf_clearance after the JS challenge.
            # We poll for the cf_clearance cookie.
            max_wait = 60  # seconds
            poll_interval = 2  # seconds
            waited = 0
            cf_clearance_found = False

            while waited < max_wait:
                cookies_list = await context.cookies("https://sora.chatgpt.com")
                for c in cookies_list:
                    if c["name"] == "cf_clearance":
                        cf_clearance_found = True
                        break
                if cf_clearance_found:
                    break

                # Check if page has loaded past CF challenge
                try:
                    title = await page.title()
                    # CF challenge page has title "Just a moment..."
                    if title and "just a moment" not in title.lower():
                        # Page loaded past CF, cookies should be set
                        cf_clearance_found = True
                        break
                except Exception:
                    pass

                await asyncio.sleep(poll_interval)
                waited += poll_interval

            # Extract all cookies for the domain
            all_cookies = await context.cookies("https://sora.chatgpt.com")
            cookies_dict = {}
            for c in all_cookies:
                cookies_dict[c["name"]] = c["value"]

            if not cf_clearance_found:
                print(f"[CF Cookie] Warning: cf_clearance not found after {max_wait}s wait")
                # Still return whatever cookies we got - they might work
                if not cookies_dict:
                    return None

            print(f"[CF Cookie] Obtained cookies: {list(cookies_dict.keys())}")

            await context.close()
            await browser.close()
            await pw.stop()

            return cookies_dict, _DEFAULT_UA

        except Exception as e:
            print(f"[CF Cookie] Error: {e}")
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass
            if pw:
                try:
                    await pw.stop()
                except Exception:
                    pass
            return None

    def invalidate(self, proxy_url: Optional[str] = None):
        """Invalidate cached cookies for a specific proxy."""
        key = self._proxy_key(proxy_url)
        if key in self._cache:
            del self._cache[key]
            print(f"[CF Cookie] Cache invalidated for proxy={key}")

    def invalidate_all(self):
        """Invalidate all cached cookies."""
        self._cache.clear()
        print("[CF Cookie] All caches invalidated")


# Global singleton instance
cf_cookie_manager = CfCookieManager()
