# 设计文档：Skill / Tool 可扩展架构 + 飞书卡片化路由

> 本文档面向**实现者（另一个 AI 模型或工程师）**。请严格按本文落地，不要自由发挥、不要引入文档未要求的抽象。
> 所有「现有接口签名」均已对照当前代码核对（截至本文撰写时），可直接调用。
> 所有「新增内容」给出了精确的文件路径、函数签名、数据结构与 JSON 负载。

---

## 0. 给实现者的总则（必读，违反即返工）

1. **不破坏现状行为**：现在「发文件→出财务摘要」「发文本→对最近文件追问」两条链路必须继续可用。本次改造是**在其上增加可扩展路由与新技能**，不是重写。
2. **最大化复用**：所有 LLM 调用走现有 `LLMProvider`；文档解析走现有 `parse_document`；事件去重走现有 `EventDeduplicator`；会话/文件画像走现有 `ConversationStore`。第 9 节有完整复用映射表，**优先查表，不要重新实现**。
3. **合规红线是硬约束**：本项目服务投顾场景，所有面向用户的 LLM 输出 prompt 必须包含「只用给定材料/出处、不臆造、标来源页码或 URL、不输出投资/审计/法律建议、检索结果与目标公司/行业不符时宁可留白绝不张冠李戴」。见第 6.8 节。
4. **不要过度设计**：不做 LLM 意图分类，不做 function-calling agent 自主循环，不做数据库。路由完全由「卡片按钮回调 + 会话状态 + 默认行为」三者决定，全程不靠模型猜意图。
5. **耗时任务必须异步**：飞书卡片回调有秒级超时，长任务（研究、分析）一律丢到后台线程，回调立即返回。见第 6.1 节「快回调慢执行」。
6. **改动前先读对应现有文件**，确认签名未变（本文档可能滞后于代码）。

---

## 1. 背景与目标

### 1.1 现状一句话
当前是一个**单一场景的垂直流水线**：飞书机器人收文件 → 解析 → Map-Reduce 出财务摘要 → 支持 RAG 多轮追问。入口与路由写死在一个函数里。

### 1.2 本次目标
让系统支持**新增领域技能（Skill）和工具（Tool）**，第一个要落地的新技能是「**行业研究**」（联网检索 + 带源改写 + 引用核验，目前仅以原型脚本存在于 `scripts/`，未集成）。

### 1.3 为什么用卡片而不是命令
飞书没有 slash 命令自动补全，强迫用户记忆 `/研究` 这类命令体验极差。因此**路由主入口是飞书交互卡片的按钮**：机器人发一张带按钮的卡片，用户点按钮，回调里直接携带 `skill_id`，命中零歧义、零记忆成本。

---

## 2. 现状架构（精确版）

### 2.1 目录与关键文件
```
backend/app/
  workers/feishu_ws.py          # 长连接 worker + 事件处理 + 两条业务链路（981 行，路由写死在此）
  integrations/feishu/
    client.py                   # FeishuClient：只会发 text 和 file 两种消息
    events.py                   # 事件解析：extract_file_message / extract_message_brief
  services/
    llm_provider.py             # LLM 抽象（complete / complete_until_done / embed / fallback）
    document_parser.py          # parse_document → ParsedDocument
    financial_summary.py        # generate_financial_summary_markdown（Map-Reduce 摘要）
    qa_service.py               # 追问 RAG（检索 + answer_question）
    conversation_store.py       # 每个 open_id 的文件画像 + 多轮历史（本地 JSON）
    task_store.py               # 任务状态（本地 JSON）
    analysis_cache.py           # 分片分析缓存 + sha256_file
    event_dedup.py              # EventDeduplicator：O_EXCL 原子去重
    markdown_report.py          # 无 LLM 时的解析预览报告
  core/config.py                # Settings（pydantic-settings + .env）
scripts/                        # 行业研究原型（未集成，本次要产品化的来源）
  grounded_section.py           # 单小节：检索词→智谱联网→带源改写→引用核验（四步闭环）
  build_from_template.py        # 无源文件：仅凭模板+公司名联网生成整篇 docx
  build_full_report.py          # 有源文件+联网生成整篇
  rewrite_by_template.py        # 按模板结构改写并渲染 docx（含 render_markdown_to_docx）
  zhipu_search_smoke.py         # 智谱 web search 探测
```

### 2.2 现状路由（要被替换的部分）
位置：`backend/app/workers/feishu_ws.py` 的 `handle_message_receive(data, settings)`（约 893-951 行）。

现状逻辑（伪代码）：
```
message_id = extract_message_id(payload)
if 重复事件: return                         # EventDeduplicator.mark_if_new
notify_admin_on_message(payload, settings)  # 旁路给管理员推送提醒
file_message = extract_file_message(payload)
if file_message is not None:
    start_file_processing(...)              # → 财务摘要链路（process_file_message_async）
else:
    question = extract_text_question(payload)
    if question is not None:
        start_question_processing(...)      # → 追问链路（process_question_async）
    else:
        忽略
```

worker 注册事件的位置：`main()`（约 954-977 行）：
```python
event_handler = (
    lark.EventDispatcherHandler.builder("", "")
    .register_p2_im_message_receive_v1(on_message_receive)
    .build()
)
cli = lark.ws.Client(app_id, app_secret, event_handler=event_handler)
cli.start()
```

### 2.3 两条现有业务链路的入口（要被包成 Skill 复用）
- **财务摘要**：`process_file_message_async(settings, message_id, file_key, file_name, sender_id, file_size)`（feishu_ws.py:235）。内部：下载→解析→`generate_financial_summary_markdown`→发报告→登记文件画像→预计算 embedding→建议追问问题。
- **追问 QA**：`process_question_async(settings, message_id, sender_id, question)`（feishu_ws.py:694）。内部：取最近文件→向量/关键词检索→`answer_question`→回复并记历史。

---

## 3. 名词定义

| 名词 | 定义 |
|------|------|
| **Skill（技能）** | 一个端到端处理用户请求的能力。例：财务摘要、追问、行业研究（联网）、模板改写（不联网）。每个 Skill 是一个类，注册到 `SkillRegistry`。 |
| **Tool（工具）** | 被 Skill 调用的可复用动作。例：联网检索（web_search）、文档解析、引用核验。 |
| **能力卡（capability menu card）** | 列出所有可用 Skill 按钮的卡片，用户首次对话/发「帮助」时下发。 |
| **下一步卡（next-step card）** | 文件分析完成后追发，提供「针对此文件」的后续动作按钮。 |
| **确认卡（confirm card）** | 对耗时/高成本 Skill（如研究），执行前发的「确认/取消」卡片。 |
| **完成卡（done card）** | 回调里用来**原地替换**原卡片、禁用按钮、提示「已开始/已处理」的卡片。 |
| **会话状态（SessionState）** | 每个用户当前归属哪个 Skill、在等什么输入。用于多轮归属。 |

---

## 4. 目标架构总览

