"""调研工作流云端版 · 主流程
每天 9:00（北京时间）由 GitHub Actions 触发：
  1. 归档昨日热点（每日热点 → 每日热点归档）
  2. 同步用户标记的入备选=Y → 备选选题库（去重）
  3. 读主题进度看板 → 定向搜索 → Kimi 筛选概述 → 写每日调研
  4. 热点窗口搜索 → Kimi 筛选 5 条 → 写每日热点（含候选选题判断）
用法：
  python -m src.main            # 正式执行（写飞书）
  python -m src.main --dry-run  # 冒烟测试：只读+搜索+LLM，打印将写入的内容，不写飞书
"""
import os
import sys
import json
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from src.feishu import FeishuClient
from src.search import SearchClient
from src.llm import KimiClient

# 北京时区（GitHub Actions 上是 UTC，必须显式指定）
CST = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parent.parent


def load_config():
    with open(ROOT / "config" / "workflow.yaml", encoding="utf-8") as f:
        wf = yaml.safe_load(f)
    with open(ROOT / "config" / "prompts.yaml", encoding="utf-8") as f:
        pr = yaml.safe_load(f)
    return wf, pr


def today_cst():
    return datetime.now(CST).date()


def date_str(d):
    """日期字段写入值：v3 接受 'YYYY-MM-DD HH:mm:ss' 字符串"""
    return d.strftime("%Y-%m-%d 00:00:00")


