# 商汤 400 "inference request is invalid" 排查报告

日期：2026-10-30　目标：`deepseek-v4-flash`　环境：生产机 `124.223.163.57`（st-rotator v1.7.0，端口 8899）

## 结论（先说答案）

**这个 400 不是 st-rotator 造成的，是商汤网关对请求体本身的判定被如实透传。**

同一份 payload，「直连上游」与「经 st-rotator 网关」得到**完全一致**的响应：

```
direct  HTTP 400 | ('inference request is invalid', 'invalid_request_error', 'invalid_parameter_error')
gateway HTTP 400 | ('inference request is invalid', 'invalid_request_error', 'invalid_parameter_error')
状态码一致: True      错误三元组一致: True
```

常见误解是"直连正常、过网关报错"，但实测：**直连这个 payload 也照样 400**。直连之所以看起来正常，
是因为客户端/框架在直连时用的是**另一份请求体**（或它自己做了字段清洗），而不是因为走了不同链路。

## 复现方法（最小用例）

```json
{
  "model": "deepseek-v4-flash",
  "messages": [
    {"role": "user", "content": "hi"},
    {"role": "assistant", "content": null}
  ]
}
```

→ `HTTP 400 {"message":"inference request is invalid","type":"invalid_request_error","code":"invalid_parameter_error"}`

## 触发规则（逐条实测，20 组用例）

### 会 400 的形态

| 请求体片段 | 上游返回 |
|---|---|
| `{"role":"assistant","content":null}`（**没有** tool_calls） | 400 `inference request is invalid` / `invalid_parameter_error` |
| `{"role":"assistant"}`（连 content 字段都没有） | 400 同上 |
| `{"role":"system","content":null}` | 400 同上 |
| `{"role":"user","content":null}` | 400 `user content required` / code `3` |
| 多轮历史中**任何一条** assistant `content:null` 且无 tool_calls | 400 同上（与位置无关） |
| `logprobs: true` | 400 `inference request is invalid` |
| `metadata: {...}` | 400 `invalid arguments` / code `3` |
| `temperature: 3`（越界） | 400 `field Temperature invalid, should be in [0.0, 2.0)` |

### 能正常 200 的形态（安全写法）

| 请求体片段 | 结果 |
|---|---|
| `{"role":"assistant","content":null,"tool_calls":[...]}` | **200**（带 tool_calls 时 null 合法） |
| `{"role":"assistant","content":""}` | **200**（空串可用） |
| `{"role":"assistant","content":"文本"}` | 200 |
| 历史中间的空串 content | 200 |
| `role:"tool"` 消息 | 200 |
| `response_format` / `thinking` / `enable_thinking` / `reasoning_effort` | 200（`deepseek-v4-flash` 不拒收） |
| `tools` / `tool_choice:"auto"` / `tools[*].function.strict:null` / `parallel_tool_calls` | 200 |
| 多条 `role:"system"` | 200 |
| `seed` / `user` / `n` / `stream_options` | 200 |
| `max_tokens: 70000` | 200（未触发上限） |

> 注：网上有资料说 SenseNova 拒收 `response_format`、`tool_choice:"auto"`、多条 system、`strict:null` 等，
> 那些结论针对的是 **`sensenova-6.7-flash-lite`** 等模型。在 **`deepseek-v4-flash`** 上实测这些字段**都能通过**。
> 因此不要照搬那套清洗规则，只需要处理 `content` 的 null/缺失。

## 根因分析

Agent 框架（Claude Code / Codex / WorkBuddy 等）在**工具调用轮次**里，习惯用
`{"role":"assistant","content":null,"tool_calls":[...]}` 表示"这一轮只有工具调用、没有文本"。
OpenAI 官方与 DeepSeek 直连都接受；商汤网关的 schema 校验更严，在**没有 tool_calls 兜底**时
（例如工具调用结束后的收尾轮、或被客户端拆掉 tool_calls 的那条）就判定为非法消息。

关键事实：**st-rotator 对请求体是逐字节透传**。用假上游抓包对比客户端原始 body 与网关实际发往上游的 body：

```
CASE null-content-assistant  payload 完全一致（字段/值/顺序无关）✔
CASE tool-history            payload 完全一致 ✔
CASE chinese-emoji           payload 完全一致 ✔
CASE unknown-extra-fields    payload 完全一致 ✔
CASE stop-and-penalties      payload 完全一致 ✔
CASE stream-false-explicit   差异: .stream False 被丢弃（非流式请求不带该字段，无害）
CASE empty-string-model      差异: .model '' -> 'deepseek-v4-flash'（回落到默认模型）
```

网关唯一会改的两处都无害：非流式请求去掉 `stream:false`、空模型名回落默认模型。

## 修复方案

