# Financial Lobster — TODO.md 对账与修正文档

> **对账基准**：仓库 `leongxf/financial-lobster` 当前工作区（已包含 Docker 部署文件与远端启动脚本）。  
> **方法**：逐条 TODO 项与 `backend/`、部署文件实际代码比对，给出结论与具体改法。  
> **结论分五类**：补勾（代码已做、TODO 未勾）/ 合并（重复条目）/ 拆分改写（颗粒度或表述问题）/ 保持（确未做，状态正确）/ 新增（代码暴露的缺口，TODO 未覆盖）。
> **当前状态**：本文中核实为正确的修正已同步到 `TODO.md`；本文件保留对账依据和后续未做项。
> **行号说明**：下文表格中的 TODO 行号为修正前的原始对账行号，仅用于追溯；最新状态以当前 `TODO.md` 为准。

---

## 总览：本次已落地的 TODO 修正

| 类别 | 数量 | 涉及小节 |
|------|------|----------|
| 补勾（已实现却未勾） | 14 | §3, §4, §5, §6, §7, §9 |
| 合并（重复） | 4 | §5, §6, §8 |
| 拆分/改写 | 5 | §2, §4, §5, §6, §7 |
| 新增（工程缺口） | 7 | §2, §4, §9, §10 |
| 状态需澄清（部分完成） | 5 | §3, §5, §6 |

---

## §2 技术方案确认 — 拆分改写（最高优先级）

**问题**：本节把 LangGraph/LangChain、MySQL、Redis、Celery、React/Vite 全部勾选为「已确认技术栈」，但 `pyproject.toml` 实际依赖只有 `fastapi` / `uvicorn` / `pydantic-settings` / `httpx` / `python-json-logger` / `lark-oapi` / `pypdf`。

| TODO 行 | 声称 | 代码实证 | 改法 |
|---------|------|----------|------|
| L31 技术栈 | LangGraph/LangChain、MySQL、Redis、Celery、React/Vite | 均未引入 | 见下方拆分方案 |
| L33 运行方式 | 本机长连接 worker | `feishu_ws.py` ✓ 属实 | 保留 |
| L36 缓存/队列 Redis+Celery | — | 实为 `threading.Thread`（worker L325）+ 本地 JSON | 移到「目标栈」 |
| L38 数据库 MySQL | — | 实为本地 JSON（`task_store.py`）+ 本地文件 | 移到「目标栈」 |
| L40 LLMProvider 封装 | — | `llm_provider.py` ✓ 属实（httpx 裸调） | 保留在「MVP 栈」 |

**具体改法**：把 §2 拆成两个子节，避免把「目标意图」当成「已落地」。

### 2A. MVP 实际技术栈（已落地）

- [x] FastAPI 应用骨架（`main.py`，仅 `/health` 与 `/api/feishu/events`）
- [x] 飞书官方 SDK 长连接 worker（`lark-oapi`）
- [x] 任务存储：本地 JSON（`TaskStore`），非 MySQL
- [x] 异步执行：`threading.Thread`（单文件一线程），非 Celery
- [x] LLM 调用：httpx 直连 OpenAI 兼容接口，非 LangChain/LangGraph
- [x] 文件解析：pypdf（仅 PDF）
- [x] 无前端

### 2B. 规模化目标栈（待迁移，未落地）

- [ ] MySQL（迁移 `TaskStore` 等 JSON 存储）
- [ ] Redis + Celery（替换 threading）
- [ ] LangGraph/LangChain 编排（替换裸 httpx 流程）
- [ ] React/Vite 管理台（若确认环境不拦截）

---

## §3 飞书入口接入 — 补勾 ×3 + 澄清 ×1

