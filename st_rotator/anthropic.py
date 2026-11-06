"""Anthropic Messages 端点支持（独立前缀，与 OpenAI 兼容的 ``/v1`` 完全分开）。

为什么要单独一条路径
--------------------
商汤上游同时提供两套 API：

* OpenAI 兼容：``POST /v1/chat/completions``（网关现有的 ``/v1`` 通道）
* Anthropic 兼容：``POST /v1/messages``（本次新增）

两套的**请求体语义、认证头、流式事件格式**都不同，若混在同一个 ``/v1`` 前缀下，
``/v1/messages`` 会被当成"未知端点原样透传"——沿用 OpenAI 的 ``Authorization: Bearer``
且不带 ``anthropic-version``，结果就是各种 4xx。所以这里给 Anthropic 客户端一条
**独立前缀**：把 ``ANTHROPIC_BASE_URL`` 指向 ``http://<host>:<port>/anthropic`` 即可。

实测确认的上游契约（2026-11-06，token.sensenova.cn）
---------------------------------------------------
* 路径：``POST /v1/messages``（``/anthropic/v1/messages``、``/v1/v1/messages`` 都是 404）
* 认证：``x-api-key``、``Authorization: Bearer`` **都接受**；``anthropic-version`` 可缺省
* 响应：标准 Anthropic ``{"type":"message","content":[{"type":"thinking"|"text"…}]}``
* 流式：标准 SSE 事件序列
  ``message_start → content_block_start → content_block_delta* →
    content_block_stop → message_delta → message_stop``
  （思考模型会先给 ``thinking_delta`` 块，再给 ``text_delta`` 块）
* 错误：``{"type":"error","error":{"type":"invalid_request_error","message":…}}``
* ``/v1/messages/count_tokens`` 上游**没有**（404），本网关也不提供

请求体清洗（实测哪些写法会被判 400）
------------------------------------
Anthropic 侧的严格点和 OpenAI 侧**不一样**，不能套用同一套规则：

    {"role":"assistant","content":null}   → 400 inference request is invalid
    {"role":"user","content":null}        → 400 inference request is invalid
    "system": null                        → 需要补 ""（原样可能 429/400）
    "temperature": "0.7"                  → 400 invalid arguments
    "max_tokens": "32"                    → 400 invalid arguments

而下列写法在 Anthropic 侧**是合法的**（不要乱改）：
    assistant.content: []（空 block 数组）、tool_result.content: null、
    metadata、top_k/top_p、message.name

注意 ``content`` 在 Anthropic 里既可以是字符串，也可以是 **block 数组**，
所以这里只把 ``null`` 补成 ``""``，绝不改动数组形态。
"""

from __future__ import annotations

import json
from typing import Any, Mapping

# Anthropic 版本头：上游实测可缺省，但补上更符合客户端预期。
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"

# 必须为数字的采样参数（实测传字符串会 400 invalid arguments）
_NUMERIC_PARAMS = ("temperature", "top_p", "top_k", "max_tokens")

# content 允许 null 的角色：全部都是 400，统一补空串
_NULLABLE_CONTENT_ROLES = ("assistant", "user")


