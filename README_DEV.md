English | [简体中文](crawler_development_wiki.zh-CN.md)

# Antigravity XHS Crawler Development Guide

This document summarizes the process, technical architecture, and key implementation details of developing the **Xiaohongshu (RedNote)** crawler system using **Antigravity (Agentic AI)**. It aims to provide a learning reference for future developers.

## 1. Project Overview

This project is an automated data collection system for the Xiaohongshu platform. The core objective is to collect notes and comments under specific keywords (e.g., "Makeup", "Skincare") and provide structured data support for subsequent analysis.

### Tech Stack

-   **Language**: Python 3.10+
-   **Browser Automation**: [DrissionPage](https://gitee.com/g1879/DrissionPage) (ChromiumPage mode) - *Chosen for its advantages in Anti-Detection and low-level browser control compared to Selenium/Playwright.*
-   **Database**: SQLite + [SQLModel](https://sqlmodel.tiangolo.com/) (ORM)
-   **Frontend**: (Optional) HTML/JS Dashboard for monitoring.
-   **Frontend**: (Optional) HTML/JS Dashboard for monitoring.

## 2. Core Architecture

The system consists of the following modules:

1.  **Crawler Core (`crawler/xhs_crawler.py`)**: Responsible for controlling browser behavior, executing search, pagination, detail scraping, Cookie management, etc.
2.  **Data Models (`database/models.py`)**: Defines the database structure for `Note` and `Comment`.
3.  **Manager (`crawler/crawler_manager.py`)**: (Logic Layer) Schedules crawler tasks and handles exception retries.
4.  **External Processor**: Data can be exported or provided to independent analysis modules via API.

## 3. Key Features & Implementation Details

### 3.1 Request Robustness

`XHSCrawler` implements basic request control:
-   **Basic Jitter**: `_sleep_with_jitter` implements simple random waiting to avoid fixed-frequency request patterns.
-   **Timeout & Retry**: Explicit timeouts are set for network requests and key element lookups to prevent the crawler from hanging indefinitely.

### 3.2 Robust Login Detection Mechanism
-   **Strict Login Check**: Mandatorily checks for avatar/username elements to prevent false positives (guest mode).
-   **Login Wall Handling**: Automatically detects `302 Redirect` or modal popups and raises exceptions for manual intervention.

### 3.3 Deep Data Collection
-   **Search & Pagination**: Supports infinite scrolling.
-   **Detail Parsing**: Extracts high-res images, content, publish time, etc.
-   **Sub-comments**: Handles the "Sibling Node Trap" mentioned earlier to ensure level-2 comments are expanded and captured.

## 4. How We Built This with Antigravity (The Antigravity Journey)

This project is a typical result of an **AI-Native** development workflow. The User (Human) and AI Agent (Antigravity) pair-programmed extensively:

1.  **Agentic Mode**:
    -   Antigravity didn't just answer questions but took over tasks as an "independent developer". It proactively created `task.md` to plan progress and maintained `implementation_plan.md` for design.
    -   For example, during the crawler architecture refactoring, the AI proactively proposed "Scheme A" (Inheritance) and automatically executed file moves, code cleanup, and dependency updates.

2.  **Debug Loop**:
    -   When CSS selectors failed, Antigravity wrote disposable `debug_xhs.py` scripts to capture the current page HTML Dump for analysis and fixed the selectors, instead of asking the user to manually test repeatedly.
    -   When solving the "Sub-comment" scraping challenge, the AI suggested printing `outerHTML` to observe minute DOM structure changes (like the discovery of sibling nodes).

3.  **Docs as Code**:
    -   All documentation (including this one) was drafted, translated (EN/CN), and maintained by the AI based on code changes. The AI ensured synchronization between documentation and code implementation (e.g., removing descriptions of obsolete features).

### Major Challenges & Solutions

1.  **Challenge**: Crawler sometimes stuck in "Not Logged In" state, but Cookie was actually valid.
    -   **Fix**: Discovered that checking Cookie expiration wasn't enough; strict DOM element checks were necessary. We introduced `_detect_security_restriction` to identify account-specific risk control pages.

2.  **Challenge**: `DrissionPage` element location failed in some environments.
    -   **Fix**: Mixed use of CSS Selector and XPath, adding Shadow DOM penetration where applicable.

## 5. Best Practices Summary

For developers wishing to extend this project:

1.  **Debug First**: When encountering anti-climbing or parsing errors, write a small `debug_*.py` script to reproduce the issue first. Don't guess blindly in the main flow.
2.  **Raw Data Storage**: It is recommended to save scraped data directly into SQLite/JSON (Raw Data) without excessive cleaning or business processing. This maximizes the preservation of original information for future use.
3.  **Logging**: Keep detailed `loguru` logs, especially network requests and state changes, critical for troubleshooting "Ghost Bugs" (sporadic issues).
4.  **Respect Rules**: Strictly control scrape frequency (Sleep interval) to avoid putting pressure on the target site and getting IP banned.
