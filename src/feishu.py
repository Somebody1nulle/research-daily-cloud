"""飞书开放平台 API 封装（base/v3 接口，tenant_access_token 方式，无需用户在线）
注意：使用 /open-apis/base/v3（与 lark-cli 相同的权限体系 base:*），
     不用 /open-apis/bitable/v1（需要另一套 bitable:app 权限）。
v3 特点：读取返回「字段名数组 + 行值数组」的位置对应结构，本模块负责映射为命名字典。
"""
import os
import time
import requests

BASE = "https://open.feishu.cn/open-apis/base/v3"


class FeishuClient:
    def __init__(self, app_id=None, app_secret=None, base_token=None):
        self.app_id = app_id or os.environ["FEISHU_APP_ID"]
        self.app_secret = app_secret or os.environ["FEISHU_APP_SECRET"]
        self.base_token = base_token
        self._token = None
        self._token_expire = 0

    # ---------- 认证 ----------
    def token(self):
        """获取 tenant_access_token，带过期缓存与 401 自动刷新"""
        if self._token and time.time() < self._token_expire - 60:
            return self._token
        r = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        assert data.get("code") == 0, f"获取token失败: {data}"
        self._token = data["tenant_access_token"]
        self._token_expire = time.time() + data.get("expire", 7200)
        return self._token

    def _headers(self):
        return {"Authorization": f"Bearer {self.token()}"}

    def _request(self, method, path, retry401=True, **kwargs):
        """统一请求入口，处理 401 刷新、业务错误码、SSL/连接抖动重试"""
        last_err = None
        for attempt in range(4):
            try:
                r = requests.request(method, f"{BASE}{path}", headers=self._headers(), timeout=30, **kwargs)
                break
            except (requests.exceptions.SSLError, requests.exceptions.ConnectionError) as e:
                last_err = e
                time.sleep(2 * (attempt + 1))
        else:
            raise RuntimeError(f"飞书API网络失败 {path}: {last_err}")
        if r.status_code == 401 and retry401:
            self._token = None  # 强制刷新
            return self._request(method, path, retry401=False, **kwargs)
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 0:
            raise RuntimeError(f"飞书API错误 {path}: {data.get('code')} {data.get('msg')}")
        return data.get("data", {})

    # ---------- 记录读取 ----------
    def list_records(self, table_id, page_size=100):
        """分页拉取整张表，返回 [{record_id, fields:{字段名:值}}, ...]
        v3 返回位置数组：fields(字段名) 与 data(行值) 按位置对应
        注意：v3 records 接口用 offset 分页（无 page_token），且单页上限 20 条；
        空页/无进展时强制 break 防死循环（曾因此挂起 30 分钟）"""
        items, offset = [], 0
        while True:
            data = self._request(
                "GET",
                f"/bases/{self.base_token}/tables/{table_id}/records",
                params={"page_size": page_size, "offset": offset},
            )
            names = data.get("fields", [])
            ids = data.get("record_id_list", [])
            rows = data.get("data", [])
            if not rows:
                break
            for rid, row in zip(ids, rows):
                items.append({"record_id": rid, "fields": dict(zip(names, row))})
            offset += len(rows)
            if not data.get("has_more"):
                break
        return items

    # ---------- 记录写入 ----------
    @staticmethod
    def _norm(v):
        """规整写入值：单选/多选v3读取时是数组，写入用字符串/None 原样"""
        if isinstance(v, list) and len(v) == 1:
            return v[0]
        return v

    def batch_create(self, table_id, fields_list):
        """批量新增。fields_list: [{字段名:值}, ...]；v3 需转为 fields+rows 位置格式"""
        if not fields_list:
            return 0
        names = list(fields_list[0].keys())
        rows = [[self._norm(f.get(n)) for n in names] for f in fields_list]
        for i in range(0, len(rows), 200):
            self._request(
                "POST",
                f"/bases/{self.base_token}/tables/{table_id}/records/batch_create",
                json={"fields": names, "rows": rows[i : i + 200]},
            )
        return len(rows)

    def update_record(self, table_id, record_id, patch):
        """单条更新（patch 为 {字段名:值}）"""
        self._request(
            "POST",
            f"/bases/{self.base_token}/tables/{table_id}/records/batch_update",
            json={"record_id_list": [record_id],
                  "patch": {k: self._norm(v) for k, v in patch.items()}},
        )

    def batch_update(self, table_id, updates):
        """批量更新（逐条，updates: [{record_id, fields}, ...]）。少量记录场景使用"""
        for u in updates:
            self.update_record(table_id, u["record_id"], u["fields"])

    def batch_delete(self, table_id, record_ids):
        """批量删除"""
        for i in range(0, len(record_ids), 200):
            self._request(
                "POST",
                f"/bases/{self.base_token}/tables/{table_id}/records/batch_delete",
                json={"record_id_list": record_ids[i : i + 200]},
            )