def sanitize_message_request(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """把 Anthropic 请求体里会被商汤判 400 的写法规范成语义等价的安全写法。

    只做**语义等价**的最小改写，返回 (请求体, 改动清单)。

    规则（每条都对应一次对真实上游的实测）：
      * ``messages[*].content`` 为 ``null`` 的 assistant/user → ``""``；
      * 顶层 ``system`` 为 ``null`` → ``""``；
      * ``temperature`` / ``top_p`` / ``top_k`` / ``max_tokens`` 传字符串 → 转数字；
      * 顶层 ``metadata`` 保持原样（Anthropic 合法，上游也接受）。

    ``content`` 是 block 数组时**原样保留**——那是 Anthropic 的多模态/工具轮次专用形态。
    """
    changes: list[str] = []
    if not isinstance(payload, dict):
        return payload, changes

    body: dict[str, Any] = dict(payload)

    # ---- 数值参数：字符串转数字 ----
    for field in _NUMERIC_PARAMS:
        value = body.get(field)
        if isinstance(value, str):
            text = value.strip()
            try:
                parsed = float(text)
            except ValueError:
                continue
            # max_tokens 必须是整数；其余保留浮点
            body[field] = int(parsed) if field == "max_tokens" else parsed
            changes.append(field)

    # ---- system：null → "" ----
    if "system" in body and body["system"] is None:
        body["system"] = ""
        changes.append("system")

    # ---- messages[*].content：null → "" ----
    messages = body.get("messages")
    if isinstance(messages, list):
        fixed_messages: list[Any] = []
        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                fixed_messages.append(message)
                continue
            if message.get("role") in _NULLABLE_CONTENT_ROLES and message.get("content") is None:
                patched = dict(message)
                patched["content"] = ""
                fixed_messages.append(patched)
                changes.append(f"messages[{index}].content")
            else:
                fixed_messages.append(message)
        if changes:
            body["messages"] = fixed_messages

    return body, changes


def anthropic_error(err_type: str, message: str) -> dict[str, Any]:
    """Anthropic 形状的错误体：``{"type":"error","error":{...}}``。

    与 OpenAI 的 ``{"error":{...}}`` 不同——Anthropic 客户端（含 SDK）按这个形状解析，
    给错形状会让它把响应当成非错误从而抛更晦涩的异常。
    """
    return {"type": "error", "error": {"type": err_type, "message": message}}


def anthropic_error_type_for(status: int) -> str:
    """把 HTTP 状态映射成 Anthropic 的错误 type。"""
    if status == 400:
        return "invalid_request_error"
    if status == 401:
        return "authentication_error"
    if status == 403:
        return "permission_error"
    if status == 404:
        return "not_found_error"
    if status == 413:
        return "request_too_large"
    if status == 429:
        return "rate_limit_error"
    if status == 529 or status == 503:
        return "overloaded_error"
    if status >= 500:
        return "api_error"
    return "invalid_request_error"


def is_sse_response(payload: Mapping[str, Any] | None) -> bool:
    """请求是否要走流式（Anthropic 用 ``stream: true``）。"""
    return isinstance(payload, Mapping) and bool(payload.get("stream"))


def extract_message_usage(result: Any) -> tuple[int, int, int] | None:
    """从 Anthropic 响应里取 (输入, 输出, 缓存命中) token。

    Anthropic 的 usage 形状是
    ``{"input_tokens":N,"output_tokens":M,"cache_read_input_tokens":K, …}``。
    """
    if not isinstance(result, dict):
        return None
    usage = result.get("usage")
    if not isinstance(usage, dict):
        return None

    def _int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    return (
        _int(usage.get("input_tokens")),
        _int(usage.get("output_tokens")),
        _int(usage.get("cache_read_input_tokens")),
    )


def extract_message_model(result: Any) -> str | None:
    if isinstance(result, dict) and isinstance(result.get("model"), str):
        return result["model"]
    return None


def iter_sse_events(text: str) -> "list[tuple[str, str]]":
    """把 SSE 文本切成 (event, data) 列表。仅用于测试与调试。"""
    events: list[tuple[str, str]] = []
    name = ""
    data_lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())
        elif not line.strip():
            if data_lines:
                events.append((name, "\n".join(data_lines)))
            name, data_lines = "", []
    if data_lines:
        events.append((name, "\n".join(data_lines)))
    return events


def load_json_object(raw: bytes) -> dict[str, Any]:
    """解析请求体，返回 dict；非法时抛 ``ValueError``（由调用方转 400）。"""
    try:
        parsed = json.loads(raw or b"{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON body: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("body must be a JSON object")
    return parsed
