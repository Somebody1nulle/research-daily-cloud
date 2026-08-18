"""每日智库：8家英文政策智库扫描 → 中文清单写入飞书
- RSS 优先，无 RSS 的站（Bridgewater/Economic Club）抓列表页
- 窗口：前一天 00:00 ~ 今天 09:00（北京时间）
- 去重：以两张表已存在的「链接」为准，重跑不产生重复行
"""
import re
import json
import requests
import feedparser
from datetime import datetime, timedelta

from src.main import CST, today_cst, date_str, field_to_date

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"}

LLM_PROMPT = """你是政策研究编辑。下面是今天从 8 家英文政策智库抓到的新文章（JSON 数组，含 title/url/summary/source/published_at）。
请为每篇生成中文处理结果：
- zh_title: 中文标题（信达雅，保留关键专名）
- zh_summary: 中文摘要 2-3 句
- topics: 主题标签，从 {topics} 中选，可多选
- relevance: 相关度 1-5，评分规则：{rules}

文章列表：
{articles}

严格输出 JSON 对象 {{"items": [...]}}，items 与输入一一对应（含 url 原样保留），不要输出任何其他文字。"""


def parse_date(entry):
    """从 feed entry 提取发布日期"""
    for key in ("published_parsed", "updated_parsed"):
        t = getattr(entry, key, None)
        if t:
            from time import mktime
            return datetime.fromtimestamp(mktime(t), tz=CST)
    return None


def _discover_feed(home_url):
    """RSS 404 时从首页 <link rel="alternate" type="application/rss+xml"> 自动发现真实 feed 地址"""
    try:
        r = requests.get(home_url, headers=UA, timeout=(8, 15))
        r.raise_for_status()
        m = re.search(r'<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]*>', r.text, re.I)
        if not m:
            return None
        tag = m.group(0)
        hm = re.search(r'href=["\']([^"\']+)["\']', tag, re.I)
        if not hm:
            return None
        from urllib.parse import urljoin
        return urljoin(home_url, hm.group(1))
    except Exception:
        return None


def fetch_rss(source, window_start, window_end):
    """抓 RSS，只保留窗口内条目；无发布时间的条目丢弃（用户要求：只要昨日更新）。
    feed 404 时自动从首页发现真实 RSS 地址（智库站常改 feed 路径）"""
    try:
        urls = [source["feed_url"]] + source.get("feed_candidates", [])
        r, used = None, None
        for u in urls:
            try:
                r = requests.get(u, headers=UA, timeout=(8, 20))
                r.raise_for_status()
                used = u
                break
            except Exception:
                r = None  # 失败时清空，避免误用坏响应
                continue
        if r is None:
            # 全部候选失败 → 首页自动发现
            from urllib.parse import urlparse
            home = f"{urlparse(urls[0]).scheme}://{urlparse(urls[0]).netloc}"
            alt = _discover_feed(home)
            if not alt:
                raise RuntimeError(f"全部候选 feed 均不可达（{len(urls)} 个）")
            r = requests.get(alt, headers=UA, timeout=(8, 20))
            r.raise_for_status()
            used = alt
            print(f"    [{source['name']}] 首页发现 feed: {alt[:70]}")
        elif used != urls[0]:
            print(f"    [{source['name']}] 主 feed 失效，候选生效: {used[:70]}")
        assert r is not None
        feed = feedparser.parse(r.content)
        total = len(feed.entries)
        items = []
        for e in feed.entries:
            pub = parse_date(e)
            if pub is None:
                continue  # 无日期不收
            if not (window_start <= pub < window_end):
                continue  # 窗口外（旧文/未来）不收
            summary = re.sub(r"<[^>]+>", "", getattr(e, "summary", "") or "")[:400]
            items.append({"title": e.get("title", ""), "url": e.get("link", ""),
                          "published_at": pub, "summary_raw": summary})
        # 诊断日志：区分「feed 空（反爬/格式错）」和「有更新但不在窗口」
        if total == 0:
            return [], f"feed 解析出 0 条目（可能反爬或格式错误，响应 {len(r.content)}B）"
        if not items:
            dates = [parse_date(e) for e in feed.entries[:5]]
            date_strs = [d.strftime("%m-%d %H:%M") if d else "无日期" for d in dates]
            return [], f"解析 {total} 条但窗口内 0 条（前5条日期: {', '.join(date_strs)}）"
        return items, None
    except Exception as ex:
        return [], str(ex)