### 4.1 新增/改动的目录结构
```
backend/app/
  skills/                       # 【新增目录】
    __init__.py
    base.py                     # Skill 协议 + SkillContext + SkillRegistry + SkillRouter + IncomingMessage + CardAction
    financial_summary_skill.py  # 包装现有摘要链路
    qa_skill.py                 # 包装现有追问链路
    industry_research_skill.py  # 新技能：把 scripts 四步联网管线产品化
    template_rewrite_skill.py   # 新技能：纯模板改写（无联网），产品化 rewrite_by_template.py
    help_skill.py               # 发能力卡（不是真业务，但用统一协议）
  tools/                        # 【新增目录】
    __init__.py
    base.py                     # Tool 协议 + ToolRegistry
    web_search.py               # 把 scripts 里的 zhipu_search 抽出来，独立配置
  services/
    session_store.py            # 【新增】会话状态存储（本地 JSON，仿 task_store）
    cards.py                    # 【新增】所有卡片 JSON 构建器
    template_docx.py            # 【新增】共享的模板/ docx 工具（从 scripts 迁入：大纲解析 + Markdown→docx 渲染 + 层级识别）
  integrations/feishu/
    client.py                   # 【改】新增 reply_card / send_card
    events.py                   # 【改】新增 extract_card_action（卡片回调归一化）
  workers/feishu_ws.py          # 【改】注册卡片回调 + handle_message_receive 改为走 SkillRouter
  core/config.py                # 【改】新增 search_* / session / card_dedup 配置
```

### 4.2 数据流总图
```
                         ┌─────────────────────────────────────────┐
飞书事件 ───────────────▶│  worker (feishu_ws.py)                   │
                         │                                          │
 ┌── im.message.receive ─┤  on_message_receive                      │
 │   (文本/文件)          │     → 归一化为 IncomingMessage           │
 │                       │     → SkillRouter.route_message()        │
 │                       │        ├ 有活跃会话 → 喂活跃 Skill        │
 │                       │        ├ 文件 → 默认摘要 Skill + 下一步卡 │
 │                       │        ├ 文本+有最近文件 → 追问 Skill     │
 │                       │        └ 否则 → 能力卡                    │
 │                       │                                          │
 └── card.action.trigger ┤  on_card_action （秒级超时！）           │
     (点击按钮)           │     → 去重(token) → 归一化为 CardAction   │
                         │     → 立即返回 toast + 完成卡(禁用按钮)   │
                         │     → 后台线程跑 SkillRouter.route_card() │
                         └─────────────────────────────────────────┘
                                          │
                                          ▼
                         ┌─────────────────────────────────────────┐
                         │  Skill.run(...)  在后台线程内             │
                         │   复用 LLMProvider / parse_document /     │
                         │   web_search tool / on_progress 通知      │
                         └─────────────────────────────────────────┘
```

---

## 5. 关键技术事实（已验证，照抄即可）

### 5.1 lark-oapi 卡片回调（已确认本地 SDK 支持）
- 注册方法：`EventDispatcherHandler.builder("","").register_p2_card_action_trigger(handler)`。
- 回调函数签名：`def handler(data: P2CardActionTrigger) -> P2CardActionTriggerResponse`。
- 导入路径：
  ```python
  from lark_oapi.event.callback.model.p2_card_action_trigger import (
      P2CardActionTrigger,
      P2CardActionTriggerResponse,
  )
  ```
- 事件字段（`data.event` 为 `P2CardActionTriggerData`）：
  | 字段 | 类型 | 说明 |
  |------|------|------|
  | `data.event.operator.open_id` | str | 点击者 open_id |
  | `data.event.token` | str | 本次回调 token，**用于去重** |
  | `data.event.action.value` | `Dict[str,Any]` | **按钮携带的负载**（我们放 `{"action":..., "skill_id":...}`） |
  | `data.event.action.tag` | str | 控件类型（button 等） |
  | `data.event.action.form_value` | `Dict[str,Any]` | 卡片表单输入（可选，进阶用） |
  | `data.event.action.input_value` | str | 单输入框值（可选） |
  | `data.event.context.open_message_id` | str | 卡片所在消息 id（用于原地更新卡片） |
  | `data.event.context.open_chat_id` | str | 会话 id |
- 返回值（`P2CardActionTriggerResponse`）支持两块：
  ```python
  return P2CardActionTriggerResponse({
      "toast": {"type": "info", "content": "已开始处理"},   # 顶部短提示
      "card": {"type": "raw", "data": <新卡片 dict>},        # 原地替换卡片（禁用按钮）
  })
  ```
  `toast.type` 可取 `info` / `success` / `error` / `warning`。

### 5.2 发送交互卡片（飞书 IM API）
通过现有 reply/send 接口，把 `msg_type` 设为 `"interactive"`，`content` 为卡片 JSON 的字符串：
```python
json={
    "msg_type": "interactive",
    "content": json.dumps(card_dict, ensure_ascii=False),
}
```
回复用：`POST {base_url}/im/v1/messages/{message_id}/reply`。
主动发用：`POST {base_url}/im/v1/messages?receive_id_type=open_id`，body 含 `receive_id`。
（与现有 `_reply_text_once` / `send_text` 完全同构，只是换 msg_type 和 content。）

### 5.3 交互卡片 JSON 结构（按钮带 value）
最小可用结构（**实现者照此模板生成，不要用更复杂的 schema 2.0**）：
```json
{
  "config": {"wide_screen_mode": true},
  "header": {
    "template": "blue",
    "title": {"tag": "plain_text", "content": "请选择要做什么"}
  },
  "elements": [
    {"tag": "div", "text": {"tag": "lark_md", "content": "这个文件接下来做什么？"}},
    {"tag": "action", "actions": [
      {
        "tag": "button",
        "text": {"tag": "plain_text", "content": "深入财务分析"},
        "type": "primary",
        "value": {"action": "run_skill", "skill_id": "financial_summary", "file_id": "<可选>"}
      },
      {
        "tag": "button",
        "text": {"tag": "plain_text", "content": "行业研究"},
        "type": "default",
        "value": {"action": "run_skill", "skill_id": "industry_research", "file_id": "<可选>"}
      }
    ]}
  ]
}
```
点击「行业研究」按钮后，回调里 `data.event.action.value == {"action":"run_skill","skill_id":"industry_research","file_id":"<可选>"}`。

---

## 6. 详细设计

### 6.1 飞书卡片管道

#### 6.1.1 `FeishuClient` 新增方法（client.py）
仿照现有 `_reply_text_once`（client.py:130）与 `send_text`（client.py:87），新增：

```python
async def reply_card(self, message_id: str, card: dict) -> None:
    """回复一张交互卡片到原消息会话。"""
    token = await self.get_tenant_access_token()
    async def _send() -> httpx.Response:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            resp = await client.post(
                f"{self.base_url}/im/v1/messages/{message_id}/reply",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "msg_type": "interactive",
                    "content": json.dumps(card, ensure_ascii=False),
                },
            )
            resp.raise_for_status()
            return resp
    await _with_retry(_send)

async def send_card(self, receive_id: str, card: dict, receive_id_type: str = "open_id") -> None:
    """主动给用户/群发送交互卡片。"""
    token = await self.get_tenant_access_token()
    async def _send() -> httpx.Response:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            resp = await client.post(
                f"{self.base_url}/im/v1/messages",
                params={"receive_id_type": receive_id_type},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                json={
                    "receive_id": receive_id,
                    "msg_type": "interactive",
                    "content": json.dumps(card, ensure_ascii=False),
                },
            )
            resp.raise_for_status()
            return resp
    await _with_retry(_send)
```
> 注意：复用文件顶部已有的 `_REQUEST_TIMEOUT`、`_with_retry`、`import json`、`import httpx`。

