import random
import time
import threading
from datetime import datetime
from typing import Generator, Dict, Any, Optional

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from DrissionPage import ChromiumPage, ChromiumOptions

from loguru import logger

from crawler.exceptions import CaptchaDetectedException, EndOfResultsException, LoginRequiredException

import re
import json
import os
import tempfile
from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_exception, retry_if_exception_type


class XHSCrawler:
    def __init__(
        self,
        headless: bool = False,
        user_data_path: Optional[str] = None,
        port: int = 9222,
        cookie_path: Optional[str] = None,
        global_cookie_path: Optional[str] = None,
        proxy: Optional[str] = None,
    ):
        """
        Initialize the XHS Crawler. / 初始化小红书爬虫。

        Args:
            headless: Whether to run in headless mode. / 是否以无头模式运行。
            user_data_path: Used for session persistence. / 用于会话持久化的用户数据目录路径。
            port: Local debugging port. / 本地调试端口。
            cookie_path: Path to save/load cookies (optional). / 保存/加载 Cookie 的路径（可选；默认不落盘）。
            global_cookie_path: Path to sync cookies to (master record). / 同步 Cookie 到（主记录）的路径。
            proxy: Proxy URL (e.g. http://user:pass@ip:port). / 代理地址。
        """
        # Lazy import to avoid startup hang
        from DrissionPage import ChromiumPage, ChromiumOptions

        # DrissionPage/ChromiumPage is not thread-safe; record the owner thread so we can
        # avoid cross-thread browser calls (e.g. saving cookies) when a stop signal is sent
        # from the API thread.
        self._owner_thread_id = threading.get_ident()
        self.headless = bool(headless)
        # When we detect "login required", avoid overwriting potentially valid cookies on disk
        # with guest/invalid cookies captured from the blocked session.
        self._suppress_cookie_save = False
        self.cookie_path = cookie_path
        self.global_cookie_path = global_cookie_path
        def _build_options(user_data_path_override: Optional[str]) -> ChromiumOptions:
            co = ChromiumOptions(read_file=False)
            co.set_local_port(port)
            if headless:
                co.headless()
                # DrissionPage error hints suggest '--headless=new' in headless environments.
                # Keep it explicit for better cross-platform stability.
                co.set_argument("--headless=new")

            # Proxy
            if proxy:
                co.set_argument(f"--proxy-server={proxy}")

            # Anti-detection settings / "Stealth" | 反检测设置 / “隐身”
            # '--no-sandbox' is mainly needed for Linux containers; avoid forcing it on other platforms.
            if os.name != "nt":
                try:
                    import platform as _platform

                    if _platform.system() == "Linux":
                        co.set_argument("--no-sandbox")
                except Exception:
                    # If platform detection fails, keep existing behavior in Linux-like envs.
                    co.set_argument("--no-sandbox")
            co.set_argument("--disable-gpu")
            # Mute audio to avoid noise | 静音以避免噪音
            co.mute(True)
            co.ignore_certificate_errors()

            if user_data_path_override:
                co.set_user_data_path(user_data_path_override)
            return co

        # Initialize page object | 初始化页面对象
        try:
            co = _build_options(user_data_path)
            self.page = ChromiumPage(addr_or_opts=co)
        except Exception as e:
            # Common cause: the profile dir is locked by a zombie Chromium/Chrome.
            # Retry once with a fresh temp profile so tasks don't get stuck in a loop.
            if not user_data_path:
                raise
            tmp_root = os.path.abspath(os.path.join("profiles", "_tmp_sessions"))
            os.makedirs(tmp_root, exist_ok=True)
            tmp_profile = tempfile.mkdtemp(prefix=f"task_port_{port}_", dir=tmp_root)
            logger.warning(
                f"Browser start/connect failed with profile '{user_data_path}', retrying with temp profile '{tmp_profile}'. Error: {e}"
            )
            co = _build_options(tmp_profile)
            self.page = ChromiumPage(addr_or_opts=co)
        
        # Load cookies if available | 如果可用，加载 Cookie
        loaded_cookies = self._load_cookies()
        if not loaded_cookies:
            logger.info("No cookies loaded (cookie persistence disabled or missing files); crawler may require manual login.")
        
        # Initialize captcha solver | 初始化验证码识别器

        
        # Start listening for search notes API | 开始监听搜索笔记 API
        # Target: /api/sns/web/v1/search/notes | 目标：/api/sns/web/v1/search/notes
        # Keep scope narrow by default to reduce captured packet volume (CPU/memory pressure).
        # Update: XHS moved search API to fe.xiaohongshu.com, so we need a broader scope or specific path.
        listen_scope = os.getenv("XHS_LISTEN_SCOPE", "xiaohongshu.com")
        self.page.listen.start(listen_scope)
        logger.info(f"XHSCrawler initialized. Listening for '{listen_scope}'.")
        
        self._stop_event = False
        self.current_page = 1
        # Scroll tuning
        self._scroll_base = (600, 1400)  # default scroll distance range in px
        
        # Human-like pace management removed for simple version
        # self._pace_factor = 1.0



    def _sleep_with_jitter(self, base: float, jitter_ratio: float = 0.4) -> float:
        """
        Sleep with random jitter for basic politeness.
        No complex pace/mood logic in the base version.
        """
        if base <= 0:
            return 0.0
        
        if os.getenv("XHS_DISABLE_WAIT", "0") == "1":
            return 0.0
        
        # Simple random jitter
        jitter = base * jitter_ratio
        min_sleep = max(0.1, base - jitter)
        max_sleep = base + jitter
        
        actual_sleep = random.uniform(min_sleep, max_sleep)
        time.sleep(actual_sleep)
        return actual_sleep

    def _random_scroll_pixels(self, base_range=None, variance: float = 0.35) -> int:
        """
        Generate a scroll distance with jitter. Defaults to the crawler's base range.
        """
        min_base, max_base = base_range if base_range else self._scroll_base
        raw = random.uniform(min_base, max_base)
        jitter = raw * random.uniform(-variance, variance)
        distance = max(120, int(raw + jitter))
        return distance

    def _load_cookies(self):
        """Load cookies from local/global file. / 从本地或全局文件加载 Cookie。"""
        if not self.cookie_path and not self.global_cookie_path:
            return False
        # Try task-specific cookies first, then fall back to global and backup
        candidates = []
        for path in [self.cookie_path, self.global_cookie_path, "cookies_backup.json"]:
            if path and path not in candidates:
                candidates.append(path)

        for path in candidates:
            if not path or not os.path.exists(path):
                continue
            try:
                if os.path.getsize(path) < 3:
                    logger.warning(f"Cookie file is empty, skip: {path}")
                    continue

                with open(path, 'r', encoding='utf-8') as f:
                    cookies = json.load(f)

                if not cookies:
                    logger.warning(f"No cookies found in {path}, try next.")
                    continue

                self.page.set.cookies(cookies)
                logger.info(f"Loaded cookies from {path}")

                # Sync back to task cookie file if we loaded from global/backup
                if path != self.cookie_path and self.cookie_path:
                    try:
                        with open(self.cookie_path, 'w', encoding='utf-8') as f:
                            json.dump(cookies, f, indent=2)
                    except Exception as sync_err:
                        logger.warning(f"Failed to sync cookies to {self.cookie_path}: {sync_err}")
                return True
            except json.JSONDecodeError as e:
                logger.warning(f"Cookie file {path} is invalid JSON: {e}")
            except Exception as e:
                logger.error(f"Failed to load cookies from {path}: {e}")
        return False

    def _save_cookies(self):
        """Save current cookies to local file. / 保存当前 Cookie 到本地文件。"""
        if not self.cookie_path:
            return
        if getattr(self, "_suppress_cookie_save", False):
            logger.warning(
                "Skipping cookie save because login is required (prevent overwriting valid cookies)."
            )
            return
        try:
            # DrissionPage 4.x: cookies() method without args often returns list of dicts
            # or it might be a property. We try to handle both.
            cookies = self.page.cookies
            if callable(cookies):
                cookies = cookies()
            elif hasattr(cookies, 'as_dict'): # If it's a Cookies object
                cookies = cookies.as_dict()
            
            # If it's already a list (which is common for simply accessing .cookies in some versions)
            # just ensuring it's serializable

            if not cookies:
                logger.warning("No cookies to save (empty list). Skipping save to avoid overwriting valid cookies.")
                return
            
            # Atomic write using temporary file to prevent corruption
            tmp_path = self.cookie_path + ".tmp"
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(cookies, f, indent=2)
            os.replace(tmp_path, self.cookie_path)
            logger.info(f"Saved cookies to {self.cookie_path}")
            
            # Sync to global path if set
            if self.global_cookie_path:
                try:
                    tmp_global = self.global_cookie_path + ".tmp"
                    with open(tmp_global, 'w', encoding='utf-8') as f:
                        json.dump(cookies, f, indent=2)
                    os.replace(tmp_global, self.global_cookie_path)
                    logger.info(f"Synced cookies to global path: {self.global_cookie_path}")
                except Exception as ex:
                    logger.warning(f"Failed to sync cookies to global path: {ex}")

        except Exception as e:
            logger.error(f"Failed to save cookies: {e}")

    def check_login_status(self):
        """
        Proactively check if logged in. If not, trigger login flow.
        主动检查是否登录。如果未登录，触发登录流程。
        Also detects "Security Restriction" pages. / 同时检测"安全限制"页面。
        """
        try:
            # Avoid navigating away if we are already on a content page
            current_url = self.page.url
            if "xiaohongshu.com" not in current_url:
                 self.page.get("https://www.xiaohongshu.com")
                 self._sleep_with_jitter(2, jitter_ratio=0.3)
            
            # Check for Security Restriction page | 检查安全限制页面
            if 'website-login/error' in current_url or '安全限制' in (self.page.title or ''):
                logger.warning("Security Restriction page detected! Session may be invalid.")
                logger.warning("安全限制页面检测到！会话可能已失效。")
                # Clear potentially bad cookies and wait for fresh login
                self.check_and_wait_for_login()
                return
            
            # 1. Primary check: Strict identity (User elements or id_token)
            # Check current page first without refresh
            if self._is_logged_in_strict():
                logger.info("Login status validated (Identity found).")
                self._save_cookies()
                return

            # Only refresh/navigate if quick check fails and we are not on a specific content page
            # (to avoid disrupting flow)
            if "/explore" not in current_url and "/user/profile" not in current_url and "/discovery/" not in current_url:
                 self.page.get("https://www.xiaohongshu.com")
                 self._sleep_with_jitter(2)
                 if self._is_logged_in_strict():
                    return

            # 2. Secondary check: Login button/modal presence
            login_prompt = (
                self.page.ele(".login-container", timeout=0.2)
                or self.page.ele(".login-modal", timeout=0.2)
                or self.page.ele(".login-btn", timeout=0.2)
                or self.page.ele("text=登录", timeout=0.2)
                or self.page.ele("text=登录注册", timeout=0.2)
                or self.page.ele(".side-bar-component .login-btn", timeout=0.2)
            )
            if login_prompt:
                logger.warning("Not logged in (Login prompt/button detected).")
                self.check_and_wait_for_login()
            else:
                logger.warning("Ambiguous login state: Identity missing but no login button found.")
                # Re-apply cookies once before forcing manual login
                reloaded = self._load_cookies()
                if reloaded:
                    logger.info("Reloaded cookies after ambiguous state, refreshing page...")
                    self.page.refresh()
                    self._sleep_with_jitter(2, jitter_ratio=0.3)
                    if self._is_logged_in_strict():
                        logger.success("Login validated after refresh with cookies.")
                        self._save_cookies()
                        return
                
                logger.warning("Assuming not logged in after ambiguous check. Triggering login flow.")
                self.check_and_wait_for_login()
        except Exception as e:
            logger.error(f"Error checking login status: {e}")

    def _detect_security_restriction(self) -> Optional[Dict[str, str]]:
        """
        Detect security restriction pages (e.g. 访问频次异常/安全限制).
        """
        try:
            current_url = self.page.url or ""
            title = self.page.title or ""
            html = self.page.html or ""
        except Exception:
            return None

        text = f"{title}\n{html}"
        if (
            "website-login/error" in current_url
            or "安全限制" in text
            or "访问频次异常" in text
            or "请勿频繁操作" in text
        ):
            code = "300013" if "300013" in text else ""
            message = "访问频次异常" if "访问频次异常" in text else "安全限制"
            if "请勿频繁操作" in text and message == "安全限制":
                message = "请勿频繁操作"
            return {"code": code, "message": message}

        return None

    def stop(self, save_cookies: bool = True):
        """Signal the crawler to stop (thread-safe best effort)."""
        self._stop_event = True
        logger.info("Crawler stop signal received.")
        
        if save_cookies:
            owner = getattr(self, "_owner_thread_id", None)
            if owner is not None and threading.get_ident() != owner:
                logger.debug("Stop called from non-owner thread; skipping cookie save to avoid blocking.")
            else:
                try:
                    self._save_cookies()
                except Exception as e:
                    logger.warning(f"Failed to save cookies on stop: {e}")
        
        # Close the browser instance
        try:
            if self.page:
                # Try to find a quit method on the page or its browser object
                if hasattr(self.page, 'quit'):
                    try:
                        self.page.quit()
                        logger.info("Browser instance closed (quit).")
                        return
                    except AttributeError:
                        # Sometimes hasattr returns True but the method is missing/not callable due to dynamic proxies
                        pass
                    except Exception as e:
                         logger.warning(f"Error calling page.quit(): {e}")

                if hasattr(self.page, 'browser') and hasattr(self.page.browser, 'quit'):
                    try:
                        self.page.browser.quit()
                        logger.info("Browser instance closed (browser.quit).")
                        return
                    except Exception as e:
                        logger.warning(f"Error calling page.browser.quit(): {e}")
                
                # Fallback: try closing the tab if strict quit failed
                if hasattr(self.page, 'close'):
                    try:
                        self.page.close()
                        logger.info("Browser tab closed (fallback).")
                    except Exception as e:
                         logger.warning(f"Error calling page.close(): {e}")
                else:
                    logger.warning("Browser object has neither quit nor close methods.")
        except Exception as e:
            logger.warning(f"Failed to close browser on stop: {e}")

    def _is_logged_in_strict(self) -> bool:
        """
        Check if we are strictly logged in by verifying user identity elements.
        Returns True if we see user avatar/name.
        """
        # More robust check with slight wait if needed?
        # But we don't want to slow down normal browsing.
        selectors = [
            '#user-avatar', 
            '.user-name', 
            '.user-side-content', 
            '.avatar-wrapper', 
            '.author-wrapper',
            '.user-nickname',
            '.nickname',
            'css:[class*="user-name"]',
            'css:[class*="avatar"]'
        ]
        
        # 1. POSITIVE CHECK FIRST: If user identity elements are found, we ARE logged in.
        # This takes priority over negative checks because pages might have residual login elements.
        has_user_identity = any(self.page.ele(sel, timeout=0.2) for sel in selectors)
        
        if has_user_identity:
            logger.debug("Strict check: User identity elements found. Login confirmed.")
            return True

        # 2. Negative Check: If "Login" button/modal is explicitly visible AND no identity found.
        # We need to ensure the element is actually visible, not just in DOM.
        login_btn = self.page.ele('.login-btn')
        login_text = self.page.ele('text:登录')
        login_container = self.page.ele('.login-container')

        if (login_btn and login_btn.states.is_displayed) or \
           (login_text and login_text.states.is_displayed) or \
           (login_container and login_container.states.is_displayed):
             logger.debug("Strict check: No identity found AND 'Login' button visible. Marking as NOT logged in.")
             return False
            
        # Fallback: Check for valid session cookie (id_token)
        try:
            cookies = self.page.cookies(all_domains=True)
            candidate_cookies = []
            if isinstance(cookies, list):
                candidate_cookies = cookies
            elif hasattr(cookies, 'as_dict'):
                # Handle Cookies object
                d = cookies.as_dict() 
                # d might be dict of key-value? or list?
                # Usually .as_dict() returns {name: value} which loses domain info but works for existence check
                if isinstance(d, dict):
                     for k, v in d.items():
                         candidate_cookies.append({'name': k, 'value': v})
            
            found = False
            for c in candidate_cookies:
                name = c.get('name')
                if name == 'id_token':
                     val = c.get('value')
                     if val:
                         found = True
                         logger.info(f"Login validated via id_token cookie (len={len(str(val))}).")
                         break
            if found:
                return True
            else:
                # Debug logging - print cookie names
                names = [c.get('name') for c in candidate_cookies]
                logger.warning(f"Strict check failed: DOM missing and id_token not found in cookies (count={len(names)}): {names}")

        except Exception as e:
            logger.error(f"Error checking cookies in strict check: {e}")
            pass
            
        return False

    def check_and_wait_for_login(self):
        """
        Check if login is detected and wait for user to complete it.
        Enforces strict login presence (user avatar/identity) to prevent guest crawling.
        Optimization: Checks strict login status FIRST to avoid waiting for UI elements when already logged in.
        """
        try:
            # 1. OPTIMIZATION: Start with strict check. If logged in, we only check for blocking modals.
            is_logged_in = self._is_logged_in_strict()
            
            # Quick check for blocking modal (fast timeout)
            login_modal = self.page.ele(".login-container", timeout=0.2) or self.page.ele(".login-modal", timeout=0.2)
            
            if is_logged_in and not login_modal:
                return True

            # 2. Close buttons for ads/popups (only if we suspect interference)
            close_btn = self.page.ele('.close-icon', timeout=0.2) or \
                        self.page.ele('css:[class*="close-circle"]', timeout=0.2) or \
                        self.page.ele('.icon-close', timeout=0.2)
            if close_btn:
                try: 
                    close_btn.click(by_js=True)
                    time.sleep(0.5)
                except: pass

            # 3. Login Modal / Button Check (if not logged in OR modal found)
            login_btn = None
            if not is_logged_in:
                login_btn = (
                    self.page.ele(".login-btn", timeout=0.2)
                    or self.page.ele("text=登录", timeout=0.2)
                    or self.page.ele("text=登录注册", timeout=0.2)
                    or self.page.ele(".side-bar-component .login-btn", timeout=0.2)
                    or self.page.ele('button:has-text("登录")', timeout=0.2)
                )

            # 2b. If only button found, try clicking to confirm modal
            if (not login_modal) and login_btn:
                try:
                    logger.info("Found login button, clicking to trigger modal...")
                    login_btn.click()
                    self._sleep_with_jitter(0.8, jitter_ratio=0.4)
                except Exception:
                    pass
                login_modal = self.page.ele(".login-container", timeout=0.3) or self.page.ele(".login-modal", timeout=0.3)

            # 4. Strict Verification (if not already confirmed)
            if not (login_modal or login_btn):
                 # We already checked strictly at the start, so if we are here and not logged in, it's failed.
                 if not is_logged_in:
                     # Double check just in case
                     if not self._is_logged_in_strict():
                         logger.warning("No login modal found, but user identity missing. Assuming NOT logged in.")
                         # We can't force login if no button found, but we should raise exception or return
                         # For now, let's just proceed to wait loop if we think we might find something eventually?
                         # However, verified logic usually relies on waiting here.
                         pass
                     else:
                         return True# All good
                 
                 # Not logged in, and no modal. Suspect Guest Mode or Page Loading.
                 current_url = self.page.url or ""
                 # Only enforce strictness on main scraping pages where user identity MUST be present
                 if "search_result" in current_url or "explore" in current_url:
                     # Retry once after a short delay to handle network lag/rendering
                     time.sleep(1.5)
                     if self._is_logged_in_strict():
                         return 
                     
                     logger.warning("No user identity found on main page (after wait). Triggering login flow.")
                     # Try to force login modal
                     force_btn = self.page.ele("text=登录", timeout=1.0)
                     if force_btn:
                         force_btn.click()
                         login_modal = self.page.ele(".login-container")
                     else:
                         # Final attempt: Refresh page and check again
                         logger.warning("Strict Check failed. Refreshing page to ensure it's not a loading glitch...")
                         self.page.refresh()
                         self._sleep_with_jitter(3, jitter_ratio=0.3)
                         if self._is_logged_in_strict():
                             return

                         # Check again for login button
                         force_btn = self.page.ele("text=登录", timeout=1.0)
                         if force_btn:
                             force_btn.click()
                             login_modal = self.page.ele(".login-container")
                         else:
                             title = self.page.title or "No Title"
                             url = self.page.url or "No URL"
                             html_snippet = (self.page.html or "")[:500]
                             logger.error(f"Strict Login Check Failed. Page Title: {title}, URL: {url}. Snippet: {html_snippet}")
                             logger.error("No user avatar and no login button found (Guest/Bot Block detected).")
                             raise LoginRequiredException("Strict Login Check Failed: Not logged in.")

            # 4. Handle Login Requirement
            if login_modal or login_btn:
                # One last check: maybe a login button is visible but we are actually logged in?
                if self._is_logged_in_strict():
                    logger.info("Login button found, but identity is already verified. Skipping login flow.")
                    return

                logger.warning("Login required detected!")
                self._suppress_cookie_save = True

                # A. Try Reloading (Cookie Refresh)
                if self.global_cookie_path:
                    logger.info("Re-applying cookies from disk to skip login modal...")
                    if self._load_cookies():
                        self.page.refresh()
                        self._sleep_with_jitter(3, jitter_ratio=0.3)
                        # Re-check strict identity
                        if self._is_logged_in_strict():
                            logger.success("Login restored after reloading cookies.")
                            self._suppress_cookie_save = False
                            self._save_cookies()
                            return
                        else:
                             # Reload didn't help, modal might be gone but still guest
                             logger.warning("Cookie reload refreshed page, but still not strictly logged in.")

                # B. Try Closing Modal (maybe just an ad?)
                if close_btn:
                    logger.info("Attempting to close modal...")
                    try:
                        close_btn.click()
                        self._sleep_with_jitter(1, jitter_ratio=0.4)
                        if self._is_logged_in_strict():
                            logger.success("Login validated after closing modal.")
                            return
                    except Exception as e:
                        pass
                
                # C. Manual Wait
                logger.warning("The crawler is paused. Please login manually or scan QR code.")
                try:
                    default_wait = 0 if self.headless else 300
                    wait_seconds = int(os.getenv("XHS_LOGIN_WAIT_SECONDS", str(default_wait)))
                except Exception:
                    wait_seconds = 0
                if wait_seconds <= 0:
                    raise LoginRequiredException(
                        "Login required. Please login via Dashboard or wait timeout."
                    )
                
                # Wait until blocked state resolves AND we are strictly logged in
                wait_start = time.time()
                while True:
                    if self._stop_event: return

                    # Check strict login status
                    if self._is_logged_in_strict():
                        logger.success("Manual login detected (identity verified).")
                        break
                    
                    if time.time() - wait_start > wait_seconds:
                        raise LoginRequiredException("Login Wait Timeout")
                    
                    self._sleep_with_jitter(2, jitter_ratio=0.25)
                
                self._suppress_cookie_save = False
                self._save_cookies() 
                self._sleep_with_jitter(3, jitter_ratio=0.3)

        except Exception as e:
            if isinstance(e, LoginRequiredException):
                raise
            if "Timeout" in str(e):
                raise
            logger.debug(f"Login check non-fatal error: {e}")

    def _hover_element(self, ele):
        """Simulate human hover over an element to trigger menus."""
        try:
            # Prefer native hover if available
            if hasattr(ele, 'hover'):
                ele.hover()
            else:
                # Fallback: dispatch mouseover/mouseenter events
                ele.run_js('this.dispatchEvent(new Event("mouseover", {bubbles: true}));'
                           'this.dispatchEvent(new Event("mouseenter", {bubbles: true}));')
            self._sleep_with_jitter(0.5, jitter_ratio=0.6)
        except Exception as e:
            logger.debug(f"Hover simulation failed: {e}")

    def _time_of_day_multiplier(self) -> float:
        """
        Slow down during lunch/dinner/late night to mimic human usage.
        """
        now = datetime.now()
        hour_fraction = now.hour + now.minute / 60

        # Lunch / dinner scrolling
        if 12 <= hour_fraction < 13.5 or 18 <= hour_fraction < 20:
            return random.uniform(1.5, 2.3)

        # Late night cautious browsing
        if hour_fraction >= 22:
            return random.uniform(1.3, 1.9)

        return random.uniform(0.9, 1.3)

    def _humanized_start_delay(self):
        """
        Add a small warm-up delay before heavy crawling to reduce pattern spikes.
        """
        wait_seconds = random.uniform(1.5, 4.0) * self._time_of_day_multiplier()
        logger.info(f"Humanized warm-up for {wait_seconds:.1f}s before crawling...")
        time.sleep(wait_seconds)

    def _humanized_page_pause(self, page: int):
        """
        Sleep between pages with human-like jitter and occasional long rests.
        """
        if self._stop_event:
            return

        multiplier = self._time_of_day_multiplier()
        pace = getattr(self, "_pace_factor", 1.0)
        
        # Lower base range but with higher potential jitter
        base = random.uniform(1.8, 4.5) * multiplier * pace

        # Occasional longer rests every few pages to mimic breaks
        # Humans don't browse forever at a constant speed
        if page % random.randint(5, 9) == 0:
            extra = random.uniform(25, 80)
            logger.info(f"Mimicking a longer rest/break: adding {extra:.1f}s")
            base += extra

        logger.info(f"Humanized pause {base:.1f}s before next page (x{multiplier:.2f}).")

        waited = 0.0
        while waited < base and not self._stop_event:
            chunk = min(5, base - waited)
            delay = self._sleep_with_jitter(chunk, jitter_ratio=0.2)
            waited += delay

    def _simulate_micro_actions(self):
        """
        Light scrolling/click-like motions to look less robotic.
        """
        try:
            if random.random() < 0.4:
                dist = self._random_scroll_pixels((320, 920), variance=0.4)
                self.page.scroll.down(dist)
                self._sleep_with_jitter(0.6, jitter_ratio=0.5)

            if random.random() < 0.15:
                dist = self._random_scroll_pixels((120, 460), variance=0.45)
                self.page.scroll.up(dist)
                self._sleep_with_jitter(0.35, jitter_ratio=0.5)
        except Exception as e:
            logger.debug(f"Micro action failed: {e}")

    def _apply_filters(self, note_type: int, time_range: int, sort_type: str = "general", search_scope: int = 0, location_distance: int = 0):
        """Ensure search filters on the page match requested note type and time range."""
        if self._stop_event:
            return
        try:
            self._check_captcha()
            # Note type tab selection
            if note_type in (1, 2):
                target_text = "视频" if note_type == 1 else "图文"
                tab = self.page.ele(f'text:{target_text}', timeout=1)
                if not tab:
                    try:
                        candidates = self.page.eles('.channel') or []
                        for cand in candidates:
                            try:
                                if (cand.text or "").strip() == target_text:
                                    tab = cand
                                    break
                            except Exception:
                                continue
                    except Exception:
                        pass
                if tab and tab.states.is_displayed:
                    tab.click()
                    logger.info(f"Applied note type filter: {target_text}")
                    self._sleep_with_jitter(1, jitter_ratio=0.3)
                else:
                    logger.warning(f"Note type tab '{target_text}' not found or not visible.")

            # Time range filter selection
            time_labels = {
                1: ["一天内", "24小时内", "近24小时"],
                2: ["一周内", "近7天", "近1周"],
                4: ["半年内", "近6个月", "近半年"], # Note: 3 (One Month) is deprecated/removed
            }
            if time_range and time_range > 0:
                labels = time_labels.get(time_range, [])
                applied = False
                for text in labels:
                    opt = self.page.ele(f'text:{text}', timeout=1)
                    if opt and opt.states.is_displayed:
                        opt.click()
                        logger.info(f"Applied time filter: {text}")
                        applied = True
                        break

                if not applied and labels:
                    filter_btn = self.page.ele('.graphic-filter') or \
                                 self.page.ele('text:筛选') or \
                                 self.page.ele('.filter-btn') or \
                                 self.page.ele('.filter-box')
                    if filter_btn:
                        self._hover_element(filter_btn)
                        self._sleep_with_jitter(1, jitter_ratio=0.3)
                        for text in labels:
                            opt = self.page.wait.ele_displayed(f'text:{text}', timeout=2)
                            if opt:
                                opt.click()
                                logger.info(f"Applied time filter via dropdown: {text}")
                                applied = True
                                break

                if applied:
                    self._sleep_with_jitter(1, jitter_ratio=0.3)
                else:
                    if labels:
                        logger.warning(f"Time filter options {labels} not found; proceeding without UI filter.")

            # Sort type selection
            sort_map = {
                "general": ["综合", "综合推荐"],
                "popularity_desc": ["最热", "最多点赞", "热度"],
                "time_desc": ["最新", "最新发布"],
                "comment_desc": ["最多评论", "评论最多", "热议"]
            }
            if sort_type and sort_type in sort_map:
                labels = sort_map[sort_type]
                applied_sort = False
                
                # 1. Try finding visible sort buttons directly (common for top-level tabs)
                for text in labels:
                    tab = self.page.ele(f'text:{text}', timeout=0.5)
                    if tab and tab.states.is_displayed:
                        # Check if already active? (Usually hard to tell class without specific selector, but clicking safe)
                        tab.click()
                        logger.info(f"Applied sort filter: {text}")
                        applied_sort = True
                        break
                
                # 2. If not found, try the "Filter" menu (common for "Most Comments" hiding inside)
                if not applied_sort:
                    filter_btn = self.page.ele('.graphic-filter') or \
                                 self.page.ele('text:筛选') or \
                                 self.page.ele('.filter-btn') or \
                                 self.page.ele('.filter-box')
                    
                    if filter_btn:
                        # Hover or click to expand
                        self._hover_element(filter_btn)
                        # Sometimes click is needed
                        # filter_btn.click() 
                        # We try to see if options appear after hover
                        
                        found_in_menu = False
                        for text in labels:
                            opt = self.page.ele(f'text:{text}', timeout=1)
                            if opt and opt.states.is_displayed:
                                opt.click()
                                logger.info(f"Applied sort filter via menu: {text}")
                                found_in_menu = True
                                applied_sort = True
                                break
                        
                        if not found_in_menu:
                            # Try clicking the filter button to ensure it's open
                            filter_btn.click()
                            self._sleep_with_jitter(1, jitter_ratio=0.3)
                            for text in labels:
                                opt = self.page.ele(f'text:{text}', timeout=1)
                                if opt and opt.states.is_displayed:
                                    opt.click()
                                    logger.info(f"Applied sort filter via menu (after click): {text}")
                                    applied_sort = True
                                    break

            if applied_sort:
                 self._sleep_with_jitter(1, jitter_ratio=0.3)

            # Search Scope (0:All, 1:Viewed, 2:Not Viewed, 3:Followed)
            scope_labels = {
                1: ["已看过"],
                2: ["未看过"],
                3: ["已关注"],
            }
            if search_scope and search_scope > 0:
                 labels = scope_labels.get(search_scope, [])
                 applied_scope = False
                 # Try finding visible buttons first
                 for text in labels:
                     opt = self.page.ele(f'text:{text}', timeout=0.5)
                     if opt and opt.states.is_displayed:
                         opt.click()
                         logger.info(f"Applied search scope: {text}")
                         applied_scope = True
                         break
                 
                 # Look in filter menu if not found
                 if not applied_scope and labels:
                     filter_btn = self.page.ele('.graphic-filter') or \
                                  self.page.ele('text:筛选') or \
                                  self.page.ele('.filter-btn') or \
                                  self.page.ele('.filter-box')
                     if filter_btn:
                         self._hover_element(filter_btn)
                         self._sleep_with_jitter(1, jitter_ratio=0.3)
                         for text in labels:
                             opt = self.page.wait.ele_displayed(f'text:{text}', timeout=1)
                             if opt:
                                 opt.click()
                                 logger.info(f"Applied search scope via menu: {text}")
                                 applied_scope = True
                                 break
            
            # Location Distance (0:All, 1:Same City, 2:Nearby)
            dist_labels = {
                1: ["同城"],
                2: ["附近"],
            }
            if location_distance and location_distance > 0:
                 labels = dist_labels.get(location_distance, [])
                 applied_dist = False
                 for text in labels:
                     opt = self.page.ele(f'text:{text}', timeout=0.5)
                     if opt and opt.states.is_displayed:
                         opt.click()
                         logger.info(f"Applied location distance: {text}")
                         applied_dist = True
                         break
                 
                 # Look in filter menu if not found
                 if not applied_dist and labels:
                     filter_btn = self.page.ele('.graphic-filter') or \
                                  self.page.ele('text:筛选') or \
                                  self.page.ele('.filter-btn') or \
                                  self.page.ele('.filter-box')
                     if filter_btn:
                         self._hover_element(filter_btn)
                         self._sleep_with_jitter(1, jitter_ratio=0.3)
                         for text in labels:
                             opt = self.page.wait.ele_displayed(f'text:{text}', timeout=1)
                             if opt:
                                 opt.click()
                                 logger.info(f"Applied location distance via menu: {text}")
                                 applied_dist = True
                                 break

        except CaptchaDetectedException:
            raise
        except Exception as e:
            logger.warning(f"Failed to apply search filters: {e}")


    @retry(
        stop=stop_after_attempt(3),
        wait=wait_fixed(2),
        reraise=True,
        retry=retry_if_exception(
            lambda e: not isinstance(e, (LoginRequiredException, CaptchaDetectedException))
        ),
    )
    def search(self, keyword: str, start_page: int = 1, sort_type: str = "general", time_range: int = 0, note_type: int = 0, search_scope: int = 0, location_distance: int = 0):
        """
        Navigate to search results page and prepare for crawling.
        导航到搜索结果页面并准备爬取。
        """
        logger.info(f"Starting search for keyword: {keyword}, start: {start_page}, sort: {sort_type}, time: {time_range}, type: {note_type}, scope: {search_scope}, dist: {location_distance}")
        
        # 1. Visit Search Page | 1. 访问搜索页面
        # Construct URL with advanced options
        # Sort: general, popularity_desc, time_desc, comment_desc
        # Time: 0:All, 1:1Day, 2:1Week, 3:1Month, 4:6Months (publishTimeType)
        # Type: 0:All, 1:Video, 2:Image (noteType)
        search_url = f'https://www.xiaohongshu.com/search_result?keyword={keyword}&source=web_search_result_notes'
        
        if sort_type and sort_type != 'general':
            search_url += f"&sort={sort_type}"
        
        if time_range and time_range > 0:
            search_url += f"&publishTimeType={time_range}"
            
        if note_type and note_type > 0:
            search_url += f"&noteType={note_type}"
        
        self.page.get(search_url)
        
        # Check for login immediately after navigation
        self.check_and_wait_for_login()
        if self._stop_event: return
        
        # 2. Wait for initial load | 2. 等待初始加载
        self._check_captcha()
        if self._stop_event: return

        self.page.wait.load_start()
        self._sleep_with_jitter(2, jitter_ratio=0.3)
        
        self._save_cookies()
        self._apply_filters(note_type=note_type, time_range=time_range, sort_type=sort_type, search_scope=search_scope, location_distance=location_distance)
        
        # Fast forward if needed
        current_page = 1
        while current_page < start_page:
            if self._stop_event: return
            self.check_and_wait_for_login()
            logger.info(f"Navigating: Page {current_page} -> {current_page + 1}")
            self._next_page()
            current_page += 1
            
        logger.info(f"Reached start page: {start_page}")

    def start_search_crawling(self, keyword: str, start_page: int = 1, sort_type: str = "general", time_range: int = 0, note_type: int = 0, search_scope: int = 0, location_distance: int = 0) -> Generator[Dict[str, Any], None, None]:
        """
        Execute the full search crawling loop, controlling pagination.
        执行完整的搜索爬取循环，控制分页。
        """
        # Strict login check before starting search sequence
        # We try to ensure we are logged in before navigation
        if not self._is_logged_in_strict():
             logger.warning("Strict login check failed before search start. Initiating login wait...")
             self.check_and_wait_for_login()
             # Re-check and potentially loop or raise if strict check still fails
             # check_and_wait_for_login is designed to loop but let's be double sure
             if not self._is_logged_in_strict():
                 # Give it one more hard wait loop
                 max_retries = 10
                 for i in range(max_retries):
                     if self._stock_stop_event and self._stop_event: break
                     logger.warning(f"Waiting for strict login confirmation... ({i+1}/{max_retries})")
                     self._sleep_with_jitter(3)
                     if self._is_logged_in_strict():
                         break
                 else:
                     raise LoginRequiredException("Failed to confirm strict login status before searching.")

        self.search(keyword, start_page, sort_type, time_range, note_type, search_scope, location_distance)
        self._humanized_start_delay()
        
        # Determine max pages or stop condition?
        # For now, infinite loop until stop event or no next page
        page = start_page
        
        while not self._stop_event:
            self.current_page = page
            logger.info(f"Crawling page {page}...")
            self._simulate_micro_actions()
            
            # Ensure we are still logged in and not blocked
            self.check_and_wait_for_login()
            if self._stop_event: break
            
            # Listen for data on current page
            # We wait for a bit to capture data. XHS loads data via XHR.
            # We use a timeout loop to yield items.
            start_time = time.time()
            data_received = False
            
            # Use internal get_data generator logic but bounded by time/count per page
            # Since get_data is a generator loop, we can't easily "break" it externally without closing?
            # Actually, we can just iterate it with a timeout.
            
            # NOTE: iterating self.get_data() directly blocks until stop_event if we don't break.
            # We will manually call listen.wait() here to have finer control.
            
            # Wait for data. We allow some buffer for processing outside this generator.
            # We don't want to move to the next page too early, but also not too late.
            wait_deadline = time.time() + 15  # Max 15s to wait for a packet
            while time.time() < wait_deadline:
                if self._stop_event: break
                
                try:
                    # Adaptive timeout for packet wait
                    pace = getattr(self, '_pace_factor', 1.0)
                    wait_timeout = random.uniform(0.6, 1.2) * pace
                    packet = self.page.listen.wait(timeout=wait_timeout)
                    if packet:
                        # DEBUG LOG - enabled to diagnose capture issues
                        logger.debug(f"Packet received: {packet.url}")
                        
                        if 'search/notes' in packet.url or 'json-to-proto' in packet.url:
                             logger.info(f"Hit search endpoint: {packet.url}")
                        res_body = packet.response.body
                        
                        items_found = []
                        if isinstance(res_body, dict):
                            # Standard JSON response
                            data_block = res_body.get('data', {})
                            if isinstance(data_block, dict):
                                items_found = data_block.get('items', [])
                            elif isinstance(data_block, list):
                                # Direct list (possible in some proto-json proxies)
                                items_found = data_block
                        elif isinstance(res_body, list):
                            # Root is a list
                            items_found = res_body

                        if items_found:
                            data_received = True
                            no_data_counter = 0
                            for item in items_found:
                                # Ensure we only process notes or items that look like notes
                                if not isinstance(item, dict): continue
                                
                                # Check model_type if present, or infer from id/title
                                m_type = item.get('model_type')
                                if m_type and m_type != 'note':
                                    continue
                                    
                                # Plan B: Client-side video filtering
                                # Check 'note_card' -> 'type' (e.g. 'video', 'normal')
                                note_card = item.get('note_card', {})
                                if note_card and note_card.get('type') == 'video':
                                    # note_type: 0=All, 1=Video, 2=Image
                                    if note_type == 2: # Image only
                                        logger.debug(f"Skipping video note (filter): {item.get('id')}")
                                        continue
                                if note_card and note_type == 1 and note_card.get('type') != 'video': # Video only
                                     continue
                                    
                                # Attach page number for resume tracking
                                item_with_meta = dict(item)
                                item_with_meta["_page"] = page
                                yield item_with_meta
                        else:
                            # If successful API hit but no items, counter increments
                            if 'search/notes' in packet.url or ('json-to-proto' in packet.url and isinstance(res_body, dict) and res_body.get('data')):
                                no_data_counter += 1
                except Exception:
                    # Prevent tight spinning if listen.wait starts raising immediately.
                    time.sleep(random.uniform(0.15, 0.35))
                
                # If we've received data and processed it, we can break early from this page's wait loop.
                # HOWEVER, we only break if we've spent at least a 'human' amount of time looking at the page.
                if data_received:
                    min_view_time = random.uniform(1.5, 3.5) * pace
                    if time.time() - start_time > min_view_time:
                        break
             
            if self._stop_event:
                logger.info("Stop signal received during crawling.")
                break
                
            # Next Page
            logger.info("Moving to next page...")
            try:
                self._next_page()
                page += 1
                self.current_page = page
                self._humanized_page_pause(page)
            except EndOfResultsException as e:
                logger.info(f"End of results reached: {e}")
                raise
            except Exception as e:

                # Critical: Don't swallow Login/Captcha errors.
                if isinstance(e, (LoginRequiredException, CaptchaDetectedException)):
                    raise
                logger.error(f"Failed to go to next page: {e}")
                # We raise here to avoid marking task as 'completed' (success) when it actually crashed.
                raise

    def _next_page(self):
        """
        Perform action to go to next page: Scroll or Click 'Next'. / 执行翻页操作：滚动或点击“下一页”。
        Instructions emphasize: "Click 'next page' button". / 指令强调：“点击‘下一页’按钮”。
        """
        try:
            # Check captcha before action | 操作前检查验证码
            self._check_captcha()

            # Attempt to find "Next Page" button. | 尝试寻找“下一页”按钮。
            # Selectors might need adjustment based on XHS implementation updates. | 选择器可能需要根据小红书的实现更新进行调整。
            # Common patterns: text="下一页", or class contains 'next' | 常见模式：文本为“下一页”，或类名包含 'next'
            # DrissionPage 'text:' strategy matches substring by default? No, exact or fuzzy? | DrissionPage 'text:' 策略默认匹配子字符串吗？不，是精确匹配还是模糊匹配？
            # 'text:下一页' implies finding element containing text. | 'text:下一页' 意味着寻找包含该文本的元素。
            
            # Using a fairly generic approach first | 首先使用相当通用的方法
            # Check for "THE END" or "End of Results" equivalent
            # 用户反馈网页显示 "THE END"
            if self.page.ele('text:THE END', timeout=0.5) or self.page.ele('text:- THE END -', timeout=0.5) or \
               self.page.ele('text:没有更多内容', timeout=0.5) or self.page.ele('text:没有更多了', timeout=0.5) or \
               self.page.ele('text:到底了', timeout=0.5) or self.page.ele('.end-container', timeout=0.5) or \
               self.page.ele('xpath://*[contains(normalize-space(.), "THE END")]', timeout=0.5):
                logger.info("End of results detected ('THE END' marker).")
                raise EndOfResultsException("End of results reached.")

            next_btn = (
                self.page.ele('xpath://button[contains(normalize-space(.), "下一页")]', timeout=0.8)
                or self.page.ele('xpath://a[contains(normalize-space(.), "下一页")]', timeout=0.8)
                or self.page.ele('text:下一页', timeout=0.8)
                or self.page.ele('.btn-next', timeout=0.8)  # Hypothetical class | 假设的类名
                or self.page.ele('.pagination-next', timeout=0.8)
                or self.page.ele('xpath://*[@aria-label="下一页" or contains(@aria-label, "下一页")]', timeout=0.8)
            )

            if next_btn:
                # Scroll into view if needed? DrissionPage click handles it usually. | 如果需要，滚动到视图中？DrissionPage 点击通常会处理。
                next_btn.click()
                # Short settle time for the next page to render / API calls to fire.
                wait_time = random.uniform(1.8, 3.2)
                logger.debug(f"Action taken. Waiting {wait_time:.2f}s...")
                time.sleep(wait_time)
                self._check_captcha()
                return

            # Many XHS search pages are infinite-scroll (no explicit "next").
            logger.info("Next page button not found. Attempting scroll-to-bottom fallback.")
            prev_height = None
            try:
                prev_height = self.page.run_js("return document.documentElement.scrollHeight || document.body.scrollHeight;")
            except Exception:
                prev_height = None

            try:
                self.page.scroll.to_bottom()
            except Exception:
                # Fallback to a large scroll if to_bottom isn't available/works in the current context.
                try:
                    self.page.scroll.down(self._random_scroll_pixels((2400, 4200), variance=0.2))
                except Exception:
                    pass

            wait_time = random.uniform(2.2, 3.8)
            logger.debug(f"Scroll fallback taken. Waiting {wait_time:.2f}s...")
            time.sleep(wait_time)
            self._check_captcha()

            # If the end marker appears after scrolling, stop gracefully.
            if self.page.ele('text:THE END', timeout=0.6) or self.page.ele('text:- THE END -', timeout=0.6) or \
               self.page.ele('text:没有更多内容', timeout=0.6) or self.page.ele('text:没有更多了', timeout=0.6) or \
               self.page.ele('text:到底了', timeout=0.6) or self.page.ele('.end-container', timeout=0.6) or \
               self.page.ele('xpath://*[contains(normalize-space(.), "THE END")]', timeout=0.6):
                logger.info("End of results detected after scroll fallback.")
                raise EndOfResultsException("End of results reached.")

            # If scroll height doesn't grow, treat it as end to avoid endless loops.
            try:
                new_height = self.page.run_js("return document.documentElement.scrollHeight || document.body.scrollHeight;")
            except Exception:
                new_height = None

            if prev_height is not None and new_height is not None and new_height <= prev_height + 120:
                raise EndOfResultsException("No further content after scrolling (scroll height unchanged).")

        except CaptchaDetectedException:
            raise
        except EndOfResultsException:
            # This is a normal stopping condition; avoid logging it as an error here.
            raise
        except Exception as e:
            logger.error(f"Error during pagination: {e}")
            # Don't strictly crash on navigation error, maybe just stop? 
            # But prompt assumes valid pagination handling.
            raise

    def _check_captcha(self):
        """
        Check for captcha elements. Base version only detects and warns.
        Override in EnhancedXHSCrawler for auto-solving.
        """
        # Search for captcha elements | 搜索验证码元素
        slide_verify = self.page.ele('.slide-verify', timeout=1)
        if slide_verify:
            logger.warning("Captcha detected! (Auto-solving disabled in basic version. Please solve manually.)")
            # In a real learning scenario, we might want to pause here or raise exception
            # raise CaptchaDetectedException("Captcha detected. Please solve manually.")
            # For now, just wait a bit to give user a chance if headed
            if not self.headless:
                time.sleep(5)
            else:
                 raise CaptchaDetectedException("Captcha detected in headless mode.")

    def get_data(self) -> Generator[Dict[str, Any], None, None]:
        """
        Yield captured data packets from the listening queue. / 从监听队列中产生捕获的数据包。
        Uses page.listen.wait() in a loop as requested. / 按要求在循环中使用 page.listen.wait()。
        """
        logger.info("Starting data capture loop...")
        try:
            while not self._stop_event:
                # Wait for packet with timeout to allow checking _stop_event
                # wait returns None/False on timeout usually
                try:
                    packet = self.page.listen.wait(timeout=1)
                except Exception:
                    # Avoid busy-looping if the listener becomes unstable or the page is gone.
                    time.sleep(random.uniform(0.15, 0.35))
                    packet = None
                
                if not packet:
                    continue

                # Log all captured URLs for debugging | 记录所有捕获的 URL 以供调试
                logger.debug(f"Listener captured: {packet.url}")

                # Relaxed filter for search results | 放宽搜索结果的过滤条件
                is_search_packet = 'search/notes' in packet.url
                is_proto_packet = 'proto/json-to-proto-json-to-proto/proxy' in packet.url

                # Catch-all for potential API changes
                if 'api/sns/web' in packet.url and 'search' in packet.url:
                    if not is_search_packet and 'search/recommend' not in packet.url and 'search/hot' not in packet.url and 'search/trending' not in packet.url:
                        logger.warning(f"Potential unhandled search API detected: {packet.url}")
                        is_search_packet = True

                if is_search_packet or is_proto_packet:
                    logger.info(f"Processing search/proto packet: {packet.url}")
                    try:
                        # Parse JSON | 解析 JSON
                        # DrissionPage 4.x: packet.response.body might be dict or text | DrissionPage 4.x：packet.response.body 可能是字典或文本
                        res_body = packet.response.body
                        if not res_body:
                            logger.warning("Empty response body.")
                            continue
                            
                        # If it's already parsed as dict | 如果它已经被解析为字典
                        if isinstance(res_body, dict):
                            # Extract items list and yield individual notes
                            data = res_body.get('data')
                            items = []
                            if isinstance(data, list):
                                items = data
                            elif isinstance(data, dict):
                                items = data.get('items', [])
                            if items:
                                for item in items:
                                    # Check model_type
                                    if item.get('model_type') != 'note':
                                        continue
                                        
                                    # Plan B: Client-side video filtering
                                    # Check 'note_card' -> 'type' (e.g. 'video', 'normal')
                                    note_card = item.get('note_card', {})
                                    if note_card.get('type') == 'video':
                                        logger.debug(f"Skipping video note: {item.get('id')}")
                                        continue
                                        
                                    yield item
                            else:
                                logger.debug("No items found in response body.")
                        else:
                            # It might be None or failed parse | 它可能是 None 或解析失败
                            logger.warning(f"Packet body is not a dict, type: {type(res_body)}")
                            
                    except Exception as e:
                        logger.error(f"Error parsing packet body: {e}")
        except KeyboardInterrupt:
            logger.info("Data capture stopped by user.")

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_fixed(2),
        reraise=True,
        retry=retry_if_exception(
            lambda e: not isinstance(e, (LoginRequiredException, CaptchaDetectedException))
        ),
    )
    def scrape_note_detail(self, note_id: str, xsec_token: str = None, max_scrolls: int = None) -> Dict[str, Any]:
        """
        Scrapes detail of a note.
        
        CRITICAL CRAWLING STRATEGY:
        Direct access to `https://www.xiaohongshu.com/explore/{note_id}` often triggers
        anti-scraping blocks (temporarily unavailable), especially for frequent access.
        
        Best Practice:
        - Navigate via Search Results or User Profile (simulate natural flow).
        - If accessing directly, ensure high-quality cookies and random delays.
        - The `xsec_token` is crucial for authentic-looking requests.
        
        Scrape detail of a specific note, including title, content, images and comments.
        抓取特定笔记的详情，包括标题、内容、图片和评论。
        
        Args:
            note_id: The ID of the note. / 笔记 ID。
            xsec_token: Optional security token for accessing the note. / 可选的安全令牌。
            max_scrolls: Maximum number of scrolls for comments. If None, defaults to dynamic or 2.
            
        Returns:
            Dict containing scraped data. / 包含抓取数据的字典。
        """
        # Construct URL with xsec_token if available
        if xsec_token:
            url = f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token={xsec_token}&xsec_source=pc_search"
        else:
            url = f"https://www.xiaohongshu.com/explore/{note_id}"
        target_url = url
        logger.info(f"Navigating to note detail: {url}")
        self.page.get(target_url)
        
        self.check_and_wait_for_login()
        
        # Wait for content to load
        self.page.wait.load_start()
        self._sleep_with_jitter(1, jitter_ratio=0.3)  # Reduced from 2s - page loads quickly

        restriction = self._detect_security_restriction()
        if restriction:
            code = restriction.get("code") or ""
            message = restriction.get("message") or "安全限制"
            logger.warning(f"Security restriction detected while scanning note {note_id}: {message} {code}".strip())
            return {
                "note_id": note_id,
                "error": "security_restriction",
                "error_code": code,
                "error_message": message,
            }

        # Anti-scraping check: Check for redirect to generic explore feed or "unavailable" message
        # 反爬检查：检查是否重定向到发现页或显示“无法浏览”
        current_url = (self.page.url or "").rstrip("/")
        if current_url == "https://www.xiaohongshu.com/explore" or (
            "explore" in current_url and note_id not in current_url
        ):
            logger.warning(f"Redirected to Explore Feed instead of Note {note_id}. Likely login wall.")
            # Try reloading cookies then re-request the original target URL.
            # Refreshing the Explore page will keep us on Explore and never recover.
            self._load_cookies()
            self.page.get(target_url)
            self.page.wait.load_start()
            self._sleep_with_jitter(3, jitter_ratio=0.3)

            retry_url = (self.page.url or "").rstrip("/")
            if "explore" in retry_url and note_id not in retry_url:
                logger.error("❌ Login wall persists after cookie reload + retry navigation.")
                return {"note_id": note_id, "error": "login_wall"}

        if "当前笔记暂时无法浏览" in self.page.html:
             logger.warning(f"Note {note_id} is temporarily unavailable.")
             return {"note_id": note_id, "error": "unavailable"}
        
        data = {
            "note_id": note_id,
            "title": "",
            "desc": "",
            "images": [],
            "comments": [],
            "published_at": "",
            "share_url": f"https://www.xiaohongshu.com/explore/{note_id}"
        }
        
        try:
            # Identify Main Container to avoid sidebar/recommendation interference
            note_container = self.page.ele('.note-container', timeout=1) or self.page.ele('.main-container', timeout=1) or \
                             self.page.ele('#detail-container', timeout=1) or self.page.ele('.side-bar-container', timeout=1) or \
                             self.page.ele('tag:body')
            
            # 1. Scrape Title | 抓取标题
            title_ele = note_container.ele('#detail-title', timeout=0.5) or note_container.ele('.title', timeout=0.5) or \
                        note_container.ele('.note-detail-title', timeout=0.5) or note_container.ele('h1', timeout=0.5)
            data['title'] = title_ele.text.strip() if title_ele else ""

            # Fallback 1: Meta og:title
            if not data['title']:
                try:
                    # Try simplified syntax first, then xpath if needed
                    meta_title = self.page.ele('xpath://meta[@property="og:title"]') or \
                                 self.page.ele('xpath://meta[@name="og:title"]')
                    if meta_title:
                        data['title'] = meta_title.attr('content').strip()
                        logger.info(f"Extracted title from meta og:title: {data['title']}")
                except Exception as e:
                    logger.warning(f"Failed to extract title from meta: {e}")

            # Fallback 2: Page Title
            if not data['title']:
                try:
                    page_title = self.page.title
                    if page_title:
                         # Remove common suffix
                         clean_title = page_title.replace(" - 小红书", "").strip()
                         data['title'] = clean_title
                         logger.info(f"Extracted title from page title: {data['title']}")
                except Exception as e:
                    logger.warning(f"Failed to extract title from page title: {e}")
            
            # 2. Scrape Description | 抓取描述
            desc_ele = note_container.ele('#detail-desc', timeout=0.5) or note_container.ele('.desc', timeout=0.5) or \
                       note_container.ele('.note-text', timeout=0.5)
            data['desc'] = desc_ele.text if desc_ele else ""
            
            # 3. Scrape Images | 抓取图片
            # Use broader container search then filter
            media_container = note_container.ele('.media-container', timeout=0.5) or \
                              note_container.ele('.note-slider-img', timeout=0.5) or \
                              note_container.ele('.image-container', timeout=0.5) or \
                              note_container
            
            # Find all images in the media area
            imgs = media_container.eles('tag:img')
            for img in imgs:
                src = img.attr('src') or img.attr('data-src') or img.attr('data-original')
                # Broaden filter for content images while avoiding avatars/UI
                if src and ('xhscdn.com' in src or 'sns-img' in src or 'sns-web-img' in src) and \
                   'avatar' not in src and '/head/' not in src and 'logo' not in (img.attr('class') or ''):
                    data['images'].append(src)
            
            # Fallback: Check for background images in swiper slides if no imgs found
            if not data['images']:
                swiper_slides = note_container.eles('.swiper-slide')
                for slide in swiper_slides:
                    style = slide.attr('style')
                    if style and 'url(' in style:
                        import re
                        m = re.search(r'url\("?(.+?)"?\)', style)
                        if m:
                            data['images'].append(m.group(1))

            # Remove duplicates
            data['images'] = list(dict.fromkeys(data['images'])) # Preserve order

            # 4. Scrape Comments | 抓取评论
            logger.info("Scrolling to load comments...")
            # Targeted scrolling on scroll container if found
            scroll_container = note_container.ele('.note-scroller') or \
                               note_container.ele('.comments-container') or \
                               self.page
            

            # Dynamic scroll based on comment count and max_scrolls
            scroll_times = 2  # Default base
            
            try:
                # Attempt to find comment count
                total_comments = 0
                
                # Selector strategy:
                # 1. explicit count element (e.g. .total-reply, .comment-count)
                # 2. interaction container item
                count_ele = note_container.ele('.total-reply', timeout=0.5) or \
                            note_container.ele('.comment-count', timeout=0.5)
                
                if count_ele:
                     match = re.search(r'(\d+)', count_ele.text)
                     if match:
                         total_comments = int(match.group(1))

                # If no explicit count, check interactions
                if total_comments == 0:
                     interact_items = note_container.eles('.interaction-item')
                     if len(interact_items) >= 2:
                         # Iterate to find one with comment-like icon or assume pos
                         # For now, let's skip complex heuristic to avoid bad data
                         pass

                if total_comments > 0:
                    # Estimate: 1 scroll reveals ~10-15 comments
                    # We want to scroll enough to cover most.
                    needed_scrolls = int(total_comments / 12) + 1
                    
                    # Apply limits
                    # If max_scrolls is provided, use it as cap
                    limit = max_scrolls if max_scrolls is not None else 20
                    scroll_times = min(needed_scrolls, limit)
                    
                    # Ensure at least 2 if there are comments
                    scroll_times = max(2, scroll_times)
                    
                    logger.info(f"Dynamic scrolling: {total_comments} comments -> {scroll_times} scrolls (Limit: {limit})")
                elif max_scrolls is not None:
                     scroll_times = max_scrolls
                     
            except Exception as e:
                logger.debug(f"Dynamic scroll calculation failed: {e}")
                if max_scrolls is not None:
                    scroll_times = max_scrolls

            for i in range(scroll_times):  # Dynamic scroll count
                scroll_amount = self._random_scroll_pixels((600, 1200), variance=0.3)  # Smaller scrolls
                if scroll_container != self.page:
                    try:
                        direction = 1 if random.random() > 0.15 else -1
                        scroll_container.run_js(f'this.scrollTop += {direction * scroll_amount}')
                    except:
                        if direction > 0:
                            self.page.scroll.down(scroll_amount)
                        else:
                            self.page.scroll.up(scroll_amount // 3)
                else:
                    direction = 1 if random.random() > 0.15 else -1
                    if direction > 0:
                        self.page.scroll.down(scroll_amount)
                    else:
                        self.page.scroll.up(scroll_amount // 3)
                self._sleep_with_jitter(0.4, jitter_ratio=0.3)  # Reduced from 1s to 0.4s
            
            comment_eles = note_container.eles('.comment-item')
            for i, c_ele in enumerate(comment_eles):
                # Comment ID (if present on DOM)
                comment_id = (
                    c_ele.attr('data-id')
                    or c_ele.attr('data-comment-id')
                    or c_ele.attr('id')
                )
                if comment_id and isinstance(comment_id, str) and comment_id.startswith("comment-"):
                    comment_id = comment_id[len("comment-") :]

                # User
                user_ele = c_ele.ele('.name') or c_ele.ele('.nickname') or c_ele.ele('.user-name')
                user = user_ele.text.strip() if user_ele else "Unknown"
                user_id = None
                try:
                    # If the username is a link, attempt to parse user id from URL path
                    user_link = (user_ele.ele('tag:a') if user_ele else None) or user_ele
                    href = user_link.attr('href') if user_link else None
                    if href and isinstance(href, str):
                        user_id = href.rstrip("/").split("/")[-1] or None
                except Exception:
                    user_id = None
                
                # DEBUG: Print HTML to stdout
                # DEBUG: Print HTML to stdout for first 2 comments
                # DEBUG: Print HTML to stdout for first 2 comments
                # if i < 2:
                #     try:
                #         logger.info(f"DEBUG_COMMENT_CLASS: {c_ele.attr('class')}")
                #         has_expand_inner = "展开" in c_ele.html
                #         logger.info(f"DEBUG_HAS_EXPAND_INNER: {has_expand_inner}")
                #         
                #         parent_html = c_ele.parent().html
                #         has_expand_parent = "展开" in parent_html
                #         logger.info(f"DEBUG_HAS_EXPAND_PARENT: {has_expand_parent}")
                #         # logger.info(f"DEBUG_PARENT_HTML_START\n{parent_html[:1000]}\nDEBUG_PARENT_HTML_END")
                # 
                #         total_expand_btns = len(self.page.eles('text:展开'))
                #         logger.info(f"DEBUG_TOTAL_EXPAND_BTNS_ON_PAGE: {total_expand_btns}")
                # 
                #         next_ele = c_ele.next()
                #         if next_ele:
                #             logger.info(f"DEBUG_NEXT_SIBLING_TAG: {next_ele.tag}")
                #             logger.info(f"DEBUG_NEXT_SIBLING_CLASS: {next_ele.attr('class')}")
                #             logger.info(f"DEBUG_HAS_EXPAND_NEXT: {'展开' in next_ele.html}")
                #         
                #         logger.info(f"DEBUG_PARENT_CHILDREN_COUNT: {len(c_ele.parent().children())}")
                # 
                #         logger.debug(f"DEBUG_HTML_DUMP_START_{i}\n{c_ele.html[:10000]}\nDEBUG_HTML_DUMP_END_{i}")
                #     except Exception as e:
                #         logger.error(f"DEBUG_HTML_DUMP_ERROR: {e}")
                
                # Content
                content_ele = c_ele.ele('.content') or c_ele.ele('.comment-content') or c_ele.ele('.note-text')
                content = content_ele.text.strip() if content_ele else ""
                
                # Date & Location
                date_ele = c_ele.ele('.date') or c_ele.ele('.comment-date') or c_ele.ele('.info .date')
                c_date = date_ele.text.strip() if date_ele else ""
                
                # IP Location (often part of date or separate)
                # Structure might be: "10-24 上海" or separate span
                c_location = ""
                location_ele = c_ele.ele('.location')
                if location_ele:
                    c_location = location_ele.text.strip()
                
                # Like Count
                like_count = "0"
                like_ele = c_ele.ele('.like-count') or c_ele.ele('.like-wrapper .count')
                if like_ele:
                    like_count = like_ele.text.strip()


                if content:
                    payload = {
                        "user": user, 
                        "content": content, 
                        "date": c_date, 
                        "like_count": like_count,
                        "ip_location": c_location,
                    }
                    if comment_id:
                        payload["id"] = str(comment_id)
                    if user_id:
                        payload["user_id"] = str(user_id)
                    
                    # Conditional Sub-comment Crawling
                    # If this comment is a Question or Negative Review, try to expand replies.
                    should_expand = False
                    # Conditional Sub-comment Crawling: Simple keyword check
                    c_low = content.lower()
                    if any(k in c_low for k in ["?", "？", "求", "请问", "差评", "避雷", "踩雷"]):
                        should_expand = True
                    
                    # Determine where to look for replies (Sibling container or inside self)
                    search_scope = c_ele
                    next_ele = c_ele.next()
                    if next_ele and "reply-container" in (next_ele.attr("class") or ""):
                        search_scope = next_ele
                    
                    # Try to find expand button in the determined scope
                    # We look for "展开" or "条回复" (e.g. 展开8条回复)
                    reply_expand_btn = search_scope.ele('.reply-expand', timeout=0.5) or \
                                       search_scope.ele('css:[class*="reply-expand"]', timeout=0.5) or \
                                       search_scope.ele('text:展开', timeout=0.5) or \
                                       search_scope.ele('text:条回复', timeout=0.5)

                    # Probabilistic Expansion based on Reply Count
                    # Even if not relevant by keyword, expand if reply count is high.
                    if reply_expand_btn and not should_expand:
                        try:
                            import re
                            btn_text = reply_expand_btn.text
                            match = re.search(r'(\d+)', btn_text)
                            if match:
                                count = int(match.group(1))
                                logger.info(f"Found reply btn (regex match): '{btn_text}' -> count {count}")
                                
                                # Probabilistic Logic
                                if count > 20: 
                                    prob = 1.0     # Always expand huge threads
                                elif count > 5:
                                    prob = 0.9     # High chance for active threads
                                elif count > 2:
                                    prob = 0.5     # Moderate chance
                                else:
                                    prob = 0.0     # Skip tiny ones (save time)
                                
                                if random.random() < prob:
                                    should_expand = True
                                    logger.info(f"Probabilistic expansion triggered (count={count}, prob={prob})")
                                else:
                                    logger.info(f"Probabilistic expansion skipped (count={count}, prob={prob})")
                            else:
                                # Regex failed but button found (e.g. "展开回复") -> Assume relevant
                                logger.info(f"Found reply btn (no number): '{btn_text}'. Forcing expansion check.")
                                should_expand = True
                                count = 1 # Dummy for data
                        except Exception:
                            pass
                    
                    if should_expand and reply_expand_btn:
                        try:
                            logger.info(f"Attempting to click expand button (JS Click): {reply_expand_btn.tag} | {reply_expand_btn.html[:100]}")
                            reply_expand_btn.click(by_js=True)
                            
                            logger.info("Clicked expand. Waiting for replies to load (2s)...")
                            self._sleep_with_jitter(2.0, jitter_ratio=0.3)
                                
                            # Scrape replies from the search scope
                            # Confirmed: XHS uses .comment-item-sub for sub-comments in the reply container
                            # Strategy: Try list-container children first (most reliable structure)
                            reply_items = []
                            list_container = search_scope.ele('.list-container', timeout=1)
                            if list_container:
                                reply_items = list_container.children()
                                logger.info(f"Found {len(reply_items)} sub-comments via .list-container")
                            else:
                                # Fallback to direct selectors
                                reply_items = search_scope.eles('css:.comment-item-sub', timeout=1) or \
                                              search_scope.eles('.reply-item', timeout=0.5) or \
                                              search_scope.eles('.comment-item', timeout=0.5)
                                if reply_items:
                                    logger.info(f"Found {len(reply_items)} sub-comments via direct selector")
                            
                            if not reply_items:
                                logger.warning(f"Expanded but no replies found! Selector issue or load failed? Button: {reply_expand_btn.text}")

                            # Filter out self (if it selects the parent) -> typically .reply-item is distinct
                            
                            replies_data = []
                            for r_item in reply_items:
                                r_content_ele = r_item.ele('.content') or r_item.ele('.note-text')
                                if not r_content_ele:
                                    continue
                                r_content = r_content_ele.text.strip()
                                
                                r_user_ele = r_item.ele('.name') or r_item.ele('.nickname')
                                r_user = r_user_ele.text.strip() if r_user_ele else "Unknown"
                                
                                if r_content:
                                    reply_item = {
                                        "user": r_user,
                                        "content": r_content
                                    }
                                    # Extract meta info
                                    try:
                                        date_ele = r_item.ele('.date', timeout=0.5) or r_item.ele('.comment-date', timeout=0.5)
                                        reply_item['date'] = date_ele.text.strip() if date_ele else ""
                                        
                                        like_ele = r_item.ele('.like-count', timeout=0.5) or r_item.ele('.like-wrapper', timeout=0.5)
                                        reply_item['likes'] = like_ele.text.strip() if like_ele else "0"
                                        
                                        location_ele = r_item.ele('.location', timeout=0.5)
                                        reply_item['location'] = location_ele.text.strip() if location_ele else ""
                                    except Exception:
                                        pass
                                    replies_data.append(reply_item)
                            
                            if replies_data:
                                payload["replies"] = replies_data
                                logger.info(f"  -> Captured {len(replies_data)} replies.")
                        except Exception as e_reply:
                            logger.debug(f"Failed to expand/scrape replies: {e_reply}")

                    data['comments'].append(payload)
                    
            logger.info(f"Scraped {len(data['comments'])} comments.")
            
            # 5. Extract Date
            data['published_at'] = self._extract_date(note_container)
            
            # 6. Extract Author
            if not data.get('nickname'):
                 author_ele = note_container.ele('.author-wrapper .name') or \
                              note_container.ele('.username') or \
                              note_container.ele('.author-name')
                 if author_ele:
                        data['nickname'] = author_ele.text.strip()
                        
            # 7. Extract Stats (Likes/Collects)
            # Try sidebar or bottom bar interaction container
            interact_container = note_container.ele('.interact-container', timeout=0.5) or \
                                 note_container.ele('.interaction-container', timeout=0.5) or \
                                 self.page.ele('.interact-container', timeout=0.5)
            
            if interact_container:
                # XHS Web usually has: [Like] [Collect] [Comment]
                # Selector: .interact-item
                items = interact_container.eles('.interact-item')
                if items:
                    # First item is usually Like
                    try:
                         # Text might be in a .count span or direct
                         count_ele = items[0].ele('.count') or items[0].ele('.text')
                         data['likes'] = count_ele.text.strip() if count_ele else items[0].text.strip()
                    except: pass
                    
                    # Second item is usually Collect
                    if len(items) > 1:
                        try:
                            count_ele = items[1].ele('.count') or items[1].ele('.text')
                            data['collected'] = count_ele.text.strip() if count_ele else items[1].text.strip()
                        except: pass
            
            # Fallback for likes if not found in interaction container (e.g. searching specific class)
            if not data.get('likes'):
                 like_ele = note_container.ele('.like-wrapper .count', timeout=0.5)
                 if like_ele:
                     data['likes'] = like_ele.text.strip()

            if not data.get('user') and data.get('nickname'):
                # Populate user dict for compat
                data['user'] = {
                    "nickname": data.get('nickname'),
                    "id": None # ID is hard to get from detail page unless in URL or API
                }
                
                # Check URL for user ID if possible (often not present in explore/id URL)
                # But sometimes available in author link
                try:
                    author_link = note_container.ele('.author-wrapper', timeout=0.5) or note_container.ele('.author-container', timeout=0.5)
                    if author_link:
                        # Try to find a link to user profile
                        a_tag = author_link.ele('tag:a') or author_link if author_link.tag == 'a' else None
                        if a_tag:
                            href = a_tag.attr('href')
                            if href and '/user/profile/' in href:
                                uid = href.split('/user/profile/')[-1].split('?')[0]
                                data['user']['user_id'] = uid
                except: pass

            
        except Exception as e:
            logger.error(f"Error scraping note detail: {e}")
        
        return data
            


    def _extract_date(self, container=None):
        """
        Extract publication date from the page.
        Tries multiple selectors.
        """
        try:
            # Use container if provided, else page
            scope = container if container else self.page

            # Common selector: .date
            # Or text containing "发布于"
            date_ele = (
                scope.ele('.date')
                or scope.ele('.publish-date')
                or scope.ele('.bottom-container .time')
                or self.page.ele('css:span[class*="date"]')
            )

            if date_ele:
                text = date_ele.text.strip()
                # Clean up "发布于 " prefix if present inside the text (sometimes it's separate)
                return text.replace('发布于', '').replace('编辑于', '').strip()
        except Exception:
            pass
        return ""
    
    def click_note_author(self) -> bool:
        """
        Click the author name/avatar on the note detail page to navigate to profile.
        Returns True if successfully navigated to profile page.
        """
        try:
            # Common selectors for author link in note detail
            author_ele = (
                self.page.ele('css:.author-container a[href*="/user/profile/"]')
                or self.page.ele('css:.author-wrapper a[href*="/user/profile/"]')
                or self.page.ele('css:a.name[href*="/user/profile/"]')
                or self.page.ele('css:a[href*="/user/profile/"]')  # Generic fallback
            )
            
            if not author_ele:
                logger.warning("Could not find clickable author element on note page.")
                return False
            
            # Extract href for verification/fallback
            href = author_ele.attr('href')
            logger.info(f"Found author element with href: {href}. Clicking...")
            
            author_ele.click()
            self._sleep_with_jitter(2)
            
            # Check if navigation happened
            current_url = self.page.url
            
            # Handle new tab if opened
            if self.page.tabs_count > 1:
                logger.info("Switching to new tab for author profile.")
                self.page = self.page.latest_tab
                current_url = self.page.url
            
            # Verify we're on profile page
            if "/user/profile/" in current_url:
                logger.info(f"Successfully navigated to profile: {current_url}")
                return True
            else:
                # Click didn't navigate - try direct navigation using the href
                if href and "/user/profile/" in href:
                    profile_url = f"https://www.xiaohongshu.com{href}" if href.startswith("/") else href
                    logger.info(f"Click didn't navigate. Opening directly: {profile_url}")
                    self.page.get(profile_url)
                    self._sleep_with_jitter(2)
                    return True
                else:
                    logger.warning(f"Click didn't navigate and no valid href found. Current URL: {current_url}")
                    return False
                    
        except Exception as e:
            logger.error(f"Error clicking author: {e}")
            return False

    def scrape_user_profile(self, user_id: str, navigate: bool = True) -> Dict[str, Any]:
        """
        Scrape user profile information.
        :param navigate: If True, go to URL direct. If False, assume current page is profile.
        """
        if not user_id:
            return {}
            
        url = f"https://www.xiaohongshu.com/user/profile/{user_id}"
        
        if navigate:
            logger.info(f"Scraping user profile (direct): {url}")
            try:
                self.page.get(url)
                self._sleep_with_jitter(2, jitter_ratio=0.3)
            except Exception as e:
                logger.error(f"Navigation failed: {e}")
                return {}
        else:
            # Wait for any pending navigation to complete
            self._sleep_with_jitter(1, jitter_ratio=0.2)
            current_url = self.page.url
            logger.info(f"Scraping user profile (current page): {current_url}")
            
            # If current page is not the expected profile, try navigating
            if user_id not in current_url and "/user/profile/" not in current_url:
                logger.warning(f"Current page doesn't appear to be profile. Navigating to {url}")
                try:
                    self.page.get(url)
                    self._sleep_with_jitter(2, jitter_ratio=0.3)
                except Exception as e:
                    logger.error(f"Fallback navigation failed: {e}")
                    return {}

        try:
            self._check_captcha()
            self.check_login_status()
            
            # Give page more time to stabilize and verify we're on profile
            time.sleep(2)
            final_url = self.page.url
            logger.debug(f"Final URL before extraction: {final_url}")
            
            # If still not on profile, the page may have redirected - abort
            if "/user/profile/" not in final_url:
                logger.error(f"Not on profile page. Final URL: {final_url}")
                return {}
            
            # Wait for key element
            user_nickname_ele = self.page.ele('.user-nickname', timeout=5) or \
                                self.page.ele('.user-name', timeout=5)
                                
            if not user_nickname_ele:
                 # Double check if we are on right page if navigate=False
                 if not navigate and user_id not in self.page.url and "profile" not in self.page.url:
                     logger.warning(f"Current page {self.page.url} does not look like profile for {user_id}")
                     
                 logger.warning(f"Could not load user profile for {user_id} (nickname missing).")
                 return {}
                 
            # Extract basic info
            nickname = user_nickname_ele.text
            avatar_ele = self.page.ele('.user-image img') or self.page.ele('.avatar-wrapper img')
            avatar = avatar_ele.attr('src') if avatar_ele else None
            
            desc_ele = self.page.ele('.user-desc') or self.page.ele('.user-description')
            desc = desc_ele.text if desc_ele else None
            
            # Extract stats (Follows/Fans/Likes)
            # Typically structure: 
            # <div class="user-interactions">
            #   <div> <span class="count">12</span> <span class="label">关注</span> </div> ...
            # </div>
            follows = None
            fans = None
            interaction = None
            
            stats = {}
            stats = {}
            # interactions = self.page.eles('.user-interactions div') 
            # Use 2-step find to be safe
            interactions_container = self.page.ele('.user-interactions')
            if interactions_container:
                interactions = interactions_container.eles('tag:div')
                logger.info(f"Found {len(interactions)} interaction divs via container.")
            else:
                interactions = []
                logger.warning("Could not find .user-interactions container")
            
            for i, div in enumerate(interactions):
                count_ele = div.ele('.count')
                label_ele = div.ele('.label') or div.ele('.shows') # Added .shows
                
                # Debug stats
                logger.info(f"Stats Div {i}: count='{count_ele.text if count_ele else 'N/A'}' label='{label_ele.text if label_ele else 'N/A'}'")
                
                if count_ele and label_ele:
                     txt = label_ele.text
                     val_str = count_ele.text
                     val = 0
                     try:
                         # Handle "1.2万" or "1000+" etc
                         val_text = val_str.replace('+', '') # 1000+ -> 1000
                         if '万' in val_text:
                             val = int(float(val_text.replace('万','')) * 10000)
                         else:
                             val = int(val_text)
                     except Exception as e:
                         # logger.warning(f"Failed to parse stat value {val_str}: {e}")
                         pass
                     
                     if '关注' in txt: stats['follows'] = val
                     elif '粉丝' in txt: stats['fans'] = val
                     elif '获赞' in txt: stats['interaction'] = val # '获赞与收藏'

            # Extract location / gender if available
            # Often in .user-info or tags
            gender = 0 # 0 unknown, 1 male, 2 female
            location = ""
            tags = self.page.eles('.user-tags .tag-item')
            for tag in tags:
                t = tag.text
                if 'IP属地：' in t:
                    location = t.replace('IP属地：', '').strip()
                elif '男' == t: gender = 1
                elif '女' == t: gender = 2
                # Age etc

            # Extract Recent Notes (New Feature)
            recent_notes = []
            try:
                # Wait for notes to load
                logger.info("Waiting for note items...")
                self.page.wait.ele_displayed('.note-item', timeout=10)
                
                # Notes are usually in a grid under tabs. 
                # Selector similar to search results: .note-item, section.note-item
                note_cards = self.page.eles('.note-item') or self.page.eles('section.note-item')
                logger.info(f"Found {len(note_cards)} note cards.")
                # Limit to first 20 to avoid excessive processing
                for card in note_cards[:20]:
                    try:
                        # Extract basic info
                        n_id = ""
                        # Try to get ID from href
                        link = card.ele('tag:a')
                        if link:
                            href = link.attr('href')
                            # href="/explore/65xyz..."
                            if href and '/explore/' in href:
                                n_id = href.split('/explore/')[-1].split('?')[0]
                        
                        # Fallback: if no ID found, generate one from title hash or skip
                        # For verification purposes, we want to see the title and likes
                        if not n_id:
                             title_txt = (card.ele('.title') or card.ele('.footer .title') or link).text or "unknown"
                             n_id = f"unknown_{hash(title_txt)}"

                        n_title = (card.ele('.title') or card.ele('.footer .title') or link).text or ""
                        n_cover_ele = card.ele('img')
                        n_cover = n_cover_ele.attr('src') if n_cover_ele else ""
                        
                        # Extract Like Count
                        n_likes = 0
                        try:
                            # Usually in footer: .like-wrapper .count
                            like_ele = card.ele('.like-wrapper .count') or card.ele('.footer .count')
                            if like_ele:
                                l_str = like_ele.text.strip()
                                # Handle "1.2万", "1000+"
                                l_clean = l_str.replace('+', '')
                                if '万' in l_clean:
                                    n_likes = int(float(l_clean.replace('万', '')) * 10000)
                                else:
                                    n_likes = int(l_clean)
                        except:
                            pass
                        
                        # Add to list
                        recent_notes.append({
                            "note_id": n_id,
                            "title": n_title,
                            "cover_url": n_cover,
                            "like_count": n_likes
                        })
                    except Exception:
                        continue
                logger.info(f"Found {len(recent_notes)} recent notes on profile.")
            except Exception as e:
                logger.warning(f"Failed to extract recent notes: {e}")

            return {
                "user_id": user_id,
                "nickname": nickname,
                "avatar": avatar,
                "desc": desc,
                "follows": stats.get('follows', 0),
                "fans": stats.get('fans', 0),
                "interaction": stats.get('interaction', 0),
                "gender": gender,
                "location": location,
                "recent_notes": recent_notes
            }
            
        except Exception as e:
            logger.error(f"Failed to scrape user profile {user_id}: {e}")
            return {}

    def close(self):

        """Close the browser. / 关闭浏览器。"""
        try:
            self._save_cookies()
        except Exception as e:
            logger.warning(f"Failed to save cookies on close: {e}")
        self.page.quit()

if __name__ == "__main__":
    # Internal simple test | 内部简单测试
    try:
        crawler = XHSCrawler(headless=False, port=9223)
        
        # 0. Check Login
        crawler.check_login_status()

        # 1. Search to get a note ID
        crawler.search("遮瑕", start_page=1)
        
        target_note_ids = []
        count = 0
        # Listen for search results
        logger.info("Listening for search results to pick notes...")
        for data in crawler.get_data():
            # data is the raw JSON response dict
            # Structure usually: data['data']['items'] -> list of notes
            if 'data' in data and 'items' in data['data']:
                items = data['data']['items']
                if items:
                    for item in items:
                        if item.get('model_type') == 'note': # or check 'id'
                            note_id = item.get('id') or item.get('note_id')
                            if note_id and note_id not in target_note_ids:
                                target_note_ids.append(note_id)
                                logger.info(f"Found candidate note ID: {note_id}")
            
            if len(target_note_ids) >= 3: # Collect top 3 candidates
                break
                
            count += 1
            if count > 5: # Timeout/Giveup
                break
        
        crawler.stop() # Stop the listener loop
        
        if target_note_ids:
            # 2. Go to detail page (Try candidates until success)
            for note_id in target_note_ids:
                logger.info(f"Attempting to scrape note: {note_id}")
                detail_data = crawler.scrape_note_detail(note_id)
                
                if "无法浏览" in detail_data['title'] or not detail_data['title']:
                    logger.warning(f"Note {note_id} unavailable or empty. Trying next...")
                    continue
                
                print("Scraped Data Summary:")
                print(f"Title: {detail_data['title']}")
                print(f"Desc: {detail_data['desc'][:50]}...")
                print(f"Images: {len(detail_data['images'])} found")
                print(f"Comments: {len(detail_data['comments'])} found")
                print("First 3 comments:")
                for c in detail_data['comments'][:3]:
                    print(f"  - {c['user']}: {c['content']}")
                
                # If success, break
                break
        else:
            logger.warning("Could not find any note IDs from search results.")


    except Exception as e:
        logger.exception(e)
    finally:
        if 'crawler' in locals():
            crawler.close()
