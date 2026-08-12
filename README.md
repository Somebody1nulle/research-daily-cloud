# 调研工作流云端版（research-daily-cloud）

把「每日调研工作流」搬到 GitHub Actions：北京时间每天 9:00 自动执行，无需本地电脑开机。

## 每天自动做什么

1. **归档昨日热点**：每日热点表中日期早于今天的记录 → 每日热点归档表（补归档时间），主表清空
2. **同步入备选**：刚归档批次中用户标记「入备选=Y」的记录 → 备选选题库（自动去重、防重复同步）
3. **定向搜索**：读主题进度看板当前小专题 → Tavily 搜索 → Kimi 筛选（产品经理视角：重原理轻论文）→ 写每日调研（文字 5 + 音视频 4，标候选精读）
4. **热点搜集**：按窗口（上次启动日期~昨天）搜五领域 → Kimi 选出 5 条（含去重审查、候选选题判断）→ 写每日热点

## 部署步骤

1. GitHub 新建**私有**仓库，推送本目录代码
2. 仓库 Settings → Secrets and variables → Actions，添加：
   | Secret | 来源 |
   |---|---|
   | `FEISHU_APP_ID` | 飞书开放平台 → 自建应用 → 凭证与基础信息 |
   | `FEISHU_APP_SECRET` | 同上 |
   | `KIMI_API_KEY` | 本地 ~/.hermes/.env 中的 KIMI_API_KEY |
   | `TAVILY_API_KEY` | 本地 ~/.hermes/.env 中的 TAVILY_API_KEY |
   | `KIMI_BASE_URL` | https://api.kimi.com/coding/v1 |
3. Actions 页手动触发一次「每日调研工作流」验证（workflow_dispatch）
4. 绿灯后每天北京时间 9:00 自动运行（UTC `0 1 * * *`）

## 本地运行（调试用）

```bash
pip install -r requirements.txt
export FEISHU_APP_ID=... FEISHU_APP_SECRET=... KIMI_API_KEY=... TAVILY_API_KEY=...
python -m src.main --dry-run   # 冒烟测试：不写飞书，只打印
python -m src.main             # 正式执行
```

## 维护指南

- **改配额/表ID/领域**：`config/workflow.yaml`
- **改提示词（选材调性/热点规则）**：`config/prompts.yaml`
- **换模型**：`config/workflow.yaml` 的 `llm.model`（注意 k3 系列只接受 temperature=1）
- **看运行日志**：GitHub 仓库 → Actions → 点进对应运行记录
- **某天表格没更新**：先看 Actions 是否红灯，日志里搜「失败」

## 注意

- GitHub Actions 定时可能有 5-30 分钟延迟（免费额度特性），不影响使用
- 免费额度 2000 分钟/月，本项目每天约用 3-5 分钟，绰绰有余