#### 6.1.2 卡片回调归一化（events.py 新增）
新增 dataclass + 解析函数：
```python
from dataclasses import dataclass, field

@dataclass
class CardAction:
    operator_id: str | None      # data.event.operator.open_id
    token: str                   # data.event.token，去重用
    open_message_id: str | None  # data.event.context.open_message_id
    chat_id: str | None          # data.event.context.open_chat_id
    action: str                  # value["action"]，如 "run_skill"/"confirm"/"cancel"
    skill_id: str | None         # value["skill_id"]
    args: dict = field(default_factory=dict)        # value 里除 action/skill_id 外的其余键
    form_value: dict = field(default_factory=dict)  # data.event.action.form_value（可选）

def extract_card_action(data) -> CardAction | None:
    """从 P2CardActionTrigger 提取归一化的 CardAction。结构缺失时返回 None。"""
    event = getattr(data, "event", None)
    if event is None:
        return None
    action_obj = getattr(event, "action", None)
    value = (getattr(action_obj, "value", None) or {}) if action_obj else {}
    if not isinstance(value, dict):
        value = {}
    operator = getattr(event, "operator", None)
    context = getattr(event, "context", None)
    action_name = str(value.get("action") or "")
    if not action_name:
        return None
    args = {k: v for k, v in value.items() if k not in ("action", "skill_id")}
    return CardAction(
        operator_id=getattr(operator, "open_id", None) if operator else None,
        token=str(getattr(event, "token", "") or ""),
        open_message_id=getattr(context, "open_message_id", None) if context else None,
        chat_id=getattr(context, "open_chat_id", None) if context else None,
        action=action_name,
        skill_id=value.get("skill_id"),
        args=args,
        form_value=(getattr(action_obj, "form_value", None) or {}) if action_obj else {},
    )
```

#### 6.1.3 worker 注册回调 + 快回调慢执行（feishu_ws.py）
在 `main()` 注册：
```python
def on_card_action(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
    return handle_card_action(data, settings)

event_handler = (
    lark.EventDispatcherHandler.builder("", "")
    .register_p2_im_message_receive_v1(on_message_receive)
    .register_p2_card_action_trigger(on_card_action)   # 新增
    .build()
)
```

`handle_card_action` 实现要点（**必须秒级返回**）：
```python
def handle_card_action(data, settings) -> P2CardActionTriggerResponse:
    from app.integrations.feishu.events import extract_card_action
    ca = extract_card_action(data)
    if ca is None:
        return P2CardActionTriggerResponse({"toast": {"type": "error", "content": "无效操作"}})

    # 幂等：飞书会重推回调，用 token 去重（独立目录，复用 EventDeduplicator）
    card_dedup = EventDeduplicator(settings.card_event_dedup_dir)
    if not card_dedup.mark_if_new(ca.token):
        return P2CardActionTriggerResponse({"toast": {"type": "info", "content": "处理中…"}})

    # cancel：清状态，原地替换卡片
    if ca.action == "cancel":
        SessionStore(settings.session_storage_dir).clear(ca.operator_id)
        return P2CardActionTriggerResponse({
            "toast": {"type": "info", "content": "已取消"},
            "card": {"type": "raw", "data": build_done_card("已取消。")},
        })

    # 其余动作：后台线程跑，回调立即返回（关键！研究/分析耗时远超回调超时）
    start_card_action_processing(settings, ca)
    return P2CardActionTriggerResponse({
        "toast": {"type": "info", "content": "已开始处理"},
        "card": {"type": "raw", "data": build_done_card("已收到，正在处理…")},
    })
```
其中 `start_card_action_processing` 仿照现有 `start_file_processing`（feishu_ws.py:616）用 `threading.Thread(daemon=True)` 起 `asyncio.run(route_card_async(...))`。

> **为什么必须异步**：飞书对卡片回调响应有秒级超时；行业研究要发多次 LLM + 多次联网检索，耗时几十秒到几分钟。若在回调里同步跑，飞书会超时重推，造成重复执行。

#### 6.1.4 后台线程里如何回复用户
卡片回调没有「原始用户消息的 message_id 可 reply」的天然语境，但有 `ca.operator_id`（open_id）和 `ca.chat_id`。后台线程用 `FeishuClient.send_text` / `send_card`（主动发，`receive_id_type="open_id"`，`receive_id=ca.operator_id`）下发进度和结果。
> 现有 `send_text`/`send_card` 支持 `receive_id_type="open_id"`，直接用。

---

### 6.2 卡片构建器（services/cards.py，新增）

集中放所有卡片 JSON 构建函数，**Skill 不直接拼 JSON**。`build_*_card` 由 `SkillRegistry` 提供的按钮元数据动态生成。

```python
from typing import Any

def _button(label: str, value: dict, type_: str = "default") -> dict:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": type_,
        "value": value,
    }

def build_capability_menu(buttons: list[dict]) -> dict:
    """能力入口卡：buttons 为各 Skill 的按钮元数据。"""
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "blue",
                   "title": {"tag": "plain_text", "content": "我能帮你做这些"}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md",
             "content": "点选下面的功能，或直接发文件 / 发问题给我。"}},
            {"tag": "action", "actions": [
                _button(b["label"],
                        {"action": "run_skill", "skill_id": b["skill_id"]},
                        "primary" if b.get("primary") else "default")
                for b in buttons
            ]},
        ],
    }

def build_next_step_card(file_id: str, buttons: list[dict]) -> dict:
    """文件分析完成后追发：针对此文件的后续动作。"""
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "turquoise",
                   "title": {"tag": "plain_text", "content": "这个文件接下来做什么？"}},
        "elements": [
            {"tag": "action", "actions": [
                _button(b["label"],
                        {"action": "run_skill", "skill_id": b["skill_id"], "file_id": file_id})
                for b in buttons
            ]},
        ],
    }

def build_confirm_card(skill_id: str, title: str, detail: str, args: dict) -> dict:
    """耗时/高成本 Skill 的确认卡。"""
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "orange",
                   "title": {"tag": "plain_text", "content": title}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": detail}},
            {"tag": "action", "actions": [
                _button("确认执行",
                        {"action": "confirm", "skill_id": skill_id, **args}, "primary"),
                _button("取消", {"action": "cancel"}, "default"),
            ]},
        ],
    }

def build_done_card(message: str) -> dict:
    """用于回调原地替换原卡片：纯文本、无按钮（=禁用）。"""
    return {
        "config": {"wide_screen_mode": True},
        "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": message}}],
    }
```

**value 契约（全系统统一，不得偏离）：**
| action | 触发后行为 | 必带字段 |
|--------|-----------|---------|
| `run_skill` | 启动某 Skill；若该 Skill 需确认则先发确认卡 | `skill_id`，可选 `file_id` |
| `confirm` | 用户确认后真正执行该 Skill | `skill_id` + 该 Skill 所需 args |
| `cancel` | 清会话状态、替换卡片为「已取消」 | 无 |

---

### 6.3 会话状态存储（services/session_store.py，新增）