| TODO 行 | 当前 | 代码实证 | 结论 |
|---------|------|----------|------|
| L45 入口可行性 Spike | [ ] | worker 已能接收/下载/回复，闭环可跑 | **补勾**（注：已由长连接 worker 验证，真实账号权限待 L152 实测确认） |
| L58 分析完成结果通知 | [ ] | `send_report_result`（worker L74-111）已实现 | **补勾** |
| L59 分析失败通知 | [ ] | worker `except httpx.ReadTimeout` / `except Exception` 分支均 `notify`（L279-300） | **补勾** |
| L53 飞书回调签名校验 | [ ] | 代码只做 `validate_verification_token`（`events.py` L21），未做签名校验（无 `FEISHU_ENCRYPT_KEY` 验签逻辑） | **保持**，改写表述 → 「实现飞书回调签名/加密校验（当前仅校验 verification_token，二者不等价）」 |

> L51/L52（应用权限、长连接订阅配置）属飞书开放平台外部配置，代码无法实证，保持 [ ]。

---

## §4 核心后端能力 — 补勾 ×1 + 澄清 ×1 + 新增 ×2

| TODO 行 | 当前 | 代码实证 | 结论 |
|---------|------|----------|------|
| L63 搭建后端 API 项目 | [ ] | `main.py` FastAPI + `/health` + `/api/feishu/events` 已存在 | **补勾**（注：骨架已建，业务接口仍缺，见 L76/L77） |
| L75 任务重试机制 | [ ] | 仅 LLM 层有 ReadTimeout 重试（见 §6 L106），任务级重试未做 | **保持**，改写 → 「实现任务级重试（LLM 调用级重试已在 LLMProvider 实现）」 |
| L64 DB 表设计 | [ ] | 未做（JSON 顶替） | **保持**，注「MVP 由本地 JSON 顶替，迁 MySQL 时落地」 |
| L65/L72 Capability 表/字段 | [ ] | 未做 | **保持** |
| L76 结果查询 API | [ ] | 未做 | **保持** |
| L77 用户反馈 API | [ ] | 未做 | **保持** |
| L78/L79 配置表/清理定时任务 | [ ] | 未做（L16 已决策 30/30/180 天留存，但无任何清理代码） | **保持**，建议提优先级 |

**新增条目**（代码暴露、TODO 未覆盖）：

- [ ] **事件幂等**：当前 `task_id` 直接用 `message_id`（worker L120），`TaskStore.create_task` 会覆盖写，飞书事件重推会重复处理同一文件，需基于 `message_id` 去重或加「已处理」短期标记。
- [ ] **并发与存储竞态边界**：`threading` + 本地 JSON 无锁读改写（`task_store.py` read→update→write），多任务共享 `analysis_cache` 目录。MVP 若接受单并发需写明；否则在迁 MySQL/Redis 前加文件锁或串行队列。

---

## §5 文件解析能力 — 合并 ×1 + 澄清 ×2

| TODO 行 | 当前 | 代码实证 | 结论 |
|---------|------|----------|------|
| L85 实现可解析 PDF 文本提取 | [ ] | 与 L86 重复 | **合并**：删 L85，保留 L86（[x]，含页码） |
| L86 …并保留页码用于引用 | [x] | `document_parser.py` 逐页 `page_number` ✓ | **保留** |
| L83 CSV 解析 | [ ] | `parse_document` 对非 `.pdf` 直接 `raise ValueError` | **保持**（确未做） |
| L84 Excel 解析 | [ ] | 同上，未做 | **保持** |
| L90 不支持文件类型错误提示 | [ ] | 仅抛 `ValueError`，被 worker 通用 `except` 兜底为「处理失败：{exc}」，无按类型友好提示 | **保持**，改写 → 「实现按文件类型的友好错误提示（当前仅通用异常兜底）」 |
| L91 超大/空/损坏文件提示 | [ ] | 空文本已处理（worker L185-205 给扫描件提示）；超大/损坏未做 | **拆分** → 空文本提示 **补勾**；超大/损坏 **保持 [ ]** |

---

## §6 大模型分析能力 — 补勾 ×3 + 合并 ×2 + 澄清 ×2

