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


def fetch_rss(source, window_start, window_end):
    """抓 RSS，过滤窗口内条目"""
    try:
        r = requests.get(source["feed_url"], headers=UA, timeout=(8, 20))
        r.raise_for_status()
        feed = feedparser.parse(r.content)
        items = []
        for e in feed.entries:
            pub = parse_date(e)
            summary = re.sub(r"<[^>]+>", "", getattr(e, "summary", "") or "")[:400]
            if pub is None:
                # 无发布时间的保留，发布日期记为抓取日期
                items.append({"title": e.get("title", ""), "url": e.get("link", ""),
                              "published_at": None, "summary_raw": summary})
            elif window_start <= pub < window_end:
                items.append({"title": e.get("title", ""), "url": e.get("link", ""),
                              "published_at": pub, "summary_raw": summary})
        return items, None
    except Exception as ex:
        return [], str(ex)


def fetch_scrape(source, window_start, window_end):
    """无 RSS 站点抓列表页（取前 15 篇文章链接，无日期信息的保留）"""
    try:
        from bs4 import BeautifulSoup
        r = requests.get(source["list_url"], headers=UA, timeout=(8, 20))
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
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
            # 只保留文章路径，过滤导航/页脚链接
            if path_inc and path_inc not in href:
                continue
            if href.rstrip("/") == source["list_url"].rstrip("/"):
                continue
            seen.add(href)
            items.append({"title": title, "url": href, "published_at": None, "summary_raw": ""})
            if len(items) >= 15:
                break
        return items, None
    except Exception as ex:
        return [], str(ex)


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

    # ---- 抓取窗口 ----
    end_hour = tt_cfg["window"]["end_hour"]
    window_end = datetime(today.year, today.month, today.day, end_hour, tzinfo=CST)
    window_start = window_end - timedelta(days=1)
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