仿照 `task_store.py`（每个 key 一个本地 JSON 文件）。**新建独立 store，不要塞进 conversation_store**（避免 schema 冲突）。

```python
from __future__ import annotations
import json, time
from pathlib import Path
from typing import Any

_SESSION_TTL_SECONDS = 30 * 60  # 30 分钟无活动则视为无活跃会话

class SessionStore:
    def __init__(self, base_dir: Path | str) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, open_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in open_id)
        return self.base_dir / f"{safe}.json"

    def get(self, open_id: str) -> dict[str, Any] | None:
        """返回活跃会话；超过 TTL 或不存在返回 None。"""
        p = self._path(open_id)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if time.time() - float(data.get("updated_at", 0)) > _SESSION_TTL_SECONDS:
            return None
        return data

    def set_active(self, open_id: str, skill_id: str,
                   awaiting: str | None = None, args: dict | None = None) -> None:
        self._path(open_id).write_text(json.dumps({
            "active_skill": skill_id,
            "awaiting": awaiting,
            "args": args or {},
            "updated_at": time.time(),
        }, ensure_ascii=False), encoding="utf-8")

    def clear(self, open_id: str) -> None:
        self._path(open_id).unlink(missing_ok=True)
```

**会话状态用法**：
- 用户点[行业研究]但还没给目标 → `set_active(open_id, "industry_research", awaiting="research_target")`，回文本「请输入要研究的公司名，或直接发给我一个文件」。
- 下一条消息进来：`route_message` 先查 `SessionStore.get`，若有 `awaiting`，把该消息交给对应 Skill 的 `resume`，然后 `clear`。

---

### 6.4 Skill 协议 / 注册表 / 路由器（skills/base.py，新增）

```python
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Protocol, Any

# ---- 归一化的入站消息 ----
@dataclass
class IncomingMessage:
    message_id: str
    sender_id: str | None
    chat_id: str | None
    msg_type: str                 # "text" | "file" | "image" | ...
    text: str = ""                # 文本消息内容（已去 @、trim）
    file_key: str | None = None
    file_name: str | None = None
    file_size: int | None = None
    raw_payload: dict = field(default_factory=dict)

# ---- Skill 在卡片上的呈现元数据 ----
@dataclass
class SkillButton:
    label: str                    # 按钮文案，如 "行业研究"
    primary: bool = False         # 是否高亮主按钮
    show_in_menu: bool = True     # 是否进能力卡
    show_in_next_step: bool = False  # 是否进文件「下一步」卡

# ---- 注入给 Skill 的运行上下文 ----
@dataclass
class SkillContext:
    settings: Any
    client: Any                   # FeishuClient
    tools: "ToolRegistry"
    compliance_prompt: str        # 合规红线，见 6.8
    # 各 store 实例（按需，由 worker 构造时注入）
    conversation_store: Any = None
    task_store: Any = None
    session_store: Any = None
    analysis_cache: Any = None

class Skill(Protocol):
    skill_id: str                 # 唯一 id，对应 value.skill_id
    needs_confirm: bool           # True → run_skill 时先发确认卡
    def button(self) -> SkillButton | None: ...
    # 由卡片按钮触发执行（在后台线程内 await）
    async def run(self, *, ctx: SkillContext, operator_id: str | None,
                  chat_id: str | None, args: dict) -> None: ...
    # 可选：处理「等待用户补充输入」状态下的后续消息
    async def resume(self, *, ctx: SkillContext, msg: IncomingMessage,
                     state: dict) -> None: ...

class SkillRegistry:
    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}
    def register(self, skill: Skill) -> None:
        self._skills[skill.skill_id] = skill
    def get(self, skill_id: str) -> Skill | None:
        return self._skills.get(skill_id)
    def menu_buttons(self) -> list[dict]:
        out = []
        for s in self._skills.values():
            b = s.button()
            if b and b.show_in_menu:
                out.append({"label": b.label, "skill_id": s.skill_id, "primary": b.primary})
        return out
    def next_step_buttons(self) -> list[dict]:
        out = []
        for s in self._skills.values():
            b = s.button()
            if b and b.show_in_next_step:
                out.append({"label": b.label, "skill_id": s.skill_id})
        return out
```

**SkillRouter**（决策逻辑，伪代码）：
```python
class SkillRouter:
    def __init__(self, registry: SkillRegistry, ctx_factory):
        self.registry = registry
        self.ctx_factory = ctx_factory  # () -> SkillContext

    async def route_card_async(self, ca):  # ca: CardAction
        ctx = self.ctx_factory()
        skill = self.registry.get(ca.skill_id) if ca.skill_id else None
        if skill is None:
            await ctx.client.send_text(ca.operator_id, "该功能暂不可用。")
            return
        # run_skill 且需确认 → 发确认卡，先不执行
        if ca.action == "run_skill" and skill.needs_confirm:
            card = build_confirm_card(skill.skill_id, "确认执行",
                                      _confirm_detail(skill, ca.args), ca.args)
            await ctx.client.send_card(ca.operator_id, card)
            return
        # run_skill(无需确认) 或 confirm → 真正执行
        await skill.run(ctx=ctx, operator_id=ca.operator_id, chat_id=ca.chat_id, args=ca.args)

    async def route_message_async(self, msg):  # msg: IncomingMessage
        ctx = self.ctx_factory()
        # 1) 有活跃会话且在等输入 → 交活跃 Skill.resume
        if msg.sender_id:
            state = ctx.session_store.get(msg.sender_id)
            if state and state.get("awaiting"):
                skill = self.registry.get(state["active_skill"])
                if skill is not None:
                    await skill.resume(ctx=ctx, msg=msg, state=state)
                    ctx.session_store.clear(msg.sender_id)
                    return
        # 2) 文件消息 → 默认摘要 Skill（保持现状），完成后追发「下一步」卡
        if msg.msg_type == "file":
            await self.registry.get("financial_summary").run(
                ctx=ctx, operator_id=msg.sender_id, chat_id=msg.chat_id,
                args={"message_id": msg.message_id, "file_key": msg.file_key,
                      "file_name": msg.file_name, "file_size": msg.file_size})
            return
        # 3) 文本消息
        if msg.msg_type == "text" and msg.text:
            # 问候/帮助 → 能力卡
            if msg.text.strip() in ("你好", "帮助", "help", "菜单", "?", "？"):
                await ctx.client.reply_card(msg.message_id,
                    build_capability_menu(self.registry.menu_buttons()))
                return
            # 否则 → 默认追问（保持现状）；QA Skill 内部会处理「无最近文件」的提示
            await self.registry.get("qa").run(
                ctx=ctx, operator_id=msg.sender_id, chat_id=msg.chat_id,
                args={"message_id": msg.message_id, "question": msg.text})
            return
        # 4) 其它类型忽略
```

> `route_card_async` / `route_message_async` 都在后台线程的 `asyncio.run(...)` 里执行（消息侧也建议异步起线程，与现状一致）。

---

### 6.5 三个 Skill 的实现

