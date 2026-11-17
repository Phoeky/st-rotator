"""商汤逐模型兼容策略：仅修正已验证的问题，不改变未知模型的请求。

规则同步自 dsh-sensenova-pool 的 a302915 / 44cf33c（2026-11-17）。
OpenAI 聊天参数与 Anthropic thinking 语义不同，本模块不处理后者。
"""
from __future__ import annotations

from typing import Any

REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
MODEL_REASONING_EFFORTS = {
    "glm-5.3": ("low", "high", "max"),
    "glm-5.2": REASONING_EFFORTS,
    "deepseek-v4-flash": REASONING_EFFORTS,
    "sensenova-6.8-flash": REASONING_EFFORTS,
    "sensenova-6.8-flash-lite": REASONING_EFFORTS,
    # 未确认的档位不发送；不要把某个模型的映射推广到其他模型。
    "kimi-k3": REASONING_EFFORTS[:-1],
    "deepseek-flash": ("none", "minimal", "low", "medium", "high", "max"),
}


def _model_id(value: Any) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


def apply_chat_model_policy(payload: dict[str, Any]) -> dict[str, Any]:
    """在最终模型确定后原地修正 OpenAI Chat 参数；不补缺省字段。"""
    model = _model_id(payload.get("model"))
    vocab = MODEL_REASONING_EFFORTS.get(model)
    if vocab is not None and "reasoning_effort" in payload:
        raw = payload["reasoning_effort"]
        effort = raw.strip().lower() if isinstance(raw, str) else ""
        if effort == "off":
            effort = "none"
        if effort not in REASONING_EFFORTS:
            payload.pop("reasoning_effort")
        elif effort in vocab:
            payload["reasoning_effort"] = effort
        elif effort in ("xhigh", "max"):
            payload["reasoning_effort"] = vocab[-1]
        else:
            rank = REASONING_EFFORTS.index(effort)
            candidates = [v for v in vocab if REASONING_EFFORTS.index(v) <= rank]
            payload["reasoning_effort"] = candidates[-1] if candidates else vocab[0]
    if model == "kimi-k3" and "temperature" in payload:
        payload["temperature"] = 1
    return payload


def patch_model_metadata(item: dict[str, Any]) -> dict[str, Any]:
    """返回目录条目的副本，保留扩展字段与未知模型的上游值。

    2026-11-17 源项目实测：Flash 输入 1,047,536 tokens 仍成功，
    约 1.05M 才失败，上限为 2²⁰；上游却报 262144。Lite 实测仍是 256K。
    此覆盖只修元数据，不扩大真实窗口，也不改写请求的 max_tokens。
    上游将来调整真实限制时，应复验并更新或撤销这条精确匹配规则。
    """
    patched = dict(item)
    if _model_id(item.get("id") or item.get("name")) == "sensenova-6.8-flash":
        patched["context_length"] = 1048576
    return patched