def _parse_date_near(tag, path_inc=""):
    """从链接附近的 HTML 中解析日期。
    支持：<time datetime> 标签、数字格式(2026-08-17)、英文格式(August 17, 2026 / 17 Aug 2026)、
    相对日期(2 days ago)。
    向上爬层时若进入含多个文章链接的「列表容器」则停止——防止把别的文章的日期错配过来。"""
    from dateutil import parser as dup
    scope = tag
    for _ in range(4):  # 最多向上找4层父元素
        if scope is None:
            break
        # 多文章容器检测：本层含 ≥2 个文章链接 → 已逃出单篇文章卡片，停止
        if path_inc and len([a for a in scope.find_all("a", href=True)
                             if path_inc in str(a["href"])]) >= 2:
            return None
        t = scope.find("time")
        if t:
            for attr in ("datetime", "title"):
                if t.get(attr):
                    try:
                        return dup.parse(t[attr])
                    except Exception:
                        pass
            if t.get_text(strip=True):
                try:
                    return dup.parse(t.get_text(strip=True))
                except Exception:
                    pass
        text = scope.get_text(" ", strip=True)[:900]  # 长摘要卡片日期位置靠后，窗口放大
        # 数字格式：2026-08-17 / 2026/8/17 / 2026年8月17日
        m = re.search(r"(20\d\d[-/年]\d{1,2}[-/月]\d{1,2})", text)
        if m:
            try:
                return dup.parse(m.group(1).replace("年", "-").replace("月", "-").replace("日", ""))
            except Exception:
                pass
        # 英文格式：August 17, 2026 / Aug 17 2026 / 17 August 2026
        m = re.search(r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+20\d\d"
                      r"|\d{1,2}(?:st|nd|rd|th)?\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?,?\s+20\d\d)",
                      text, re.I)
        if m:
            try:
                return dup.parse(m.group(1))
            except Exception:
                pass
        # 相对日期：N days ago / N hours ago / yesterday
        m = re.search(r"(\d+)\s+(day|hour)s?\s+ago|yesterday", text, re.I)
        if m:
            from datetime import datetime, timedelta
            from src.main import CST as _CST
            now = datetime.now(_CST)
            if m.group(0).lower() == "yesterday":
                return now - timedelta(days=1)
            n, unit = int(m.group(1)), m.group(2).lower()
            return now - timedelta(**{"days" if unit == "day" else "hours": n})
        scope = scope.parent
    return None


def _extract_items_from_html(html, source, window_start, window_end):
    """从 HTML 中提取文章链接+日期，过滤出窗口内条目（requests/浏览器共用）"""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    items, seen = [], set()
    path_inc = source.get("path_include", "")
    for a in soup.find_all("a", href=True):
        href = str(a["href"])
        title = a.get_text(strip=True)
        if not title or len(title) < 15:
            continue
        if href.startswith("/"):
            from urllib.parse import urljoin
            href = urljoin(source["list_url"], href)
        if href in seen or not href.startswith("http"):
            continue
        if path_inc and path_inc not in href:
            continue
        if href.rstrip("/") == source["list_url"].rstrip("/"):
            continue
        seen.add(href)
        pub = _parse_date_near(a, path_inc)
        if pub is None:
            continue  # 无日期不收
        if pub.tzinfo is None:
            from src.main import CST as _CST
            pub = pub.replace(tzinfo=_CST)
        if not (window_start <= pub < window_end):
            continue  # 旧文不收
        items.append({"title": title, "url": href, "published_at": pub, "summary_raw": ""})
        if len(items) >= 15:
            break
    return items