#### 6.5.1 FinancialSummarySkill（skills/financial_summary_skill.py）
**包装现有 `process_file_message_async`，几乎零改动。**
- `skill_id = "financial_summary"`，`needs_confirm = False`。
- `button()` → `SkillButton(label="深入财务分析", show_in_menu=True, show_in_next_step=True)`。
- `run(...)`：args 含 `message_id/file_key/file_name/file_size`，直接调用现有 `process_file_message_async(settings, message_id, file_key, file_name, sender_id=operator_id, file_size=file_size)`。
- **新增点**：在 `process_file_message_async` 成功发出报告后，追发「下一步」卡。
  - 落地方式：在 `process_file_message_async` 末尾（feishu_ws.py:553 之后，原「建议追问」之后）调用 `client.reply_card(message_id, build_next_step_card(task_id, registry.next_step_buttons()))`。
  - `file_id` 用现有的 `entry_key`（= `file_hash or task_id`，见 feishu_ws.py:488）。

#### 6.5.2 QASkill（skills/qa_skill.py）
**包装现有 `process_question_async`。**
- `skill_id = "qa"`，`needs_confirm = False`。
- `button()` → `SkillButton(label="追问文件内容", show_in_menu=True, show_in_next_step=True)`。
- `run(...)`：args 含 `message_id/question`，调用现有 `process_question_async(settings, message_id, sender_id=operator_id, question=question)`。
- 从「下一步卡」点进来（没有 question）时：`set_active(open_id,"qa",awaiting="question")` + 提示「请直接发文字提问」；下一条文本走 `resume` → 调 `process_question_async`。

#### 6.5.3 IndustryResearchSkill（skills/industry_research_skill.py）— 核心新功能
**把 `scripts/grounded_section.py` + `scripts/build_from_template.py` 的四步管线产品化。**
- `skill_id = "industry_research"`，`needs_confirm = True`（联网研究耗时且烧 token，必须确认）。
- `button()` → `SkillButton(label="行业研究", primary=True, show_in_menu=True, show_in_next_step=True)`。

**四步管线（从 scripts 搬迁，逐函数对应）：**
1. **生成检索词** —— 搬 `build_from_template.gen_queries` / `grounded_section.generate_queries`。输入：小节标题/写作要求/项目设定（公司名+事实）或源文件文本；输出 N 条中文检索词。
2. **联网检索** —— 调 `tools/web_search.py` 的 `WebSearchTool`（见 6.6，从 `grounded_section.zhipu_search` 抽出）。逐 query 检索、按 URL 去重、统一 `S#` 编号、上限 `search_max_sources`。
3. **带源改写** —— 搬 `grounded_section.grounded_rewrite`（有源文件）或 `build_from_template.fill_from_web`（无源文件）。用 `provider.complete_until_done`（自动续写）。**system prompt 必须含合规红线**（见 6.8）。
4. **引用核验** —— 搬 `grounded_section.verify_citations`，逐条核对被引出处是否支撑结论，产出核验表。

**输入获取（两种触发场景）：**
- **从「下一步卡」点进**（args 含 `file_id`）：以该文件为源，走「有源文件」管线（gen_queries + grounded_rewrite）。`file_id` → 查 `conversation_store` 拿 `pages_path` → `load_pages` 重组源文本。
- **从「能力卡」点进**（无 file_id）：走「无源文件」管线，需要目标公司名。
  - **稳健默认（先做这个）**：`run` 里发现无 file_id 且无公司名 → `session_store.set_active(open_id,"industry_research",awaiting="research_target")` + 发文本「请输入要研究的公司名」。下一条文本进 `resume`，拿到公司名后执行 `build_profile(company, facts)` + 无源管线。
  - **可选进阶（后做）**：在能力卡里用卡片表单输入框收公司名（`form_value`），一次提交直达。**Phase 3 先不做表单，用上面的状态机往返。**

**输出**：组装整篇 Markdown → 可选渲染 docx（搬 `rewrite_by_template.render_markdown_to_docx`，需 `python-docx` 依赖）→ 用 `client.send_text` 发正文 + 核验摘要，长则 `upload_file` + `_reply_file_once`/`reply_file` 发附件（复用现有报告下发逻辑，见 feishu_ws.py `send_report_result`）。
**进度**：复用「逐步 notify」模式，每步用 `client.send_text(operator_id, ...)` 告知「[1/4] 生成检索词…」等。

> ⚠️ 模板解析依赖：`scripts/build_from_template.py` 依赖 `build_full_report.parse_template_nodes` 与 `rewrite_by_template.render_markdown_to_docx` / `_infer_level`。产品化时把这些函数迁入 `backend/app/services/template_docx.py`，不要让 `backend` 反向 import `scripts/`。

#### 6.5.4 TemplateRewriteSkill（skills/template_rewrite_skill.py）— 纯模板改写（无联网）

**把 `scripts/rewrite_by_template.py` 产品化。与行业研究是两种不同诉求：本技能只把【源文件】内容按【模板】结构重排成稿，不联网、不补充外部信息。**
- `skill_id = "template_rewrite"`，`needs_confirm = False`（不联网、相对快；两文件收集本身已是显式交互，不再加确认）。受 `enable_template_rewrite` 开关控制。
- `button()` → `SkillButton(label="按模板改写", show_in_menu=True, show_in_next_step=True)`。

**能力**：以「模板」为基底文档（保留其主题字体、页面设置、页眉页脚），清空正文后按模板章节骨架重写；内容只来自「源文件」，缺某节信息写「（源文件中未提供相应内容）」，不臆造、不篡改数值/名称/日期。

**关键特殊性：需要两个文件（源文件 + 模板）**，且**模板必须是 .docx 原始文件保留在磁盘上**（`extract_template_outline` / `render_markdown_to_docx` 用 python-docx 直接读模板文件本身，不是解析文本）。

**输入收集（多步状态机，复用 `SessionStore`）：**
- **从能力卡进**（无文件上下文）：
  1. `set_active(open_id, "template_rewrite", awaiting="source_file")` → 提示「请先发送【源文件】（内容来源，支持 PDF/Word/Excel/CSV）」。
  2. 用户发文件 → `resume`：取源文本（见下），存入 `args["source_text_path"]` → `set_active(..., awaiting="template_file", args=...)` → 提示「已收到源文件，请再发送【模板文件】(.docx)」。
  3. 用户发文件 → `resume`：校验后缀必须 `.docx`（否则提示重发）→ 下载为本地 .docx → 两者就绪 → 跑管线 → `clear`。
- **从文件「下一步」卡进**（`args["file_id"]` = 刚上传的文件作源）：
  1. `set_active(open_id, "template_rewrite", awaiting="template_file", args={"source_file_id": file_id})` → 提示「请发送【模板文件】(.docx)」。
  2. 用户发 .docx → `resume` → 跑管线。

> `route_message_async`（§6.4）的第 1 步「有 awaiting 则交 resume」保证**文件消息在等待态会进 `resume`**，而不是被默认摘要链路截走。`resume` 要能处理 `msg.msg_type == "file"`（用 `msg.file_key` + `msg.message_id` 下载）。

**源文本两种来源**：
- 来自 `file_id`（下一步卡）：`conversation_store` 里有该文件的 `pages_path` → `load_pages` 拼出全文文本（源只需文本，无需原始文件）。
- 来自即时上传（`awaiting="source_file"`）：`download_message_file` + `parse_document(...).text`。

