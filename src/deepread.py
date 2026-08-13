"""云端精读：抓取全文 → Kimi 生成精读总结+学习卡片 → 上传飞书云盘（Obsidian 同步文件夹）"""
import os
import re
import html
import json
import requests

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"}

DEEPREAD_PROMPT = """你是为一位【产品经理】写精读笔记的助手。基于下面的文章全文，输出两个 Markdown 文档。

要求：
- 用小白能懂的语言，多用直觉类比和「这意味着什么」，讲清概念与基本原理
- 公式/训练细节/参数推导一律跳过或一句话带过「技术细节略」
- 目标是读者读完能向别人复述

文章标题：{title}
文章链接：{url}
文章全文：
{body}

严格输出 JSON 对象，包含两个键（值为 Markdown 字符串，不要输出其他文字）：
{{
  "summary": "# 精读 · {title}\\n\\n> 原文：{url}\\n\\n## 一句话核心观点\\n...\\n## 关键论点（3-5条）\\n...\\n## 重要数据与事实\\n...\\n## 与当前主题的关联\\n（主题：{topic}）\\n## 技术细节\\n（略，或一句话带过）",
  "card": "# 学习卡片 · {title}\\n\\n## 是什么\\n（大白话，能直接背）\\n## 为什么重要\\n...\\n## 关键点\\n1. ...\\n## 我的判断\\n（留空）"
}}"""


def fetch_text(url, min_len=500):
    """抓取网页正文（去脚本/样式/标签），返回文本；失败返回 None
    超时用 (连接5s, 读取10s) 元组——防止服务器慢速滴流导致永久挂起"""
    try:
        r = requests.get(url, headers=UA, timeout=(5, 10), stream=True)
        if r.status_code != 200:
            return None
        # 最多读 2MB，防止超大页面拖慢
        chunks, size = [], 0
        for chunk in r.iter_content(chunk_size=65536, decode_unicode=False):
            chunks.append(chunk)
            size += len(chunk)
            if size > 2 * 1024 * 1024:
                break
        r.close()
        raw = b"".join(chunks)
        try:
            t = raw.decode("utf-8")
        except UnicodeDecodeError:
            t = raw.decode("gb18030", errors="ignore")
        t = re.sub(r"<script.*?</script>", "", t, flags=re.S)
        t = re.sub(r"<style.*?</style>", "", t, flags=re.S)
        t = re.sub(r"<[^>]+>", "\n", t)
        t = html.unescape(t)
        lines = [l.strip() for l in t.split("\n") if len(l.strip()) >= 40]
        body = "\n".join(lines)
        return body if len(body) >= min_len else None
    except Exception:
        return None


def upload_markdown(fz_token_headers, folder_token, filename, content_md):
    """上传 md 文件到飞书云盘指定文件夹（drive v1 upload_all）"""
    r = requests.post(
        "https://open.feishu.cn/open-apis/drive/v1/files/upload_all",
        headers=fz_token_headers,
        data={
            "file_name": filename,
            "parent_type": "explorer",
            "parent_node": folder_token,
            "size": str(len(content_md.encode("utf-8"))),
        },
        files={"file": (filename, content_md.encode("utf-8"), "text/markdown")},
        timeout=60,
    )
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"上传失败 {filename}: {data.get('code')} {data.get('msg')}")
    return data["data"]["file_token"]


def slugify(title, maxlen=20):
    """标题缩写为文件名安全串"""
    s = re.sub(r'[\\/:*?"<>|\s]+', "", title)
    return s[:maxlen]
