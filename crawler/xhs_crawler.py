import random
import time
from typing import Generator, Dict, Any, Optional

from DrissionPage import ChromiumPage, ChromiumOptions
from loguru import logger

from crawler.exceptions import CaptchaDetectedException
from core.captcha_solver import CaptchaSolver
import json
import os
from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_exception_type


class XHSCrawler:
    def __init__(self, headless: bool = False, user_data_path: Optional[str] = None, port: int = 9222, cookie_path: str = "cookies.json"):
        """
        Initialize the XHS Crawler. / 初始化小红书爬虫。

        Args:
            headless: Whether to run in headless mode. / 是否以无头模式运行。
            user_data_path: Path to user data directory for session persistence. / 用于会话持久化的用户数据目录路径。
            port: Local debugging port to avoid conflicts. / 本地调试端口，用于避免冲突。
            cookie_path: Path to save/load cookies. / 保存/加载 Cookie 的路径。
        """
        self.cookie_path = cookie_path
        co = ChromiumOptions(read_file=False)
        co.set_local_port(port)
        if headless:
            co.headless()
        
        # Anti-detection settings / "Stealth" | 反检测设置 / “隐身”
        co.set_argument('--no-sandbox')
        co.set_argument('--disable-gpu')
        # Mute audio to avoid noise | 静音以避免噪音
        co.mute(True)
        co.ignore_certificate_errors()
        
        if user_data_path:
            co.set_user_data_path(user_data_path)

        # Initialize page object | 初始化页面对象
        self.page = ChromiumPage(addr_or_opts=co)
        
        # Load cookies if available | 如果可用，加载 Cookie
        self._load_cookies()
        
        # Initialize captcha solver | 初始化验证码识别器
        self.solver = CaptchaSolver()
        
        # Start listening for search notes API | 开始监听搜索笔记 API
        # Target: /api/sns/web/v1/search/notes | 目标：/api/sns/web/v1/search/notes
        # Broadening scope to debug | 扩大范围以调试
        self.page.listen.start('api/sns/web/') 
        logger.info("XHSCrawler initialized. Listening for 'api/sns/web/'.")
        
        self._stop_event = False

    def _load_cookies(self):
        """Load cookies from local file. / 从本地文件加载 Cookie。"""
        if os.path.exists(self.cookie_path):
            try:
                with open(self.cookie_path, 'r', encoding='utf-8') as f:
                    cookies = json.load(f)
                self.page.set.cookies(cookies)
                logger.info(f"Loaded cookies from {self.cookie_path}")
            except Exception as e:
                logger.error(f"Failed to load cookies: {e}")

    def _save_cookies(self):
        """Save current cookies to local file. / 保存当前 Cookie 到本地文件。"""
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
            
            with open(self.cookie_path, 'w', encoding='utf-8') as f:
                json.dump(cookies, f, indent=2)
            logger.info(f"Saved cookies to {self.cookie_path}")
        except Exception as e:
            logger.error(f"Failed to save cookies: {e}")

    def check_login_status(self):
        """
        Proactively check if logged in. If not, trigger login flow.
        主动检查是否登录。如果未登录，触发登录流程。
        Also detects "Security Restriction" pages. / 同时检测"安全限制"页面。
        """
        try:
            self.page.get("https://www.xiaohongshu.com")
            time.sleep(2)
            
            # Check for Security Restriction page | 检查安全限制页面
            current_url = self.page.url
            if 'website-login/error' in current_url or '安全限制' in (self.page.title or ''):
                logger.warning("Security Restriction page detected! Session may be invalid.")
                logger.warning("安全限制页面检测到！会话可能已失效。")
                # Clear potentially bad cookies and wait for fresh login
                self.check_and_wait_for_login()
                return
            
            # Check for login button or user icon
            # If 'Login' button is visible, we are not logged in
            if self.page.ele('text=登录注册') or self.page.ele('.login-container'):
                logger.warning("Not logged in (Login button detected).")
                self.check_and_wait_for_login()
            else:
                # Positive check: verify if we are REALLY logged in
                # Look for user avatar, nickname, or side menu
                is_logged_in = False
                if self.page.ele('#user-avatar') or \
                   self.page.ele('.user-name') or \
                   self.page.ele('.user-side-content') or \
                   self.page.ele('.avatar-wrapper'):
                    is_logged_in = True
                
                if is_logged_in:
                    logger.info("Login status validated (User element found).")
                    # Save cookies on successful login check | 登录检查成功时保存 Cookie
                    self._save_cookies()
                else:
                    logger.warning("Ambiguous login state: No login button, but no user element found.")
                    # It might be a guest view or loading issue. 
                    # We should probably treat it as not logged in if it persists.
                    # Let's give it a retry or force check
                    logger.warning("Assuming not logged in. Triggering login wait...")
                    self.check_and_wait_for_login()
        except Exception as e:
            logger.error(f"Error checking login status: {e}")

    def stop(self):
        """Signal the crawler to stop."""
        self._stop_event = True
        logger.info("Crawler stop signal received.")
        # Save cookies to ensure next run has fresh session
        self._save_cookies()

    def check_and_wait_for_login(self):
        """
        Check if login is detected and wait for user to complete it.
        检查是否出现登录窗口，并等待用户完成登录。
        """
        # Quick check for login elements
        # .login-container is a common class for the login modal
        # Also checking for text "登录" in specific contexts if needed
        # Using DrissionPage syntax
        try:
            # 尝试查找关闭按钮：.close-icon, .close, .icon-close-circle
            # 有时候只需关闭弹窗即可继续浏览
            close_btn = self.page.ele('.close-icon') or \
                        self.page.ele('css:[class*="close-circle"]') or \
                        self.page.ele('.icon-close')
            
            login_modal = self.page.ele('.login-container') or \
                          self.page.ele('.login-modal') or \
                          self.page.ele('text=登录注册')

            if login_modal:
                logger.warning("Login required detected!")
                
                # 尝试点击关闭按钮
                if close_btn:
                    logger.info("Attempting to close login modal...")
                    try:
                        close_btn.click()
                        time.sleep(1)
                        if not (self.page.ele('.login-container') or self.page.ele('.login-modal')):
                            logger.success("Login modal closed successfully.")
                            return
                    except Exception as e:
                        logger.warning(f"Failed to close login modal: {e}")

                logger.warning("The crawler is paused. Please login manually or scan QR code.")
                
                # Wait until the modal covers or button disappears
                while self.page.ele('.login-container') or \
                      self.page.ele('.login-modal') or \
                      (self.page.ele('text=登录注册') and self.page.ele('text=登录注册').states.is_displayed):
                    if self._stop_event:
                        logger.info("Stop signal received while waiting for login.")
                        return
                    time.sleep(2)
                
                logger.info("Login state seems resolved. Resuming...")
                self._save_cookies() # Save cookies after successful login | 登录成功后保存 Cookie
                time.sleep(3) # Wait for page reload/redirect
        except Exception as e:
            # Login check shouldn't crash the flow
            logger.debug(f"Login check non-fatal error: {e}")


    @retry(stop=stop_after_attempt(3), wait=wait_fixed(2))
    def search(self, keyword: str, start_page: int = 1, sort_type: str = "general", time_range: int = 0, note_type: int = 0):
        """
        Navigate to search results page and prepare for crawling.
        导航到搜索结果页面并准备爬取。
        """
        logger.info(f"Starting search for keyword: {keyword}, start: {start_page}, sort: {sort_type}, time: {time_range}, type: {note_type}")
        
        # 1. Visit Search Page | 1. 访问搜索页面
        # Construct URL with advanced options
        # Sort: general, popularity_desc, time_desc
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
        time.sleep(2)
        
        self._save_cookies()
        
        # Fast forward if needed
        current_page = 1
        while current_page < start_page:
            if self._stop_event: return
            self.check_and_wait_for_login()
            logger.info(f"Navigating: Page {current_page} -> {current_page + 1}")
            self._next_page()
            current_page += 1
            
        logger.info(f"Reached start page: {start_page}")

    def start_search_crawling(self, keyword: str, start_page: int = 1, sort_type: str = "general", time_range: int = 0, note_type: int = 0) -> Generator[Dict[str, Any], None, None]:
        """
        Execute the full search crawling loop, controlling pagination.
        执行完整的搜索爬取循环，控制分页。
        """
        self.search(keyword, start_page, sort_type, time_range, note_type)
        
        # Determine max pages or stop condition?
        # For now, infinite loop until stop event or no next page
        page = start_page
        
        while not self._stop_event:
            logger.info(f"Crawling page {page}...")
            
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
            
            no_data_counter = 0
            
            # Spending some time on the page to collect data
            while time.time() - start_time < 10: # Wait up to 10s per page for data
                if self._stop_event: break
                
                try:
                    packet = self.page.listen.wait(timeout=1)
                    if packet and 'search/notes' in packet.url:
                        # Process packet similar to get_data logic
                        res_body = packet.response.body
                        if isinstance(res_body, dict):
                            items = res_body.get('data', {}).get('items', [])
                            if items:
                                data_received = True
                                no_data_counter = 0
                                for item in items:
                                    # Ensure we only process notes
                                    if item.get('model_type') == 'note':
                                        # Video filter logic based on note_type
                                        is_video = item.get('note_card', {}).get('type') == 'video'
                                        
                                        # note_type: 0=All(Default), 1=Video, 2=Image
                                        if note_type == 2 and is_video:
                                            continue
                                        if note_type == 1 and not is_video:
                                            continue
                                            
                                        yield item
                            else:
                                no_data_counter += 1
                except Exception:
                    pass
                
                if data_received and time.time() - start_time > 5:
                    # If we got data and spent at least 5s, maybe move on?
                    # But XHS might load more on scroll?
                    # Usually one XHR per page load.
                    break
             
            if self._stop_event:
                logger.info("Stop signal received during crawling.")
                break
                
            # Next Page
            logger.info("Moving to next page...")
            try:
                self._next_page()
                page += 1
                time.sleep(random.uniform(2, 4))
            except Exception as e:
                logger.warning(f"Failed to go to next page or reached end: {e}")
                break

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
            next_btn = self.page.ele('xpath://button[contains(text(), "下一页")]') or \
                       self.page.ele('text:下一页') or \
                       self.page.ele('.btn-next') # Hypothetical class | 假设的类名

            if next_btn:
                # Scroll into view if needed? DrissionPage click handles it usually. | 如果需要，滚动到视图中？DrissionPage 点击通常会处理。
                next_btn.click()
            else:
                logger.warning("Next page button not found. Attempting scroll-to-bottom fallback.")
                self.page.scroll.to_bottom()
                # If infinite scroll, scrolling trigger new api call. | 如果是无限滚动，滚动会触发新的 API 调用。
            
            # Anti-scraping random wait | 反爬虫随机等待
            wait_time = random.uniform(3, 5)
            logger.debug(f"Action taken. Waiting {wait_time:.2f}s...")
            time.sleep(wait_time)
            
            self._check_captcha()

        except CaptchaDetectedException:
            raise
        except Exception as e:
            logger.error(f"Error during pagination: {e}")
            # Don't strictly crash on navigation error, maybe just stop? 
            # But prompt assumes valid pagination handling.
            raise e

    def _check_captcha(self):
        """
        Check for captcha elements and attempt to solve them. / 检查验证码元素并尝试解决。
        """
        # Search for captcha elements | 搜索验证码元素
        slide_verify = self.page.ele('.slide-verify', timeout=1)
        if slide_verify:
            logger.warning("Captcha detected! Attempting to solve...")
            try:
                # Find specific elements within the captcha modal
                # These selectors are common for XHS but might need refinement
                bg_img = self.page.ele('.captcha-background') # Placeholder selector
                slice_img = self.page.ele('.captcha-slice')    # Placeholder selector
                slider_knob = self.page.ele('.slider-knob')   # Placeholder selector
                
                if bg_img and slice_img and slider_knob:
                    bg_url = bg_img.attr('src')
                    slice_url = slice_img.attr('src')
                    self.solver.solve(self.page, slider_knob, bg_url, slice_url)
                    logger.success("Captcha solved successfully.")
                    time.sleep(2) # Wait for dismissal
                else:
                    logger.error("Found captcha but couldn't find all required elements (bg, slice, knob).")
                    raise CaptchaDetectedException("Captcha detected but missing elements.")
            except Exception as e:
                logger.error(f"Failed to solve captcha: {e}")
                raise CaptchaDetectedException(f"Captcha solving failed: {e}")

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
                    # e.g. Timeout or other issue, just continue to check stop_event
                    packet = None
                
                if not packet:
                    continue

                # Log all captured URLs for debugging | 记录所有捕获的 URL 以供调试
                logger.debug(f"Listener captured: {packet.url}")

                # Relaxed filter for search results | 放宽搜索结果的过滤条件
                if 'search/notes' in packet.url:
                    logger.info(f"Processing search packet: {packet.url}")
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
                            data = res_body.get('data', {})
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

    @retry(stop=stop_after_attempt(3), wait=wait_fixed(2))
    def scrape_note_detail(self, note_id: str, xsec_token: str = None) -> Dict[str, Any]:
        """
        Scrape detail of a specific note, including title, content, images and comments.
        抓取特定笔记的详情，包括标题、内容、图片和评论。
        
        Args:
            note_id: The ID of the note. / 笔记 ID。
            xsec_token: Optional security token for accessing the note. / 可选的安全令牌。
            
        Returns:
            Dict containing scraped data. / 包含抓取数据的字典。
        """
        # Construct URL with xsec_token if available
        if xsec_token:
            url = f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token={xsec_token}&xsec_source=pc_search"
        else:
            url = f"https://www.xiaohongshu.com/explore/{note_id}"
        logger.info(f"Navigating to note detail: {url}")
        self.page.get(url)
        
        self.check_and_wait_for_login()
        
        # Wait for content to load
        self.page.wait.load_start()
        time.sleep(2)

        # Anti-scraping check: Check for redirect to generic explore feed or "unavailable" message
        # 反爬检查：检查是否重定向到通过发现页或显示“无法浏览”
        if self.page.url == "https://www.xiaohongshu.com/explore" or \
           "explore" in self.page.url and note_id not in self.page.url:
            logger.warning(f"Redirected to Explore Feed instead of Note {note_id}. Likely login wall.")
            # Try reloading cookies
            self._load_cookies()
            self.page.refresh()
            time.sleep(3)
            if "explore" in self.page.url and note_id not in self.page.url:
                 logger.error("❌ Login wall persists after reload.")
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
            note_container = self.page.ele('.note-container') or self.page.ele('.main-container') or \
                             self.page.ele('#detail-container') or self.page.ele('.side-bar-container') or \
                             self.page.ele('tag:body')
            
            # 1. Scrape Title | 抓取标题
            title_ele = note_container.ele('#detail-title') or note_container.ele('.title') or \
                        note_container.ele('.note-detail-title') or note_container.ele('h1')
            data['title'] = title_ele.text if title_ele else ""
            
            # 2. Scrape Description | 抓取描述
            desc_ele = note_container.ele('#detail-desc') or note_container.ele('.desc') or \
                       note_container.ele('.note-text')
            data['desc'] = desc_ele.text if desc_ele else ""
            
            # 3. Scrape Images | 抓取图片
            # Use broader container search then filter
            media_container = note_container.ele('.media-container') or \
                              note_container.ele('.note-slider-img') or \
                              note_container.ele('.image-container') or \
                              note_container
            
            # Find all images in the media area
            imgs = media_container.eles('tag:img')
            for img in imgs:
                src = img.attr('src')
                # Strict check for SNS images to avoid avatars/icons
                # Filter out 'avatar', 'head', and ensure it looks like a content image
                if src and ('sns-web-img' in src or 'sns-img' in src or 'sns-search' in src) and \
                   'avatar' not in src and '/head/' not in src:
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
            
            for i in range(3):
                if scroll_container != self.page:
                    try:
                        scroll_container.run_js('this.scrollTop += 1000')
                    except:
                        self.page.scroll.down(1000)
                else:
                    self.page.scroll.down(1000)
                time.sleep(1)
            
            comment_eles = note_container.eles('.comment-item')
            for c_ele in comment_eles:
                # User
                user_ele = c_ele.ele('.name') or c_ele.ele('.nickname') or c_ele.ele('.user-name')
                user = user_ele.text.strip() if user_ele else "Unknown"
                
                # Content
                content_ele = c_ele.ele('.content') or c_ele.ele('.comment-content') or c_ele.ele('.note-text')
                content = content_ele.text.strip() if content_ele else ""
                
                if content:
                    data['comments'].append({
                        "user": user,
                        "content": content
                    })
                    
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
            date_ele = scope.ele('.date') or \
                       scope.ele('.publish-date') or \
                       scope.ele('.bottom-container .time') or \
                       self.page.ele('css:span[class*="date"]')
            
            if date_ele:
                text = date_ele.text.strip()
                # Clean up "发布于 " prefix if present inside the text (sometimes it's separate)
                return text.replace('发布于', '').replace('编辑于', '').strip()
            
        except Exception:
            pass
        return "" 
    
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