**管线（搬 `rewrite_by_template.py`，逐函数对应）：**
1. **模板大纲** → `extract_template_outline(template_path) -> str`（迁入 `template_docx.py`）。把模板序列化成带层级 + 「> 填写要求：」的大纲。
2. **源文本** → 见上（文本即可）。
3. **源文件压缩**（过长才触发）→ `condense_source(text, provider, budget=SOURCE_CHAR_BUDGET)`。超 `SOURCE_CHAR_BUDGET`(36000) 先分片压缩成保真事实笔记。
4. **按模板改写** → `rewrite_to_markdown(outline, source_text, provider)`，内部用 `provider.complete_until_done`（自动续写）。
5. **渲染 docx** → `render_markdown_to_docx(markdown, template_path, output_path)`（迁入 `template_docx.py`，继承模板主题/页眉页脚）。

**输出**：`upload_file` + `reply_file`/`_reply_file_once` 把改写后的 .docx 发给用户（复用现有报告下发）。进度用 `send_text(operator_id, ...)` 通知「[1/4] 解析模板…」等。

**合规 prompt（重要区别）**：本技能用 `rewrite_by_template.py` 现有的 `REWRITE_SYSTEM_PROMPT` 原文（已含「只用源文件 / 不臆造 / 保真数值名称日期 / 缺失写占位」）。**不要**注入联网相关的【S编号】/URL 规则——本技能无联网、无出处，套用那套规则会让模型困惑。即：合规约束按技能特性区分，研究用 §6.8 的 `COMPLIANCE_PROMPT`，改写用 `REWRITE_SYSTEM_PROMPT`。

**迁移落点**：
- 结构化 docx 工具（`extract_template_outline`、`render_markdown_to_docx` 及其全部 `_clear_body`/`_add_*`/`_render_table` 助手、`_infer_level`/`_para_signals`/`_style_heading_level`）→ `services/template_docx.py`（与行业研究共用）。
- 改写专属的 LLM 步骤（`condense_source`、`rewrite_to_markdown`、`REWRITE_SYSTEM_PROMPT`、`CONDENSE_SYSTEM_PROMPT`、`_chunk_text`）→ 放 `skills/template_rewrite_skill.py` 或 `services/template_rewrite.py`。

---

### 6.6 Tool 层（tools/，新增）

#### 6.6.1 Tool 协议（tools/base.py）
```python
from typing import Protocol, Any

class Tool(Protocol):
    name: str
    async def run(self, **kwargs) -> Any: ...

class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, "Tool"] = {}
    def register(self, tool: "Tool") -> None:
        self._tools[tool.name] = tool
    def get(self, name: str) -> "Tool":
        return self._tools[name]
```

#### 6.6.2 WebSearchTool（tools/web_search.py）
从 `scripts/grounded_section.py:zhipu_search`（76 行）抽出，**改用独立配置**（见 6.7），不再偷用 embedding key。
```python
import httpx

class WebSearchTool:
    name = "web_search"
    def __init__(self, base_url: str, api_key: str, engine: str = "search_std"):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.engine = engine

    async def run(self, query: str) -> list[dict]:
        """返回 [{title, url, content, date}, ...]。失败抛异常，由调用方兜底。"""
        url = self.base_url + "/web_search"
        async with httpx.AsyncClient(timeout=40) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"},
                json={"search_engine": self.engine, "search_query": query},
            )
            resp.raise_for_status()
        results = resp.json().get("search_result") or []
        return [
            {"title": r.get("title", ""), "url": r.get("link") or "",
             "content": (r.get("content") or "").strip(),
             "date": r.get("publish_date") or ""}
            for r in results if r.get("link")
        ]
```
> 智谱接口约定（来自现有原型）：`POST {base}/web_search`，body `{"search_engine","search_query"}`，返回 `search_result[]`，每项有 `title/link/content/publish_date`。`base` 默认 `https://open.bigmodel.cn/api/paas/v4`。

---

### 6.7 配置新增项（core/config.py + .env.example）

在 `Settings` 增加（放在 embedding 配置之后）：
```python
    # 联网检索（web search）独立配置：此前原型偷用 qa_embedding_api_key，现拆开。
    search_provider: str = "zhipu"
    search_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    search_api_key: str = ""
    search_engine: str = "search_std"
    search_max_sources: int = 8          # 单次研究喂给改写的去重后出处上限
    search_queries_per_section: int = 3  # 每小节生成的检索词条数

    # 会话状态 / 卡片回调去重
    session_storage_dir: str = "storage/sessions"
    card_event_dedup_dir: str = "storage/card_events"

    # 技能开关（关闭则不出现在能力卡 / 拒绝触发）
    enable_industry_research: bool = True
    enable_template_rewrite: bool = True
```
增加一个向后兼容属性（**让现有 .env 不改也能跑**）：
```python
    @property
    def search_key(self) -> str:
        """web search 的 key：未单独配置则回退到此前偷用的 qa_embedding_api_key。"""
        return self.search_api_key.strip() or self.qa_embedding_api_key.strip()

    @property
    def search_endpoint(self) -> str:
        return self.search_base_url.strip() or self.qa_embedding_base_url.strip() \
            or "https://open.bigmodel.cn/api/paas/v4"
```
`.env.example` 同步追加对应注释项（`SEARCH_API_KEY=` 等）。

---

### 6.8 合规红线共享注入

新建常量（建议放 `skills/base.py` 或单独 `skills/compliance.py`）：
```python
COMPLIANCE_PROMPT = (
    "必须严格遵守：\n"
    "1. 只使用给定材料 / 联网出处中的客观内容，不臆造、不补全、不推断未给信息。\n"
    "2. 联网出处的每条数据/结论句末标【S编号】（编号只能用给定出处的），来自源文件标（源文件），"
    "来自项目设定标（项目设定）。\n"
    "3. 不编造 URL 或编号。多源数据冲突时并列标注，不擅自取舍。\n"
    "4. 若联网出处与目标公司/行业明显不符（检索回来是其他公司或行业），不得采用，"
    "宁可写「（未检索到与目标公司相关的可靠来源）」，绝不张冠李戴。\n"
    "5. 不输出投资建议、审计意见、法律意见或确定性风险结论。\n"
    "6. 输出中文 Markdown。"
)
```
- 注入方式：`SkillContext.compliance_prompt` 携带，每个 Skill 拼 system prompt 时前置/合并它。
- 第 4、5 条来自真实教训（commit 654c06b 修过「竞争地位写成钢铁/比亚迪」的张冠李戴问题），**不可删减**。

---

## 7. 消息处理总流程（最终形态伪代码）

```python
# === 文本/文件事件 ===
def handle_message_receive(data, settings):
    payload = json.loads(lark.JSON.marshal(data))
    message_id = extract_message_id(payload)
    if message_id and not EventDeduplicator(settings.event_dedup_dir).mark_if_new(message_id):
        return                                  # 现有去重，保留
    notify_admin_on_message(payload, settings)  # 现有旁路，保留
    msg = normalize_incoming(payload)           # 文件/文本 → IncomingMessage（去@、trim）
    if msg is None:
        return
    start_message_processing(settings, msg)     # 后台线程 → router.route_message_async(msg)

# === 卡片回调事件（秒级超时）===
def handle_card_action(data, settings) -> P2CardActionTriggerResponse:
    ca = extract_card_action(data)
    if ca is None:
        return resp_toast("error", "无效操作")
    if not EventDeduplicator(settings.card_event_dedup_dir).mark_if_new(ca.token):
        return resp_toast("info", "处理中…")
    if ca.action == "cancel":
        SessionStore(settings.session_storage_dir).clear(ca.operator_id)
        return resp_toast_and_card("info", "已取消", build_done_card("已取消。"))
    start_card_action_processing(settings, ca)  # 后台线程 → router.route_card_async(ca)
    return resp_toast_and_card("info", "已开始处理", build_done_card("已收到，正在处理…"))
```

