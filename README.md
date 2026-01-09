English | [简体中文](./README.zh-CN.md)

# RedNote Drission Crawler (`XHSCrawler`)

A "RedNote (RedNote/Xiaohongshu) Learning Crawler" module based on [DrissionPage](https://gitee.com/g1879/DrissionPage). The core objective is to stably scrape **search page data** and **note details (including images/comments)**, encapsulating engineering details such as "login/cookie/pagination/request jitter" into a single class for easy learning and secondary development.

> This module focuses on the learning and research of "crawler collection technology".

## What You Can Do With It

- **Keyword Search Collection**: Listen to `/api/sns/web/v1/search/notes` based on browser network traffic, continuously turning pages to output note items (Generator).
- **Scrape Note Details**: Open the note detail page, extract title, content, publish time, author nickname, and image URL list.
- **Scrape Note Comments (Level 1 & Partial Level 2)**: Parse `.comment-item` by scrolling the comment area; attempts **single expansion** for threaded replies on top comments or specific keywords.
- **Login/Session Persistence**: Support `cookies.json` and browser `user_data_path` (profile) persistence; pause and wait for manual login when encountering a login popup.

## What Is Not Included (Important)

- **Do NOT download images or save to database**: `XHSCrawler` only returns structured data/URLs. Image downloading and JSON/SQLite storage belong to your own "Storage Layer".
- **Partial Comments Only**: Scrapes only currently loaded L1 comments (scrolled twice) and their expanded sub-comments. **Now includes** Comment ID, Date, Like Count, and Location (if available).
- **Support Level 2 comments (Threaded replies)**: Currently supports **single expansion** (clicks "Expand replies" once), recursive multiple expansion is not yet implemented.

## Installation

In this repository:
```bash
pip install -r requirements.txt
```

If you package `crawler/` separately for release (refer to `crawler/pyproject.toml`), install it by your package name.

## Quick Start (Minimal Usable)

```python
from crawler import XHSCrawler

crawler = XHSCrawler(
    headless=False,          # Recommended False for the first time to facilitate scan code/manual login
    port=9222,               # Avoid port conflict
    cookie_path="cookies.json",  # Optional: Explicitly enable cookies persistence (be careful not to commit/share this file)
)

try:
    # 1) Search: Output "note items" one by one (from search/notes API)
    for item in crawler.start_search_crawling(keyword="Python", start_page=1, sort_type="general"):
        note_id = item.get("id") or item.get("note_id")
        card = item.get("note_card", {}) or {}
        title = card.get("display_title") or card.get("title") or ""
        print(note_id, title)
        break  # demo: only take the first one

    crawler.stop()

    # 2) Detail: Title/Content/Images/Comments (Level 1)
    detail = crawler.scrape_note_detail(note_id)
    print(detail["title"], len(detail["images"]), len(detail["comments"]))
finally:
    crawler.close()  # Close browser
```

## API Overview

### Initialization Parameters

`XHSCrawler(headless=False, user_data_path=None, port=9222, cookie_path=None, global_cookie_path=None, proxy=None)`

- `headless`: Headless mode; recommended `False` for the first time (headless is inconvenient when manual login is required).
- `user_data_path`: Browser profile path (recommended separate directory for each account/task).
- `port`: Chromium debug port; must distinguish when running multiple crawlers in parallel.
- `cookie_path`: Task Cookie file path (optional). To avoid accidentally sharing sensitive information, default **DO NOT read/write** any `cookies*.json`; explicitly pass path if persistence is needed.
- `global_cookie_path`: Global Cookie backup/sync path (optional).
- `proxy`: Browser proxy (e.g., `http://user:pass@ip:port`).

### Common Methods

- `start_search_crawling(...) -> Generator[dict]`: Search and turn pages, continuously output "note item" in search results.
- `scrape_note_detail(note_id, xsec_token=None) -> dict`: Scrape detail page (including image URLs and level-1 comments).
- `check_login_status()` / `check_and_wait_for_login()`: Check and handle login status (wait if login popup appears).
- `stop(save_cookies=True)`: Stop loop (does not simple auto close browser).
- `close()`: Exit browser (recommended in `finally`).

## Search Result Data Description (`start_search_crawling` Output)

The `item` produced by `start_search_crawling` comes from `data.items[]` of the RedNote search API response, commonly used fields are usually in:

- `item["id"]`: Note ID (recommended priority).
- `item["note_card"]`: Card information (title/cover/author/interaction etc., specific fields vary with site version).
- `item["_page"]`: Attached by this crawler, indicating which page the result comes from (convenient for resuming/debugging).

Supported search parameters:

- `sort_type`: `general` (General), `popularity_desc` (Hottest), `time_desc` (Newest)
- `time_range`: `0` All, `1` Last 1 Day, `2` Last 1 Week, `4` Last 6 Months (Note: `3` removed)
- `search_scope`: `0` All, `1` Viewed, `2` Not Viewed, `3` Followed
- `location_distance`: `0` All, `1` Same City, `2` Nearby
- `note_type`: `0` All, `1` Video, `2` Image (internally determined based on `note_card.type == "video"`)

## Detail Data Description (`scrape_note_detail` Output)

Return structure (fields may be missing depending on page structure):

- `note_id`: Note ID
- `title`: Title (with multi-level fallback: DOM -> `og:title` -> page title)
- `desc`: Content/Description
- `images`: Image URL list (deduplicated)
- `comments`: List of L1 comments: `[{ "id": "...", "user": "...", "content": "...", "date": "...", "like_count": "...", "ip_location": "..." }, ...]`
- `published_at`: Publish time (best effort extraction from page)
- `share_url`: Share URL (base link without token)

About comments:

- Current implementation loads the comment area by scrolling, then scrapes text of `.comment-item`.
- **Only covers a loaded segment of level-1 comments (dynamic scrolling)**; not guaranteed full amount.
- **Supports partial Level 2 comments/Threaded replies**: Currently implements "Single Expansion" (expands replies once per comment), no recursive pagination.

## How to Store Locally (JSON / Images)

### 1) Save as JSON (Recommended do this first)

```python
import json
from pathlib import Path
from crawler import XHSCrawler

out_dir = Path("data")
out_dir.mkdir(parents=True, exist_ok=True)

crawler = XHSCrawler(headless=False)
try:
    detail = crawler.scrape_note_detail("YOUR_NOTE_ID")
    (out_dir / f"{detail['note_id']}.json").write_text(
        json.dumps(detail, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
finally:
    crawler.close()
```

### 2) Download Images (Optional)

`images` returns URLs; you can decide whether to download and how to name them.

If you want to **save disk space**, you can choose to scale images to lower resolution locally and re-encode (e.g., `max_side=1280`, or save as `webp`). This saves storage space; but if you download original images first then scale, **it will not save bandwidth**.

Install optional dependencies:

```bash
pip install -r requirements-images.txt
```

```python
import os
import requests
from urllib.parse import urlparse

def download_images(note_id: str, urls: list[str], root: str = "images"):
    os.makedirs(os.path.join(root, note_id), exist_ok=True)
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.xiaohongshu.com/",
    })
    for i, url in enumerate(urls):
        name = os.path.basename(urlparse(url).path) or f"{i}.jpg"
        path = os.path.join(root, note_id, f"{i:02d}_{name}")
        r = sess.get(url, timeout=30)
        r.raise_for_status()
        with open(path, "wb") as f:
            f.write(r.content)
```

Tip: Images may have anti-hotlinking/403 issues, need to adjust headers based on your environment.

## Login & Cookie (Why "Manual Login" is often required)

RedNote will show login popups or redirect to "Unable to browse/Security restriction" in unlogged/risk control scenarios. The strategy of this crawler is:

- When you configure `cookie_path/global_cookie_path`: Priority read `cookie_path` (then try `global_cookie_path`, `cookies_backup.json`);
- If still judged as not logged in, **pause and wait for you to manually login/scan code**;
- Cookies will be automatically saved upon successful login (and can be synced to global path).

Common advice:

- Run `headless=False` for the first time, complete one login and generate usable cookies;
- Production/Long-term running can use `user_data_path` to fix profile, reducing frequent logins;
- Port conflict/browser zombie processes cause profile locking; this crawler will create temporary profile fallback in `profiles/_tmp_sessions` when necessary.

## Pre-Open Source Self-Check (Prevent accidental sharing of cookies)

Recommended to confirm no sensitive running artifacts in warehouse before release/packaging:

```bash
find . -maxdepth 3 \( -name 'cookies*.json' -o -name 'profiles' -o -name '*.db' -o -name 'logs' \) -print
```

This directory already provides `crawler/.gitignore` and `crawler/MANIFEST.in` to reduce risk of accidental commit/packaging.

## Limitations & Roadmap

Known limitations:

- Comments currently scrape level-1 and **single-step expanded level-2 replies**, not guaranteed full amount.
- Search interface/page structure changes, some fields might not be captured, need to adjust selector according to actual page.

Roadmap (PR/Discussion welcome):

- [x] Support **Level 2 comments (Threaded replies)** (Single expansion only)
- [ ] Recursive comment expansion and full pagination scrolling
- [x] Complete comment structure: ID, Date, Like Count, Location (Implemented)
- [ ] More stable "Interface Level" comment scraping (Prioritize network interface over DOM parsing)
- [ ] Video note detail parsing and media link extraction

## Buy me a Coffee ☕️

**Code by a Girl.** 👩🏻‍💻

If this project helps you, please consider buying me a coffee! Your support powers my project maintenance.

- **🇨🇳 China**: WeChat Pay / Alipay
- **🌍 International**: PayPal (Standard choice for mainland devs) or USDT

<!-- ![Payment QR Code](qrcode.jpg) -->
<!-- PayPal.Me/YourName -->

## Disclaimer

This tool is for educational and research purposes only. Please comply with the target website's terms of service and laws and regulations, respect privacy and copyright.
Strictly prohibited to use this tool for any illegal purposes (such as large-scale data scraping, account abuse, etc.).

## Technical Review (2026-01-07): Level 2 Comment Scraping & DOM Pitfalls

This project spent a long time debugging when implementing "Level 2 Comment (Sub-comment) Expansion & Scraping", recording the following Critical Path & Pitfalls for future developers:

1.  **Sibling Node Pitfall (Flat DOM Structure)**
    - **Phenomenon**: Cannot find `Expand x replies` button or sub-comment DOM inside `.comment-item` no matter what.
    - **Truth**: RedNote Web side renders level-2 comment container `.reply-container` as **Sibling** of level-1 comment `.comment-item` for performance (list virtualization optimization), not child node.
    - **Lesson**: Do not take it for granted that sub-comments are inside parent comments. Must check `element.next()` or flat list of parent container.

2.  **Ambiguity of Text Selectors**
    - **Issue**: Using `text:Reply` attempts to locate "Expand Reply" button.
    - **Result**: Crawler frequently mis-touched "Reply (Write Comment)" button, causing input box popup instead of expanding list, and log misreported "Click Success".
    - **Fix**: Use more explicit `text:replies` or regex match numbers. In UI interaction complex scenarios, fuzzy text selectors are huge hidden dangers.

3.  **Importance of Debug Visualization**
    - **Pain Point**: Cannot confirm click effect with naked eye in Headless mode, and `page.html` is often truncated because it is too large.
    - **Breakthrough**: Print **Critical Elements and their Sibling Nodes** `outerHTML` directly to stdout (`logger.error` wrapping before log truncation) to precisely locate DOM changes. For dynamically loaded content, Raw HTML Dump is better than screenshots (convenient to search Class names).
