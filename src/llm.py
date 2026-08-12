"""Kimi API 封装：批量生成结构化 JSON 输出，带解析重试"""
import os
import json
import requests


class KimiClient:
    def __init__(self, api_key=None, base_url=None, model=None, max_retries=2):
        self.api_key = api_key or os.environ["KIMI_API_KEY"]
        self.base_url = (base_url or os.environ.get("KIMI_BASE_URL")
                         or "https://api.kimi.com/coding/v1").rstrip("/")
        self.model = model or "k3-256k"
        self.max_retries = max_retries

    def chat_json(self, prompt, temperature=1):
        """发送提示词并期望严格 JSON 输出；解析失败自动重试
        注意：k3 系列模型只接受 temperature=1，不要传其他值"""
        last_err = None
        for attempt in range(self.max_retries + 1):
            try:
                r = requests.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": "你是严谨的助手，只输出合法 JSON，不输出 markdown 代码块包裹。"},
                            {"role": "user", "content": prompt},
                        ],
                        "temperature": temperature,
                        "response_format": {"type": "json_object"},
                    },
                    timeout=120,
                )
                r.raise_for_status()
                content = r.json()["choices"][0]["message"]["content"]
                return self._parse_json(content)
            except Exception as e:
                last_err = e
                print(f"  [LLM 第{attempt+1}次失败] {e}")
        raise RuntimeError(f"LLM 输出解析失败（重试{self.max_retries}次后仍失败）: {last_err}")

    @staticmethod
    def _parse_json(text):
        """容错解析：去掉可能的 ```json 包裹，截取首个 [ 或 { 到末尾"""
        t = text.strip()
        if t.startswith("```"):
            t = t.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        # 找到第一个 JSON 起始符
        idx = min([i for i in (t.find("["), t.find("{")) if i >= 0], default=-1)
        if idx < 0:
            raise ValueError("输出中未找到 JSON")
        return json.loads(t[idx:])