`normalize_incoming`：复用现有 `extract_file_message`（→ 文件）与 `extract_text_question`（→ 文本）的解析逻辑，组装成 `IncomingMessage`；文本要去掉 `@_user_x` 提及占位（用 `event.message.mentions` 映射）并 trim。

---

## 8. 实施阶段与验收标准

> 每阶段做完都要跑第 8.4 节「回归检查」，确认现状未被破坏。

### Phase 1：卡片管道最小闭环（不接业务）
**做**：
- `client.py` 加 `reply_card` / `send_card`。
- `events.py` 加 `CardAction` + `extract_card_action`。
- `config.py` 加 `card_event_dedup_dir`。
- `cards.py` 加 `build_capability_menu` / `build_done_card`（先 hardcode 两个假按钮，如「测试A/测试B」）。
- `feishu_ws.py`：`main()` 注册 `register_p2_card_action_trigger`；`handle_card_action` 实现「去重 → toast → 替换为完成卡」；收到文本「菜单」时 `reply_card` 发能力卡。
**验收**：
1. 给机器人发「菜单」，收到一张带两个按钮的卡片。
2. 点按钮 → 顶部出现 toast「已开始处理」，卡片原地变为「已收到，正在处理…」（按钮消失）。
3. 重复点同一按钮（或飞书重推）不触发二次处理（token 去重生效）。
4. 日志打印出 `CardAction`（action/skill_id/operator_id）。

### Phase 2：路由层 + 现有两链路 Skill 化（行为不变 + 多了卡片）
**做**：
- `skills/base.py`（协议/注册表/路由器/IncomingMessage/SkillContext）。
- `services/session_store.py`。
- `FinancialSummarySkill` / `QASkill`（包装现有 `process_file_message_async` / `process_question_async`）。
- `HelpSkill` 或在 router 内处理「菜单/帮助」→ 能力卡（按钮来自 `registry.menu_buttons()`）。
- `handle_message_receive` 改为 `normalize_incoming` + `router.route_message_async`。
- 文件分析完成后追发「下一步」卡（`build_next_step_card`）。
**验收**：
1. 发文件 → 仍出财务摘要（与改造前逐字一致）→ 之后多收到一张「下一步」卡。
2. 发文本（已有最近文件）→ 仍正常追问（与改造前一致）。
3. 点「下一步」卡的「追问文件内容」→ 提示发问 → 发文本 → 正确回答该文件。
4. 发「菜单」→ 能力卡按钮与已注册 Skill 一致（新增 Skill 自动出现）。

### Phase 3：行业研究 Skill 落地
**做**：
- `tools/web_search.py`（`WebSearchTool`）+ `config.py` 的 `search_*` 配置 + 向后兼容属性。
- 把 `scripts` 的模板解析/docx 渲染函数迁入 `backend/app/services/template_docx.py`。
- `skills/industry_research_skill.py`：四步管线 + `needs_confirm=True` + 确认卡 + 进度通知 + 合规红线 prompt + 状态机收公司名。
- 注册到 `SkillRegistry`（受 `enable_industry_research` 开关控制）。
**验收**：
1. 能力卡出现「行业研究」按钮。点击 → 提示输入公司名 → 输入「比亚迪」→ 收到确认卡 → 点确认 → 收到逐步进度 → 最终收到带【S#】引用与「来源」段的报告 + 引用核验摘要。
2. 从文件「下一步」卡点「行业研究」→ 以该文件为源走有源管线，正常产出。
3. 联网出处与目标公司无关时，对应小节输出「（未检索到与目标公司相关的可靠来源）」，不张冠李戴。
4. 研究全程不阻塞：回调秒回，进度/结果通过主动消息下发。

### Phase 3b：模板改写 Skill 落地（可与 Phase 3 并行或紧随）
> 依赖 Phase 3 已迁好的 `services/template_docx.py`（两技能共用）。
**做**：
- 把 `rewrite_by_template.py` 的改写专属步骤（`condense_source` / `rewrite_to_markdown` / `REWRITE_SYSTEM_PROMPT` / `CONDENSE_SYSTEM_PROMPT` / `_chunk_text`）迁入 `skills/template_rewrite_skill.py` 或 `services/template_rewrite.py`。
- `skills/template_rewrite_skill.py`：`needs_confirm=False` + 两文件多步状态机（源文件→模板）+ `resume` 处理文件消息 + 模板后缀 .docx 校验 + 进度通知 + 四步管线 + 发回 docx。
- 注册到 `SkillRegistry`（受 `enable_template_rewrite` 开关控制）。
**验收**：
1. 能力卡出现「按模板改写」按钮。点击 → 提示发源文件 → 发文件 → 提示发模板 → 发 .docx → 收到逐步进度 → 最终收到改写后的 .docx 附件（继承模板主题/页眉页脚）。
2. 从文件「下一步」卡点「按模板改写」→ 以该文件为源 → 提示发模板 → 发 .docx → 正常产出。
3. 模板缺某节对应信息时，该节正文为「（源文件中未提供相应内容）」，不臆造。
4. 等待模板态发了非 .docx 文件 → 被提示重发 .docx，状态不丢失。
5. 改写不联网：全程无 web_search 调用。

### 8.4 回归检查（每阶段必跑）
- 发 PDF / docx / xlsx / csv → 财务摘要正常。
- 超大文件 / 不支持类型 → 仍被上传门禁拦截并提示。
- 文本追问 → 向量检索优先、关键词兜底，附「参考第 X 页」。
- 事件重推（同 message_id）→ 不重复处理。
- 未配置 `LLM_API_KEY` → 文件返回解析预览，不崩。

---

## 9. 复用映射表（实现时优先查此表）