def field_to_date(v):
    """日期字段读取值：v3 返回 ISO 字符串或毫秒时间戳，统一转 date"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(int(v) / 1000, tz=CST).date()
    # ISO 字符串，如 2026-08-12T00:00:00.000+08:00
    return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(CST).date()


# ---------------------------------------------------------------- 步骤 1：归档昨日热点
def step_archive(fz, cfg, dry_run):
    t_hot = cfg["feishu"]["tables"]["daily_hot"]
    t_arc = cfg["feishu"]["tables"]["hot_archive"]
    today = today_cst()

    records = fz.list_records(t_hot)
    old = [r for r in records
           if r["fields"].get("日期") and field_to_date(r["fields"]["日期"]) < today]
    print(f"[步骤1] 主表共 {len(records)} 条，待归档（早于今天） {len(old)} 条")

    if not old:
        return []

    now_str = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    if dry_run:
        for r in old:
            print(f"  [dry] 归档: {str(r['fields'].get('标题'))[:40]}")
        return old

    # 写入归档表（补归档时间），再删主表
    rows = []
    for r in old:
        f = r["fields"]
        rows.append({
            "日期": f.get("日期"),
            "领域": f.get("领域"),
            "标题": f.get("标题"),
            "一句话概述": f.get("一句话概述"),
            "链接": f.get("链接"),
            "入备选": f.get("入备选") or "未判",
            "备注": f.get("备注"),
            "归档时间": now_str,
        })
    fz.batch_create(t_arc, rows)
    fz.batch_delete(t_hot, [r["record_id"] for r in old])
    print(f"[步骤1] 已归档 {len(rows)} 条并清空主表旧记录")
    return old


# ---------------------------------------------------------------- 步骤 2：同步入备选
def step_sync_topics(fz, cfg, archived, dry_run):
    t_topics = cfg["feishu"]["tables"]["topics"]
    # 只处理「刚归档的那批」中用户打了 Y 且未同步的
    pending = [r for r in archived
               if r["fields"].get("入备选") == "Y"
               and "已同步备选选题库" not in str(r["fields"].get("备注") or "")]
    print(f"[步骤2] 刚归档批次中待同步入备选: {len(pending)} 条")
    if not pending:
        return

    # 去重：读选题库现有名称
    existing = {str(r["fields"].get("选题名称")) for r in fz.list_records(t_topics)}
    today = today_cst()
    new_rows, updates = [], []
    for r in pending:
        f = r["fields"]
        note = str(f.get("备注") or "")
        # 选题名称：优先取备注中【候选选题】后的名称，否则用标题
        name = f.get("标题", "")
        if "【候选选题】" in note:
            name = note.split("【候选选题】", 1)[1].split("：")[0].split("【")[0].strip()
        if name in existing:
            print(f"  [跳过-重复] {name[:30]}")
            continue
        new_rows.append({
            "选题名称": name,
            "来源": "高频信号",
            "出现次数": 1,
            "录入日期": date_str(today),
            "优先级": "一般",
            "状态": "备选",
            "备注": f"来自每日热点归档：{f.get('标题')}。{note[:80]}",
        })
        updates.append({
            "record_id": r["record_id"],
            "fields": {"备注": note + f"【已同步备选选题库 {today.strftime('%m/%d')}】"},
        })
        print(f"  [同步] {name[:40]}")

    if dry_run:
        return
    if new_rows:
        fz.batch_create(t_topics, new_rows)
    if updates:
        t_arc = cfg["feishu"]["tables"]["hot_archive"]
        fz.batch_update(t_arc, updates)
    print(f"[步骤2] 同步完成：新增 {len(new_rows)} 条选题")


# ---------------------------------------------------------------- 步骤 3：定向搜索 → 每日调研
def step_research(fz, sc, llm, cfg, pr, dry_run):
    t_board = cfg["feishu"]["tables"]["board"]
    t_res = cfg["feishu"]["tables"]["daily_research"]

    board = fz.list_records(t_board)
    if not board:
        print("[步骤3] 看板为空（冷启动），跳过定向搜索")
        return
    latest = board[-1]  # 看板只有一行当前行
    subtopic = latest["fields"].get("小专题", "")
    task = latest["fields"].get("当日任务", "")

    # --- 日程轮换：按今天日期对照 config.schedule，看板滞后则自动推进 ---
    today = today_cst()
    entry = next((e for e in cfg.get("schedule", [])
                  if str(e["date"]) == today.isoformat()), None)
    if entry and entry["subtopic"] != subtopic:
        print(f"[步骤3] 日程轮换：{subtopic} → {entry['subtopic']}")
        subtopic, task = entry["subtopic"], entry["task"]
        if not dry_run:
            fz.update_record(t_board, latest["record_id"],
                             {"小专题": subtopic, "当日任务": task})
    print(f"[步骤3] 当前小专题: {subtopic} | 任务: {str(task)[:50]}")

    rp = pr["research"]
    text_q = [q.format(subtopic=subtopic) for q in rp["text_queries"]]
    av_q = [q.format(subtopic=subtopic) for q in rp["av_queries"]]
    print(f"[步骤3] 搜索 {len(text_q)+len(av_q)} 组查询...")
    text_cands = sc.search_many(text_q)
    av_cands = sc.search_many(av_q)
    print(f"[步骤3] 候选：文字 {len(text_cands)} 条 / 音视频 {len(av_cands)} 条")

    candidates = {
        "texts": text_cands[:20],
        "avs": av_cands[:15],
    }
    prompt = rp["select_prompt"].format(
        subtopic=subtopic, task=task,
        candidates=json.dumps(candidates, ensure_ascii=False, indent=1),
        n_text=cfg["quota"]["research_text"],
        n_av=cfg["quota"]["research_av"],
        n_cand=cfg["quota"]["candidate_deepread"],
    )
    result = llm.chat_json(prompt)
    texts = result.get("texts", [])
    avs = result.get("avs", [])
    print(f"[步骤3] Kimi 选出：文字 {len(texts)} 篇 / 音视频 {len(avs)} 份")

    today = today_cst()
    step_name = "专题调研"  # 压缩节奏：单日专题
    rows = []
    for it in texts + avs:
        rows.append({
            "日期": date_str(today),
            "所属小专题": subtopic,
            "日步骤": step_name,
            "标题": it.get("title", ""),
            "一句话概述": it.get("summary", ""),
            "链接": it.get("url", ""),
            "材料类型": it.get("material_type", "文章"),
            "预估时长": it.get("duration", "15-30min"),
            "是否精读": "候选精读" if it.get("candidate") else None,
        })
    if dry_run:
        for r in rows:
            print(f"  [dry] [{r['材料类型']}] {r['标题'][:35]} ({r['预估时长']}) {'★候选' if r['是否精读'] else ''}")
        return
    fz.batch_create(t_res, rows)
    print(f"[步骤3] 已写入每日调研 {len(rows)} 条")


# ---------------------------------------------------------------- 步骤 4：热点搜集 → 每日热点
def step_hotspots(fz, sc, llm, cfg, pr, dry_run):
    t_hot = cfg["feishu"]["tables"]["daily_hot"]
    t_arc = cfg["feishu"]["tables"]["hot_archive"]
    today = today_cst()
    yesterday = today - timedelta(days=1)

    # 窗口 = 归档表中最新日期 至 昨天（上次启动覆盖过的日期不重复）
    arc = fz.list_records(t_arc)
    dates = [field_to_date(r["fields"]["日期"]) for r in arc if r["fields"].get("日期")]
    start = max(dates) + timedelta(days=1) if dates else yesterday
    if start > yesterday:
        start = yesterday
    window = f"{start.month}月{start.day}日至{yesterday.month}月{yesterday.day}日"
    print(f"[步骤4] 热点窗口: {window}")

    # 按领域搜索（带日期关键词增强时效）
    hp = pr["hotspot"]
    date_kw = f"{yesterday.month}月{yesterday.day}日"
    all_cands = []
    for domain in cfg["hot_domains"]:
        q = hp["query_template"].format(domain=domain, date_kw=date_kw)
        results = sc.search(q)
        for it in results:
            it["domain"] = domain
        all_cands.extend(results)
        print(f"  [{domain}] 搜到 {len(results)} 条")

    prompt = hp["select_prompt"].format(
        window=window,
        candidates=json.dumps(all_cands[:40], ensure_ascii=False, indent=1),
        n_hot=cfg["quota"]["hotspots"],
    )
    picks = llm.chat_json(prompt)
    # 容错：兼容 {"items":[...]} / 纯数组 / 其他键包裹
    if isinstance(picks, dict):
        picks = picks.get("items") or next((v for v in picks.values() if isinstance(v, list)), [])
    picks = [p for p in picks if isinstance(p, dict)]
    print(f"[步骤4] Kimi 选出 {len(picks)} 条热点")

    rows = []
    for it in picks:
        cand = (it.get("candidate_topic") or "").strip()
        rows.append({
            "日期": date_str(today),
            "领域": it.get("domain", "科技综合"),
            "标题": it.get("title", ""),
            "一句话概述": it.get("summary", ""),
            "链接": it.get("url", ""),
            "入备选": "未判",
            "备注": f"【候选选题】{cand}" if cand else None,
        })
    if dry_run:
        for r in rows:
            print(f"  [dry] [{r['领域']}] {r['标题'][:40]}")
        return
    fz.batch_create(t_hot, rows)
    print(f"[步骤4] 已写入每日热点 {len(rows)} 条")


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只读+搜索+LLM，不写飞书")
    args = ap.parse_args()

    cfg, pr = load_config()
    fz = FeishuClient(base_token=cfg["feishu"]["base_token"])
    sc = SearchClient(max_results=cfg["search"]["max_results_per_query"],
                      days=cfg["search"]["days_lookback"])
    llm = KimiClient(base_url=cfg["llm"]["base_url"],
                     model=cfg["llm"]["model"],
                     max_retries=cfg["llm"]["max_retries"])

    print(f"===== 调研工作流云端版 {datetime.now(CST).strftime('%Y-%m-%d %H:%M')} (北京时间) {'[DRY-RUN]' if args.dry_run else ''} =====")
    archived = step_archive(fz, cfg, args.dry_run)
    step_sync_topics(fz, cfg, archived, args.dry_run)
    step_research(fz, sc, llm, cfg, pr, args.dry_run)
    step_hotspots(fz, sc, llm, cfg, pr, args.dry_run)
    print("===== 完成 =====")


if __name__ == "__main__":
    main()
