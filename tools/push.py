"""通过 GitHub REST API 推送整个目录（绕过被墙的 github.com git 协议）"""
import base64
import json
import os
import sys
import requests
from pathlib import Path

TOKEN = sys.argv[1]
REPO = "Somebody1nulle/research-daily-cloud"  # 用法: python tools/push.py <PAT>
PROJ = Path(os.path.expanduser("~/projects/research-daily-cloud"))
API = "https://api.github.com"
H = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"}


def collect_files():
    files = []
    for p in PROJ.rglob("*"):
        if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts:
            # workflow 文件需要令牌带 workflow scope，缺失时先跳过
            if ".github/workflows" in p.as_posix() and os.environ.get("SKIP_WORKFLOW"):
                continue
            files.append(p)
    return files


S = requests.Session()
S.headers.update(H)
S.trust_env = False  # 忽略系统代理环境变量


def req(method, url, **kwargs):
    """带重试的请求（网络对 GitHub 不稳定，SSL EOF/超时自动重试）
    用 Session 复用同一条 TLS 连接，减少新连接被中间设备重置的概率"""
    kwargs.setdefault("timeout", 30)
    for attempt in range(10):
        try:
            r = S.request(method, url, **kwargs)
            # 5xx、假限流 401/403、以及 trees 端点的间歇 404（中转注入）都重试
            transient = (r.status_code in (401, 403) and "rate limit" in r.text) \
                or r.status_code >= 500 \
                or (r.status_code == 404 and "git/trees" in url)
            if transient:
                print(f"  [重试{attempt+1}] HTTP {r.status_code} {url[-30:]}")
                import time as _t; _t.sleep(3 * (attempt + 1))
                continue
            return r
        except Exception as e:
            print(f"  [重试{attempt+1}] {type(e).__name__}")
            import time as _t; _t.sleep(3 * (attempt + 1))
    raise RuntimeError(f"请求失败: {method} {url}")


def main():
    # 1. 拿 main 分支当前 commit（空仓库可能没有）
    r = req("GET", f"{API}/repos/{REPO}/git/refs/heads/main")
    print("refs status:", r.status_code, r.text[:120])
    base_sha = r.json()["object"]["sha"] if r.status_code == 200 else None
    if base_sha is None:
        # 空仓库不能用 git/blobs API（409），先用 Contents API 放 README 垫出首个 commit
        readme = (PROJ / "README.md").read_bytes()
        cr = req("PUT", f"{API}/repos/{REPO}/contents/README.md",
                          json={"message": "init: README",
                                "content": base64.b64encode(readme).decode(),
                                "branch": "main"})
        cr.raise_for_status()
        r = req("GET", f"{API}/repos/{REPO}/git/refs/heads/main")
        base_sha = r.json()["object"]["sha"]
    base_tree = None
    if base_sha:
        c = req("GET", f"{API}/repos/{REPO}/git/commits/{base_sha}").json()
        base_tree = c["tree"]["sha"]
    print("base commit:", base_sha or "(空仓库)")

    # 2. 逐文件创建 blob
    tree_items = []
    files = collect_files()
    for p in files:
        rel = p.relative_to(PROJ).as_posix()
        content = base64.b64encode(p.read_bytes()).decode()
        r = req("POST", f"{API}/repos/{REPO}/git/blobs",
                          json={"content": content, "encoding": "base64"})
        r.raise_for_status()
        tree_items.append({"path": rel, "mode": "100644", "type": "blob",
                           "sha": r.json()["sha"]})
        print(f"  blob: {rel}")

    # 3. 创建 tree
    payload = {"tree": tree_items}
    if base_tree:
        payload["base_tree"] = base_tree
    r = req("POST", f"{API}/repos/{REPO}/git/trees", json=payload)
    print("trees status:", r.status_code, r.text[:200])
    r.raise_for_status()
    tree_sha = r.json()["sha"]

    # 4. 创建 commit
    cbody = {"message": "调研工作流云端版：GitHub Actions 每日9:00自动执行（归档/同步/定向搜索/热点）",
             "tree": tree_sha}
    if base_sha:
        cbody["parents"] = [base_sha]
    r = req("POST", f"{API}/repos/{REPO}/git/commits", json=cbody)
    r.raise_for_status()
    commit_sha = r.json()["sha"]
    print("commit:", commit_sha[:8])

    # 5. 更新/创建 main 引用
    if base_sha:
        r = req("PATCH", f"{API}/repos/{REPO}/git/refs/heads/main",
                           json={"sha": commit_sha, "force": False})
    else:
        r = req("POST", f"{API}/repos/{REPO}/git/refs",
                          json={"ref": "refs/heads/main", "sha": commit_sha})
    r.raise_for_status()
    print(f"✅ 推送完成：{len(files)} 个文件 -> main")


if __name__ == "__main__":
    main()