| 需求 | 直接调用的现有接口 | 文件:行 |
|------|-------------------|---------|
| LLM 文本补全 | `LLMProvider.complete(messages)` | llm_provider.py:187 |
| LLM 补全+自动续写 | `LLMProvider.complete_until_done(messages)` | llm_provider.py:252 |
| 构造 chat provider（含 fallback） | `build_chat_provider(LLMConfig(...), settings.fallback_models)` | llm_provider.py:403 |
| LLM 配置对象 | `LLMConfig(provider, base_url, api_key, model, timeout_ms, max_tokens, temperature)` | llm_provider.py:16 |
| 文档解析 | `parse_document(Path) -> ParsedDocument(file_type,page_count,text,pages)` | document_parser.py:37 |
| 财务摘要主流程 | `generate_financial_summary_markdown(document, provider, chunk_chars, max_pages, max_chunks, prompt_version, file_hash, reduce_group_size, reduce_max_chars, map_concurrency, cache, on_progress)` | financial_summary.py:125 |
| 追问回答 | `answer_question(question, context, history, provider, retrieval_mode)` | qa_service.py:320 |
| 文件内检索（关键词） | `retrieve_pages(question, pages, top_k, max_chars)` | qa_service.py:249 |
| 文件内检索（向量） | `retrieve_by_embedding(question, chunks, provider, model, top_k, max_chars)` | qa_service.py:203 |
| 读分页文本 | `load_pages(pages_path) -> list[dict]` | qa_service.py:59 |
| 关键词抽取 | `extract_keywords(text, top_n)` | qa_service.py:41 |
| 文件画像登记 | `ConversationStore.upsert_file(open_id, file_id=, file_name=, pages_path=, summary=, keywords=, file_hash=, embeddings_path=)` | conversation_store.py:66 |
| 列出最近文件 | `ConversationStore.list_files(open_id)` | conversation_store.py:170 |
| 多轮历史 | `ConversationStore.recent_history / append_history` | conversation_store.py:136/176 |
| 事件去重 | `EventDeduplicator(dir).mark_if_new(key) -> bool` | event_dedup.py:31 |
| 任务状态 | `TaskStore.create_task / update_task` | task_store.py:14/47 |
| 文件 sha256 | `sha256_file(path)` | analysis_cache.py:97 |
| 发文本（回复原消息） | `FeishuClient.reply_text(message_id, text)` | client.py:120 |
| 发文本（主动） | `FeishuClient.send_text(receive_id, text, receive_id_type)` | client.py:87 |
| 发文件附件 | `FeishuClient.reply_file(message_id, path, file_name)` | client.py:148 |
| 上传文件取 file_key | `FeishuClient.upload_file(path, file_name)` | client.py:162 |
| 下载消息文件 | `FeishuClient.download_message_file(message_id, file_key, target_path)` | client.py:211 |
| 联网检索（原型，待抽出） | `scripts/grounded_section.py:zhipu_search` | grounded_section.py:76 |
| 检索词生成（原型） | `build_from_template.gen_queries` / `grounded_section.generate_queries` | — |
| 带源改写（原型） | `grounded_section.grounded_rewrite` / `build_from_template.fill_from_web` | — |
| 引用核验（原型） | `grounded_section.verify_citations` | grounded_section.py:152 |
| 模板节点解析（原型） | `build_full_report.parse_template_nodes` | — |
| Markdown→docx（原型） | `rewrite_by_template.render_markdown_to_docx`（+ `_clear_body`/`_add_*`/`_render_table` 助手） | rewrite_by_template.py:301 |
| 模板大纲序列化（原型） | `rewrite_by_template.extract_template_outline` | rewrite_by_template.py:119 |
| 源文件保真压缩（原型） | `rewrite_by_template.condense_source` | rewrite_by_template.py:163 |
| 按模板改写（原型） | `rewrite_by_template.rewrite_to_markdown`（system=`REWRITE_SYSTEM_PROMPT`） | rewrite_by_template.py:194 |

---

## 10. 边界与坑（清单，逐条检查）

1. **回调超时**：`handle_card_action` 内禁止任何 LLM/网络长调用，一律丢后台线程，立即返回 toast+完成卡。
2. **回调幂等**：用 `data.event.token` 去重（独立目录 `card_event_dedup_dir`）。漏了会重复执行研究、重复烧 token。
3. **按钮防重复点**：回调返回时用 `build_done_card` 原地替换卡片（无按钮）。
4. **后台线程回复用户**：卡片回调无可 reply 的 message_id，用 `send_text/send_card` + `receive_id=operator_id, receive_id_type="open_id"`。
5. **backend 不许 import scripts/**：原型函数要迁入 `backend/app/services/`，否则部署（Docker）时 `scripts/` 不在 import path 会崩。
6. **web search key 拆分**：新增 `search_api_key`，但用 `search_key` 属性回退到 `qa_embedding_api_key`，保证现有 `.env` 不改也能跑。
7. **合规红线不可删**：第 6.8 节 prompt 全文注入研究/改写类调用，尤其第 4 条（防张冠李戴）。
8. **会话 TTL**：`SessionStore` 30 分钟过期，避免用户隔天发消息被误判为「还在等公司名」。
9. **文本去 @**：群聊文本含 `@_user_x` 占位，归一化时按 `event.message.mentions` 删除再判断。
10. **docx 依赖**：渲染 docx 需 `python-docx`，确认已在依赖中（原型 import `from docx import Document`）；若仅发 Markdown 则不需要。
11. **模板必须是磁盘上的原始 .docx**：模板改写技能的 `extract_template_outline` / `render_markdown_to_docx` 用 python-docx 直接读模板**文件本身**（继承主题/页眉页脚），不能只传解析文本。等待模板态务必校验后缀为 `.docx`，并把下载的模板文件留在磁盘直到渲染完成。
12. **两文件收集是多步状态**：模板改写要按顺序收「源文件 + 模板」，`awaiting` 会经历 `source_file`→`template_file` 两态；`resume` 必须能处理**文件消息**（不止文本）。非法/缺失输入时给提示但不要 `clear` 状态，避免用户重头再来。
13. **飞书权限**：发 interactive 卡片、收 card.action.trigger 回调需在开放平台开通（见第 11 节）。
14. **不要新增 message_type 路由分支硬编码**：所有「该消息归谁」的决策只在 `SkillRouter` 里，新增 Skill 不准再回去改 `handle_message_receive`。

---

## 11. 飞书开放平台配置 checklist
- 事件订阅方式：**长连接**（现状，保持）。
- 订阅事件：在原有「接收消息 im.message.receive_v1」基础上，**新增「卡片回调 / card.action.trigger」**。
- 权限：接收消息、读取消息中的文件、回复消息、**发送消息（含交互卡片）**。
- 卡片回调地址：长连接模式由 SDK 处理，无需单独配 URL；确认开放平台「机器人 - 卡片回调」未被禁用。
- 用真实机器人账号验证：卡片下发、按钮点击回调、原地更新卡片三件事都要在真实飞书里点一遍（开放平台调试器不完全等价）。

---

## 12. 明确的非目标（不要做）
- ❌ 不做 LLM 意图分类 / 自然语言路由（保持确定性，避免误判烧 token）。
- ❌ 不做 function-calling agent 自主循环。
- ❌ 不引入数据库 / 消息队列 / 向量库中间件（继续本地 JSON + 线程）。
- ❌ 不做多渠道（继续只支持飞书）。
- ❌ Phase 3 不做卡片表单输入框（先用状态机往返收公司名；表单作为后续可选优化）。
- ❌ 不重写现有摘要 / 追问的内部实现，只做包装。

---

## 13. 附：最小可运行验证命令
```bash
# 启动 worker（现状方式不变）
PYTHONPATH=backend .venv/bin/python -m app.workers.feishu_ws

# 行业研究原型（产品化前可先单独验证管线，参数来自 scripts）
PYTHONPATH=backend .venv/bin/python scripts/build_from_template.py \
    --template ~/Desktop/模板.docx --company "比亚迪" --fact "新能源汽车制造商"
PYTHONPATH=backend .venv/bin/python scripts/grounded_section.py \
    --source ~/Desktop/源文件.docx --template ~/Desktop/模板.docx --section 市场规模
```

---

*文档结束。实现时如发现现有签名与本文不符，以代码为准，并在对应小节就近修正后再继续。*