### 方案 A（推荐）：在客户端侧清洗

在把历史发给上游前，把 messages 里**除了带 tool_calls 的 assistant 之外**的
`content` 为 `null` 或缺失的消息，统一规范成空串 `""`：

```python
def normalize_content(messages):
    out = []
    for m in messages:
        m = dict(m)
        if m.get("content") is None and not m.get("tool_calls"):
            m["content"] = ""
        if "content" not in m:
            m["content"] = ""
        m.pop("logprobs", None)      # 商汤不接受
        m.pop("metadata", None)      # 商汤不接受
        out.append(m)
    return out
```

实测效果（修复前 → 修复后）：

| 形态 | 修复前 | 修复后 |
|---|---|---|
| assistant `content:null`（无 tc） | 400 | **200 ✔** |
| assistant 缺 content 字段 | 400 | **200 ✔** |
| system `content:null` | 400 | **200 ✔** |
| 多轮历史中间 `content:null` | 400 | **200 ✔** |
| assistant `content:null` + tool_calls | 200 | 200（保持不动）✔ |

### 方案 B：在 st-rotator 网关侧清洗

在 `proxy._handle_chat_inner` 拆出 `messages` 之后加同样的规范化，
好处是**所有接入的 Agent 都自动受益**，无需逐个改客户端。

## 需要的改动位置（方案 B）

- `st_rotator/proxy.py` `_handle_chat_inner()`：`messages = params.pop("messages", None)` 之后
- `st_rotator/proxy.py` `_forward_simple()`：POST 透传端点同样处理
- 需要新增一个 `sanitize_messages()` 工具函数 + 单元测试

---

# 第二轮排查（v2.0.1）：`code: "3"` 的残余 400

v2.0.0 上线后用户反馈**仍然 400**，但 `code` 变成了 `"3"`（v2.0.0 修掉的那类是
`invalid_parameter_error`）。说明是**另一批触发点**。补测 20 组写法后确认：

## 新增的 400 触发点（逐条对真实上游验证）

| 请求体片段 | 上游返回 | 是否已修 |
|---|---|---|
| `{"role":"developer","content":"…"}` | 400 `inference request is invalid` | ✅ 映射成 `system` |
| `{"role":"tool","content":null}` / 缺 `content` | 400 `inference request is invalid` | ✅ 补 `""` |
| `tool_calls[*].function.arguments` 传**对象** | 400 `invalid arguments` (`code:"3"`) | ✅ 序列化成字符串 |
| `content` 传数字（`123`） | 400 `invalid arguments` | ✅ 转字符串 |
| `temperature` 传字符串（`"0.7"`） | 400 `invalid arguments` | ✅ 转数字 |
| `frequency_penalty` 越界（`5`） | 400 `should be in [-2, 2]` | ❌ 故意不改（用户意图问题） |
| `temperature` 越界（`3`） | 400 `should be in [0.0, 2.0)` | ❌ 故意不改 |
| `{"role":"user","content":null}` | 400 `user content required` | ❌ 故意不改（真的是空用户轮） |

**最可能的残余原因：`role: "developer"`。** 它是 OpenAI 新规范里替代 `system` 的角色，
Codex 等较新的 Agent 框架会大量使用；商汤只认 `system`。
实测 `developer` → 400，同一份内容改成 `system` → 200。

## v2.0.1 的完整清洗规则

```python
_ROLE_ALIASES = {"developer": "system"}

def sanitize_messages(messages):
    for m in messages:
        # 1) 角色名映射：developer -> system
        # 2) content 缺失/null 的 assistant|system|tool -> ""
        #    （带 tool_calls 的 assistant 保持 null 不动）
        # 3) tool_calls[*].function.arguments 传对象 -> json.dumps 成字符串
        # 4) content 传数字/布尔 -> str()

def sanitize_params(params):
    # 摘掉 logprobs / top_logprobs / metadata
    # temperature / top_p / frequency_penalty / presence_penalty 传字符串 -> float()
```

## 真正安全的做法：直接抓线上请求体

本轮排查最大的教训是**靠猜字段效率太低**。更快的路径是让网关记录"上游返回 400 时
的原始请求体"，直接看到 Agent 到底发了什么。建议后续加一个可选的 debug 开关。

## 附：本次排查用到的服务器命令

```bash
# 服务定义（含管理 Token）
systemctl cat st-rotator.service

# 看日志
tail -60 /opt/st-rotator/rotator.log

# 配置
cat /opt/st-rotator/config.json
```

探测脚本已存于服务器 `/tmp/`：`fieldprobe.py`、`nullprobe.py`、`ruleprobe.py`、
`gwcmp.py`、`patcmp.py`、`finalproof.py`、`fixverify.py`（对应输出同名 `.out`）。