| TODO 行 | 当前 | 代码实证 | 结论 |
|---------|------|----------|------|
| L97 设计财务摘要 Prompt | [ ] | 与 L99 重复 | **合并**：删 L97，保留 L99 |
| L99 实现财务摘要 Prompt 初版 | [x] | `financial_summary.py` SYSTEM_PROMPT + 分片/合成 prompt ✓ | **保留** |
| L102 大模型调用封装（切换 baseURL/timeout/maxTokens/temperature） | [ ] | 与 L98 重复，且这些参数 `LLMConfig` 全部已配置化 | **合并**：删 L102，保留 L98 |
| L98 通义千问兼容 LLMProvider | [x] | ✓ | **保留** |
| L101 解析内容切片和压缩 | [ ] | `build_chunks`（`financial_summary.py` L40）已实现，按 `chunk_chars`/`max_chunks` 切 | **补勾** |
| L106 失败重试和超时控制 | [ ] | `LLMProvider.complete(max_retries=2)` 重试 ReadTimeout + `httpx.Timeout` 配置 | **补勾**（注：LLM 调用级已实现；任务级重试见 §4 L75） |
| L108 合并报告 | [ ] | `synthesize_final_report` ✓ | **补勾** |
| L109 保存模型名/Prompt版本/耗时/成本 | [ ] | 部分：model/provider/prompt_version/token 已存入 task JSON（worker L221-262），耗时与成本未存 | **保持**，改写 → 「补充调用耗时与估算成本（模型名/Prompt版本/token 已存）」 |
| L111 记录每次调用 token + request id | [ ] | 仅存聚合 token（`TokenUsage` 累加），每次调用明细与供应商 request id 未存 | **保持** |
| L103 LangGraph/LangChain 编排 | [ ] | 未用，纯函数流程 | **保持**，建议标注「目标栈，MVP 不阻塞」（与 §2B 呼应） |
| L104/L105 结构化 JSON 输出 + schema 校验 | [ ] | 输出为 Markdown，未做 JSON/schema | **保持** |
| L110 分片缓存 | [x] | `AnalysisCache` + sha256 ✓ | **正确** |
| L112 返回 token 汇总 | [x] | `format_token_usage` ✓ | **正确** |

---

## §7 飞书 Markdown 报告回复 — 补勾 ×1 + 澄清 ×1

| TODO 行 | 当前 | 代码实证 | 结论 |
|---------|------|----------|------|
| L148 报告过长拆多条/附件 | [ ] | `reply_text` 自动按 `max_chars` 拆多条（`client.py _split_text`）+ 超阈值发 `.md` 附件（worker L91-110） | **补勾** |
| L136 飞书 Markdown 格式报告回复 | [ ] | 已用文本消息回复 markdown 内容，但 `msg_type="text"` 不是飞书富文本/交互卡片 | **保持**，改写 → 「升级为飞书富文本/交互卡片渲染（当前以纯文本消息发送 markdown 源码，可读但不渲染）」 |
| L138-L145 展示各模块（概览/摘要/数据表/引用…） | [ ] | 报告内容由 L99 prompt 强制产出这些模块，但属「内容已含」，非「渲染保证」 | **保持**为验收项，注「内容层已由 prompt 覆盖，此处作为真实消息渲染验收」 |
| L147 用户反馈入口 | [ ] | 未做（与 §4 L77 反馈 API 联动） | **保持** |
| L149/L150/L151 大报告/.md/发送策略 | [x] | ✓ | **正确** |
| L152 实测附件权限 | [ ] | 外部真实账号验证，代码标注 `download_message_file` docstring 待验权限 | **保持** |

---

## §8 安全与数据保护 — 合并 ×1 + 澄清 ×1

| TODO 行 | 当前 | 代码实证 | 结论 |
|---------|------|----------|------|
| L157 飞书回调接口校验签名 | [ ] | 与 §3 L53 重复 | **合并**：二者合一，保留一条「飞书回调签名/加密校验」 |
| L162 文件大小限制 | [ ] | 未实现（L12 已决策 20MB 上限，但 worker/parser 无任何大小校验） | **保持**，建议提优先级（决策已定、实现缺失） |
| L163 文件类型白名单 | [ ] | 部分：parser 仅接受 `.pdf`，但无上传前白名单/大小门禁 | **保持**，改写 → 「增加上传前文件类型白名单与大小门禁（当前仅解析阶段拒绝非 PDF）」 |

