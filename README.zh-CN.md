[English](./README.md) | 简体中文

# RedNote Drission Crawler (`XHSCrawler`)

一个基于 [DrissionPage](https://gitee.com/g1879/DrissionPage) 的“小红书（RedNote）学习型爬虫”模块：核心目标是把**搜索页数据**与**笔记详情（含图片/评论）**稳定采出来，并把“登录/Cookie/翻页/随机请求间隔”等工程细节封装到一个类里，方便学习与二次开发。

> 本模块专注于“爬虫采集技术”的学习与研究。

## 你能用它做什么

- **关键词搜索采集**：基于浏览器网络监听抓 `/api/sns/web/v1/search/notes`，持续翻页产出笔记条目（Generator）。
- **抓笔记详情**：打开笔记详情页，提取标题、正文、发布时间、作者昵称、图片 URL 列表。
- **抓笔记评论（一级与部分二级）**：通过滚动加载评论区，解析页面上的 `.comment-item`；对于热评或包含特定关键词的评论，尝试**单次展开**二级回复。
- **登录/会话持久化**：支持 `cookies.json` 与浏览器 `user_data_path`（profile）持久化；遇到登录弹窗会暂停等待人工登录。

## 不包含什么（重要）

- **不下载图片、不存数据库**：`XHSCrawler` 只返回结构化数据/URL。图片下载、JSON/SQLite 落库属于你自己的“存储层”。
- **不保证全量评论**：抓取页面已加载的一级评论（自动根据评论数动态调整滚动次数，支持通过程序参数 `max_scrolls` 设定上限）及其展开的子评论。**现已包含**评论 ID、时间、点赞数、IP 属地等字段（若页面存在），但部分字段仍可能缺失。
- **支持二级评论（楼中楼）**：目前已实现**单次展开**（点击一次“展开回复”），但暂未实现递归多次展开以获取超过首页加载量的子评论。

## 安装

在本仓库中使用：
```bash
pip install -r requirements.txt
```

如果你把 `crawler/` 单独打包发布（参考 `crawler/pyproject.toml`），再按你的包名安装即可。

## 快速开始（最小可用）

```python
from crawler import XHSCrawler

crawler = XHSCrawler(
    headless=False,          # 首次建议 False，方便扫码/手动登录
    port=9222,               # 避免端口冲突
    cookie_path="cookies.json",  # 可选：显式开启 cookies 落盘（注意不要提交/分享该文件）
)

try:
    # 1) 搜索：逐条产出“笔记条目”（来自 search/notes 接口）
    for item in crawler.start_search_crawling(keyword="Python", start_page=1, sort_type="general"):
        note_id = item.get("id") or item.get("note_id")
        card = item.get("note_card", {}) or {}
        title = card.get("display_title") or card.get("title") or ""
        print(note_id, title)
        break  # demo: 只取第一条

    crawler.stop()

    # 2) 详情：标题/正文/图片/评论（一级）
    detail = crawler.scrape_note_detail(note_id)
    print(detail["title"], len(detail["images"]), len(detail["comments"]))
finally:
    crawler.close()  # 关闭浏览器
```

## API 一览

### 初始化参数

`XHSCrawler(headless=False, user_data_path=None, port=9222, cookie_path=None, global_cookie_path=None, proxy=None)`

- `headless`：无头模式；首次建议 `False`（需要人工登录时无头不方便）。
- `user_data_path`：浏览器 profile 路径（建议每个账号/任务独立一个目录）。
- `port`：Chromium 调试端口；并行多个 crawler 时必须区分。
- `cookie_path`：任务 Cookie 文件路径（可选）。为了避免误分享敏感信息，默认**不读写**任何 `cookies*.json`；如需持久化登录态请显式传入路径。
- `global_cookie_path`：全局 Cookie 备份/同步路径（可选）。
- `proxy`：浏览器代理（例：`http://user:pass@ip:port`）。

### 常用方法

- `start_search_crawling(...) -> Generator[dict]`：搜索并翻页，持续产出搜索结果里的“note item”。
- `scrape_note_detail(note_id, xsec_token=None) -> dict`：抓详情页（含图片 URL 与一级评论）。
- `check_login_status()` / `check_and_wait_for_login()`：检查并处理登录状态（出现登录弹窗会等待）。
- `stop(save_cookies=True)`：停止循环（不会自动关闭浏览器）。
- `close()`：退出浏览器（建议放在 `finally`）。

## 搜索结果数据说明（`start_search_crawling` 输出）

`start_search_crawling` 产出的 `item` 来自小红书搜索接口响应的 `data.items[]`，其中常用字段通常在：

- `item["id"]`：笔记 ID（推荐优先用它）。
- `item["note_card"]`：卡片信息（标题/封面/作者/互动等，具体字段随站点版本变化）。
- `item["_page"]`：由本 crawler 额外附加，表示该条结果来自第几页（便于断点续爬/排查）。

支持的搜索参数：

- `sort_type`：`general`（综合）、`popularity_desc`（最热）、`time_desc`（最新）
- `time_range`：`0` 全部、`1` 近 1 天、`2` 近 1 周、`4` 近 6 月（注：原 `3` 已移除）
- `search_scope`：`0` 全部、`1` 已看过、`2` 未看过、`3` 已关注
- `location_distance`：`0` 全部、`1` 同城、`2` 附近
- `note_type`：`0` 全部、`1` 视频、`2` 图文（内部基于 `note_card.type == "video"` 判断）

## 详情数据说明（`scrape_note_detail` 输出）

返回结构（字段可能随页面结构变化而空缺）：

- `note_id`：笔记 ID
- `title`：标题（带多级 fallback：DOM -> `og:title` -> 页面 title）
- `desc`：正文/描述
- `images`：图片 URL 列表（去重后）
- `comments`：一级评论列表：`[{ "id": "...", "user": "...", "content": "...", "date": "...", "like_count": "...", "ip_location": "..." }, ...]`
- `published_at`：发布时间（尽力从页面提取）
- `share_url`：分享 URL（不带 token 的基础链接）

关于评论：

- 当前实现通过滚动让评论区加载，然后抓取 `.comment-item` 的文本。
- **只覆盖已加载的一段一级评论（默认动态调整滚动次数）**；不保证全量。
- **支持部分二级评论**（见 Roadmap）：目前实现“单次展开”，即每条热评至多展开一页子评论，未递归翻页。

## 如何在本地存储（JSON / 图片）

### 1) 保存为 JSON（推荐先这样做）

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

### 2) 下载图片（可选）

`images` 返回的是 URL；你可以自行决定是否下载与如何命名。

如果你想**节省磁盘空间**，可以选择在本地把图片缩放到较低分辨率并重编码（例如 `max_side=1280`，或保存为 `webp`）。这会节省存储空间；但如果你先下载原图再缩放，**不会节省带宽**。

安装可选依赖：

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

提示：图片可能存在防盗链/403 等情况，需要按你的环境调整 headers。

## 登录与 Cookie（为什么经常“必须手动登录”）

小红书在未登录/风控场景下会出现登录弹窗或跳转“无法浏览/安全限制”。本 crawler 的策略是：

- 当你配置了 `cookie_path/global_cookie_path` 时：优先读取 `cookie_path`（再尝试 `global_cookie_path`、`cookies_backup.json`）；
- 若仍被判定未登录，则**暂停等待你手动登录/扫码**；
- 登录成功会自动保存 cookies（并可同步到全局路径）。

常见建议：

- 首次运行 `headless=False`，完成一次登录并生成可用 cookies；
- 生产/长期运行可以使用 `user_data_path` 固定 profile，减少频繁登录；
- 端口冲突/浏览器僵尸进程会导致 profile 锁定；本 crawler 会在必要时用 `profiles/_tmp_sessions` 创建临时 profile 兜底。

## 开源发布前自检（防止误分享 cookies）

建议在发布/打包前确认仓库内不存在敏感运行产物：

```bash
find . -maxdepth 3 \( -name 'cookies*.json' -o -name 'profiles' -o -name '*.db' -o -name 'logs' \) -print
```

本目录已提供 `crawler/.gitignore` 与 `crawler/MANIFEST.in` 来降低误提交/误打包的风险。

## Limitations & Roadmap

已知限制：

- 评论目前抓取一级评论及**单次展开的二级评论**，不保证全量（只抓页面加载及展开的部分）。
- 搜索接口/页面结构会变，部分字段可能抓不到，需要你按实际页面调整选择器。

Roadmap（欢迎 PR/讨论）：

- [x] 支持**二级评论（楼中楼）**（目前仅单次展开）
- [ ] 评论分页/滚动全量抓取与递归展开
- [x] 评论结构补全：评论 ID、时间、点赞数、IP 属地（已实现）
- [ ] 更稳定的“接口级”评论抓取（优先网络接口而不是 DOM 解析）
- [ ] 视频笔记详情解析与媒体链接提取

## 请作者喝杯咖啡 / Buy me a Coffee ☕️

如果本项目对你有所帮助，欢迎打赏支持！你的支持是我维护项目的动力。

- **🇨🇳 China**: WeChat Pay / Alipay (微信/支付宝)
![QR Code](qrcode/Alipay.jpg)

![QR Code](qrcode/WeChatPay.jpg)

- **🌍 International**: PayPal (Standard choice for mainland devs) or USDT

<!-- ![Payment QR Code](qrcode.jpg) -->
<!-- PayPal.Me/YourName -->

## 免责声明

本工具仅供教育和研究目的使用。请遵守目标网站的服务条款与法律法规，尊重隐私与版权。
严禁将本工具用于任何非法用途（如大规模数据抓取、账号滥用等）。

## 技术复盘（2026-01-07）：二级评论抓取与 DOM 陷阱

该项目在实现“二级评论（子评论）展开与抓取”时曾耗时较长进行调试，记录以下 Critical Path 与踩坑经验，供后续开发者参考：

1.  **兄弟节点陷阱（Flat DOM Structure）**
    - **现象**：在 `.comment-item` 内部死活找不到 `展开 x 条回复` 按钮，也找不到子评论 DOM。
    - **真相**：小红书 Web 端为了性能（列表虚拟化优化），将二级评论容器 `.reply-container` 渲染为一级评论 `.comment-item` 的**兄弟节点（Sibling）**，而非子节点。
    - **教训**：不要想当然地认为子评论就在父评论内部。必须检查 `element.next()` 或父级容器的扁平列表。

2.  **文本选择器的歧义性**
    - **问题**：使用 `text:回复` 试图定位“展开回复”按钮。
    - **结果**：爬虫频繁误触“回复（写评论）”按钮，导致弹出输入框而非展开列表，且日志误报“点击成功”。
    - **修正**：使用更明确的 `text:条回复` 或正则匹配数字。在 UI 交互复杂的场景下，模糊文本选择器是巨大的隐患。