def _fetch_browser(source, window_start, window_end):
    """Playwright 无头浏览器渲染列表页（应对 JS 渲染/反爬页面），按需懒加载。
    优先用 runner 预装的 Google Chrome（channel=chrome，省下载），失败退回内置 Chromium。"""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome",
                                        args=["--no-sandbox", "--disable-dev-shm-usage"])
        except Exception:
            browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            page = browser.new_page(user_agent=UA["User-Agent"])
            page.goto(source["list_url"], wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4000)  # 等 JS 渲染出列表
            html = page.content()
        finally:
            browser.close()
    _debug_dump_cards(html, source, "浏览器")
    return _extract_items_from_html(html, source, window_start, window_end)


def _parse_date_from_article_page(html):
    """从文章详情页提取发布日期：meta 标签 → time 标签 → 正文前部英文日期"""
    from bs4 import BeautifulSoup
    from dateutil import parser as dup
    soup = BeautifulSoup(html, "html.parser")
    # 1. meta 标签（最可靠）
    for attrs in ({"property": "article:published_time"}, {"name": "date"},
                  {"name": "publish_date"}, {"name": "dc.date"}, {"itemprop": "datePublished"}):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            try:
                return dup.parse(tag["content"])
            except Exception:
                pass
    # 2. time 标签
    t = soup.find("time")
    if t:
        for v in (t.get("datetime"), t.get("title"), t.get_text(strip=True)):
            if v:
                try:
                    return dup.parse(v)
                except Exception:
                    pass
    # 3. 页面文本前 2000 字符内的日期（数字/英文）
    text = soup.get_text(" ", strip=True)[:2000]
    m = re.search(r"(20\d\d[-/年]\d{1,2}[-/月]\d{1,2})", text) or re.search(
        r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+20\d\d"
        r"|\d{1,2}(?:st|nd|rd|th)?\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?,?\s+20\d\d)", text, re.I)
    if m:
        try:
            return dup.parse(m.group(1).replace("年", "-").replace("月", "-").replace("日", ""))
        except Exception:
            pass
    return None