---

## §9 部署与运维 — 补勾 ×5 + 保持若干

**更新说明**：初版对账未覆盖后续新增的 Docker 部署文件。当前工作区已经补齐远端长连接 worker 的基础 Docker 部署，但仍不是完整生产运维方案。

| TODO 行 | 当前状态 | 代码/文档实证 | 结论 |
|---------|----------|---------------|------|
| L176 Docker 和 Docker Compose | 已落地 | `Dockerfile`、`docker-compose.yml` | **补勾**（当前只部署长连接 worker） |
| L177 编写 `docker-compose.yml` | 已落地 | `docker-compose.yml` 仅包含 `feishu-worker` | **补勾** |
| 新增远端启动脚本 | 已落地 | `scripts/start.sh` 支持 `git pull --ff-only`、检查 `.env`、创建存储目录、重建容器 | **新增并补勾** |
| L179 配置后端环境变量 | 部分落地 | `.env.example` 已存在 | **补勾为环境变量模板**，真实 `.env` 仍由部署机填写 |
| L186 编写部署文档 | 部分落地 | `README.md` 已区分本地直接运行与远端 Docker 部署 | **补勾为基础部署文档** |
| L178 nginx / L175 HTTPS | 未做 | 无 nginx/TLS 配置 | **保持** |
| L180-L183 DB/Redis/备份 | 未做 | 当前无 MySQL/Redis | **保持** |
| L184-L185 监控/告警 | 未做 | 无监控配置；跨 chat 告警已回滚 | **保持** |

---

## §10 测试与验收 — 新增 ×1

**新增条目**：第 10 节有 L204「验证系统不会臆造材料中不存在的信息」，但未定义如何验证。建议补：

- [ ] **定义防幻觉验收方法**：构建带「标准答案/已知不存在项」的评测文件集，对报告逐项人工抽检（数据是否可溯源到页码、「未在材料中发现」是否被滥用/漏标），给出可量化的通过标准（如：客观数据表抽检 N 条全部可溯源、零臆造）。

> §10 其余测试项（CSV/Excel/超大文件/并发 5 任务/20MB）均为未来验收，与当前未实现功能对应，状态正确，保持。  
> 注意：L201「测试并发 5 个任务」与 §4 新增的「并发竞态」项强相关，实现前需先解决 JSON 无锁问题。

---

## 改动清单（已同步到 `TODO.md`）

### 删除/合并（4 处）

1. 删 §5 L85，并入 L86
2. 删 §6 L97，并入 L99
3. 删 §6 L102，并入 L98
4. §8 L157 与 §3 L53 合并为一条

### 补勾（14 处）

§3 L45/L58/L59；§4 L63；§5 空文本提示（拆自 L91）；§6 L101/L106/L108；§7 L148；§9 Docker / compose / 启动脚本 / `.env.example` / 基础部署文档。

### 改写表述（5 处）

§3 L53（token≠签名）；§4 L75（任务级 vs LLM 级重试）；§5 L90/L91（错误类型拆分）；§6 L109（补耗时/成本）；§7 L136（卡片渲染 vs 文本）

### 拆分（1 处）

§2 → §2A MVP 实际栈 / §2B 目标栈

### 新增（7 条）

事件幂等（§4）、并发竞态边界（§4）、远端启动脚本（§9）、基础部署文档状态（§9）、防幻觉验收方法（§10）、文件大小门禁提优先级（§8 L162 已存在但需标红）、富文本卡片渲染（§7 L136 改写后保留）

### 提优先级提醒

- **L162**（20MB 限制）
- **L78/L79**（数据留存清理）

这两块决策已拍板但代码零实现，且涉及安全与磁盘，建议在试用前补齐。
