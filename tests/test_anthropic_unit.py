"""Anthropic 端点离线单元测试：清洗规则、错误形状、SSE 渲染、路由解析。

不联网、不消耗额度。真实链路由 ``_test_anthropic_e2e.py`` 覆盖。
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from st_rotator import anthropic

FAILS: list[str] = []
PASS = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAILS.append(f"{name}{' — ' + detail if detail else ''}")
        print(f"  FAIL  {name}{' — ' + detail if detail else ''}")


def main() -> int:
    print("[1] 清洗：content 为 null")
    payload = {
        "model": "deepseek-v4-flash",
        "max_tokens": 32,
        "messages": [
            {"role": "user", "content": None},
            {"role": "assistant", "content": None},
        ],
    }
    body, changes = anthropic.sanitize_message_request(payload)
    check("user null → \"\"", body["messages"][0]["content"] == "")
    check("assistant null → \"\"", body["messages"][1]["content"] == "")
    check("记录了两处改动", len(changes) == 2, str(changes))
    check("不改动原始入参", payload["messages"][0]["content"] is None)

    print("\n[2] 清洗：content 是 block 数组时保持不动")
    arr = [{"type": "text", "text": "hi"}]
    payload2 = {"model": "m", "max_tokens": 8, "messages": [{"role": "user", "content": arr}]}
    body2, changes2 = anthropic.sanitize_message_request(payload2)
    check("数组原样保留", body2["messages"][0]["content"] == arr)
    check("无改动", changes2 == [], str(changes2))

    print("\n[3] 清洗：空数组 content 也是合法的（不动）")
    payload3 = {"model": "m", "max_tokens": 8,
                "messages": [{"role": "user", "content": "hi"},
                             {"role": "assistant", "content": []}]}
    body3, changes3 = anthropic.sanitize_message_request(payload3)
    check("空数组不动", body3["messages"][1]["content"] == [])
    check("无改动", changes3 == [], str(changes3))

    print("\n[4] 清洗：system 为 null")
    payload4 = {"model": "m", "max_tokens": 8, "system": None,
                "messages": [{"role": "user", "content": "hi"}]}
    body4, changes4 = anthropic.sanitize_message_request(payload4)
    check("system null → \"\"", body4["system"] == "")
    check("记录了 system", "system" in changes4, str(changes4))

    print("\n[5] 清洗：数值参数传字符串")
    payload5 = {"model": "m", "max_tokens": "64", "temperature": "0.7", "top_p": "0.9",
                "messages": [{"role": "user", "content": "hi"}]}
    body5, changes5 = anthropic.sanitize_message_request(payload5)
    check("max_tokens 转 int", body5["max_tokens"] == 64 and isinstance(body5["max_tokens"], int))
    check("temperature 转 float", body5["temperature"] == 0.7)
    check("top_p 转 float", body5["top_p"] == 0.9)
    check("记录了 3 处", len([c for c in changes5 if c in ("max_tokens", "temperature", "top_p")]) == 3, str(changes5))

    print("\n[6] 清洗：非法字符串不硬转（交给上游如实报错）")
    payload6 = {"model": "m", "max_tokens": "abc", "messages": [{"role": "user", "content": "hi"}]}
    body6, _ = anthropic.sanitize_message_request(payload6)
    check("max_tokens 保持原样", body6["max_tokens"] == "abc")

    print("\n[7] 清洗：metadata / top_k 等合法字段不动")
    payload7 = {"model": "m", "max_tokens": 8, "top_k": 10, "metadata": {"user_id": "u"},
                "messages": [{"role": "user", "content": "hi"}]}
    body7, changes7 = anthropic.sanitize_message_request(payload7)
    check("top_k 保留", body7["top_k"] == 10)
    check("metadata 保留", body7["metadata"] == {"user_id": "u"})
    check("无改动", changes7 == [], str(changes7))

    print("\n[8] 错误形状")
    err = anthropic.anthropic_error("invalid_request_error", "bad")
    check("外层 type=error", err.get("type") == "error")
    check("内层 type 正确", err["error"]["type"] == "invalid_request_error")
    check("内层 message 正确", err["error"]["message"] == "bad")
    check("400 → invalid_request_error", anthropic.anthropic_error_type_for(400) == "invalid_request_error")
    check("401 → authentication_error", anthropic.anthropic_error_type_for(401) == "authentication_error")
    check("403 → permission_error", anthropic.anthropic_error_type_for(403) == "permission_error")
    check("404 → not_found_error", anthropic.anthropic_error_type_for(404) == "not_found_error")
    check("429 → rate_limit_error", anthropic.anthropic_error_type_for(429) == "rate_limit_error")
    check("503 → overloaded_error", anthropic.anthropic_error_type_for(503) == "overloaded_error")
    check("500 → api_error", anthropic.anthropic_error_type_for(500) == "api_error")

    print("\n[9] usage 提取")
    result = {"type": "message", "usage": {"input_tokens": 12, "output_tokens": 34,
                                           "cache_read_input_tokens": 5}}
    check("提取三元组", anthropic.extract_message_usage(result) == (12, 34, 5))
    check("缺失 usage 返回 None", anthropic.extract_message_usage({"type": "message"}) is None)
    check("非法值容错", anthropic.extract_message_usage({"usage": {"input_tokens": "x"}}) == (0, 0, 0))

    print("\n[10] stream 判定")
    check("stream:true", anthropic.is_sse_response({"stream": True}) is True)
    check("stream:false", anthropic.is_sse_response({"stream": False}) is False)
    check("无 stream", anthropic.is_sse_response({"model": "m"}) is False)

    print("\n[11] 请求体解析")
    obj = anthropic.load_json_object(b'{"model":"m"}')
    check("正常解析", obj == {"model": "m"})
    try:
        anthropic.load_json_object(b"not json")
        ok = False
    except ValueError:
        ok = True
    check("非法 JSON 抛 ValueError", ok)
    try:
        anthropic.load_json_object(b"[1,2]")
        ok2 = False
    except ValueError:
        ok2 = True
    check("非对象抛 ValueError", ok2)

    print("\n[12] SSE 切分工具")
    text = ("event: message_start\ndata: {\"type\":\"message_start\"}\n\n"
            "event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n")
    events = anthropic.iter_sse_events(text)
    check("切出 2 个事件", len(events) == 2, str(events))
    check("事件名正确", events[0][0] == "message_start" and events[1][0] == "message_stop")
    check("data 可解析", json.loads(events[0][1])["type"] == "message_start")

    print("\n[13] 非 dict 入参不炸")
    body13, changes13 = anthropic.sanitize_message_request(None)  # type: ignore[arg-type]
    check("None 原样返回", body13 is None and changes13 == [])

    print()
    if FAILS:
        for f in FAILS:
            print("FAIL:", f)
        print(f"ANTHROPIC UNIT FAILED ({PASS} passed, {len(FAILS)} failed)")
        return 1
    print(f"ALL ANTHROPIC UNIT PASSED ({PASS} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