def _fetch_article_dated(source, window_start, window_end, limit=5):
    """列表页无日期时（如桥水）：取前 N 个文章链接，逐个进文章页提取发布日期"""
    try:
        r = requests.get(source["list_url"], headers=UA, timeout=(8, 20))
        r.raise_for_status()
        html = r.text
    except Exception:
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                try:
                    browser = p.chromium.launch(channel="chrome", args=["--no-sandbox", "--disable-dev-shm-usage"])
                except Exception:
                    browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
                try:
                    page = browser.new_page(user_agent=UA["User-Agent"])
                    page.goto(source["list_url"], wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(4000)
                    html = page.content()
                finally:
                    browser.close()
        except Exception as bx:
            return [], f"列表页获取失败: {str(bx)[:60]}"
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    path_inc = source.get("path_include", "")
    links, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = str(a["href"])
        if path_inc and path_inc not in href:
            continue
        if href.startswith("/"):
            from urllib.parse import urljoin
            href = urljoin(source["list_url"], href)
        title = a.get_text(strip=True)
        if len(title) < 15 or href in seen:
            continue
        seen.add(href)
        links.append({"title": title, "url": href})
        if len(links) >= limit:
            break
    items = []
    for it in links:
        try:
            pr = requests.get(it["url"], headers=UA, timeout=(8, 20))
            pr.raise_for_status()
            pub = _parse_date_from_article_page(pr.text)
        except Exception:
            continue
        if pub is None:
            continue
        if pub.tzinfo is None:
            from src.main import CST as _CST
            pub = pub.replace(tzinfo=_CST)
        if window_start <= pub < window_end:
            items.append({"title": it["title"], "url": it["url"], "published_at": pub, "summary_raw": ""})
    return items, None if items else f"进{len(links)}个文章页取日期，窗口内 0 条"


def _debug_dump_cards(html, source, label):
    """调试模式（TT_DEBUG_HTML=1）：打印前2个文章卡片的 HTML 结构，供分析各站日期位置"""
    import os
    if not os.environ.get("TT_DEBUG_HTML"):
        return
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    path_inc = source.get("path_include", "")
    anchors = [a for a in soup.find_all("a", href=True)
               if path_inc in str(a["href"]) and len(a.get_text(strip=True)) >= 15][:2]
    print(f"    [DEBUG {source['name']}@{label}] 整页 {len(html)}B，文章链接 {len(anchors)} 个（取样）")
    for i, a in enumerate(anchors):
        card = a.parent.parent if a.parent and a.parent.parent else a
        snippet = str(card)[:600].replace("\n", " ")
        print(f"    [DEBUG 卡片{i+1}] {snippet}")


def fetch_scrape(source, window_start, window_end):
    """抓列表页；静态抓取无结果时自动降级 Playwright 浏览器渲染；
    列表页本身无日期的站点（article_dated: true）进文章页取日期"""
    # 桥水类：列表页无日期，直接走文章页取日期通道
    if source.get("article_dated"):
        return _fetch_article_dated(source, window_start, window_end)
    try:
        r = requests.get(source["list_url"], headers=UA, timeout=(8, 20))
        r.raise_for_status()
        _debug_dump_cards(r.text, source, "静态")
        items = _extract_items_from_html(r.text, source, window_start, window_end)
        if items:
            return items, None
        # 静态无结果（JS 渲染/日期在动态元素里）→ 浏览器兜底
        try:
            items = _fetch_browser(source, window_start, window_end)
            return items, None if items else "静态+浏览器渲染后仍 0 条（窗口内无更新或日期未渲染）"
        except Exception as bx:
            return [], f"静态 0 条，浏览器兜底失败: {str(bx)[:60]}"
    except Exception as ex:
        # 静态请求直接失败也尝试浏览器（反爬 403 等）
        try:
            items = _fetch_browser(source, window_start, window_end)
            return items, None if items else f"静态失败({str(ex)[:40]})，浏览器渲染后 0 条"
        except Exception as bx:
            return [], f"静态失败({str(ex)[:40]})，浏览器兜底也失败: {str(bx)[:60]}"


def step_thinktank(fz, llm, tt_cfg, dry_run):
    """每日智库：先归档昨日内容，再扫描 8 家信源写入新文章"""
    t_main = tt_cfg["tables"]["main"]
    t_arc = tt_cfg["tables"]["archive"]
    today = today_cst()

    # ---- 归档：抓取日期早于今天的记录移入归档表 ----
    records = fz.list_records(t_main)
    old = [r for r in records if r["fields"].get("抓取日期")
           and field_to_date(r["fields"]["抓取日期"]) is not None
           and field_to_date(r["fields"]["抓取日期"]) < today]
    print(f"[智库] 主表 {len(records)} 条，待归档 {len(old)} 条")
    if old and not dry_run:
        now_str = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
        rows = []
        for r in old:
            f = dict(r["fields"])
            f["归档时间"] = now_str
            rows.append(f)
        fz.batch_create(t_arc, rows)
        fz.batch_delete(t_main, [r["record_id"] for r in old])
        print(f"[智库] 已归档 {len(rows)} 条")

    # ---- 抓取窗口：前一天 00:00 ~ 今天 09:00（北京时间，与需求一致）----
    end_hour = tt_cfg["window"]["end_hour"]
    window_end = datetime(today.year, today.month, today.day, end_hour, tzinfo=CST)
    yesterday = today - timedelta(days=1)
    window_start = datetime(yesterday.year, yesterday.month, yesterday.day, 0, 0, tzinfo=CST)
    print(f"[智库] 抓取窗口: {window_start.strftime('%m-%d %H:%M')} ~ {window_end.strftime('%m-%d %H:%M')}")

    # ---- 逐信源抓取（单家失败不中断）----
    all_items, failures = [], []
    for src in tt_cfg["sources"]:
        if src["type"] == "rss":
            items, err = fetch_rss(src, window_start, window_end)
        else:
            items, err = fetch_scrape(src, window_start, window_end)
        for it in items:
            it["source"] = src["name"]
        all_items.extend(items)
        status = f"{len(items)} 条" if not err else f"失败: {err[:60]}"
        print(f"  [{src['name']}] {status}")
        if err:
            failures.append(src["name"])

    # ---- 去重：主表+归档表已有链接 ----
    existing = set()
    for tid in (t_main, t_arc):
        for r in fz.list_records(tid):
            u = r["fields"].get("链接")
            if u:
                existing.add(str(u).strip("[]\"'"))
    new_items = [it for it in all_items if it["url"] and it["url"] not in existing]
    print(f"[智库] 抓取 {len(all_items)} 条，去重后新增 {len(new_items)} 条")
    if not new_items:
        # 用户要求：昨日全部智库无更新时，写入一条占位记录；有更新才记录正式条目
        # 防重：主表已有今天的占位记录则不重复写（手动多次触发时）
        has_placeholder = any("（昨日无更新）" in str(r["fields"].get("原标题", ""))
                              and str(r["fields"].get("抓取日期", "")).startswith(today.isoformat())
                              for r in records)
        if not dry_run:
            if has_placeholder:
                print("[智库] 今日占位记录已存在，跳过")
            else:
                fz.batch_create(t_main, [{
                    "原标题": "（昨日无更新）",
                    "中文标题": f"昨日无更新：8家智库均无新发布（{window_start.strftime('%m-%d')}）",
                    "抓取日期": date_str(today),
                    "状态": "跳过",
                }])
                print("[智库] 已写入「昨日无更新」占位记录")
        else:
            print("[智库] [dry] 应写入「昨日无更新」占位记录" if not has_placeholder else "[智库] [dry] 占位已存在")
        return

    # ---- LLM 批量生成中文标题/摘要/标签/相关度（每批≤20）----
    today_str = date_str(today)
    rows = []
    for i in range(0, len(new_items), 20):
        batch = new_items[i : i + 20]
        arts = [{"title": it["title"], "url": it["url"], "summary": it["summary_raw"],
                 "source": it["source"],
                 "published_at": it["published_at"].strftime("%Y-%m-%d") if it["published_at"] else None}
                for it in batch]
        result = llm.chat_json(LLM_PROMPT.format(
            topics=tt_cfg["topics"], rules=tt_cfg["relevance_rules"],
            articles=json.dumps(arts, ensure_ascii=False)))
        items = result.get("items", result if isinstance(result, list) else [])
        url2item = {it["url"]: it for it in batch}
        for it in items:
            src_item = url2item.get(it.get("url"), {})
            pub = src_item.get("published_at")
            rows.append({
                "原标题": src_item.get("title", ""),
                "中文标题": it.get("zh_title", ""),
                "来源": src_item.get("source", ""),
                "发布日期": date_str(pub.date()) if pub else today_str,
                "抓取日期": today_str,
                "链接": it.get("url", ""),
                "摘要": it.get("zh_summary", ""),
                "主题标签": it.get("topics") or [],
                "相关度": it.get("relevance", 3),
                "精读": False,
                "状态": "待读",
            })

    if dry_run:
        for r in rows:
            print(f"  [dry] [{r['来源']}] {str(r['中文标题'])[:40]} ★{r['相关度']}")
        return
    fz.batch_create(t_main, rows)
    print(f"[智库] 已写入 {len(rows)} 条" + (f"；失败信源: {','.join(failures)}" if failures else ""))
