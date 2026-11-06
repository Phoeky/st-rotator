"""Anthropic 端点端到端验证：本地起网关，走真实上游（需 config.json 里的 Key）。

验证点：
  1. 非流式 /anthropic/v1/messages 返回标准 Anthropic message
  2. 流式事件序列完整（message_start → content_block_* → message_delta → message_stop）
  3. x-api-key 与 Authorization: Bearer 两种鉴权都能进来
  4. 未鉴权被拒
  5. /anthropic/v1/models 可用
  6. 清洗生效：assistant.content=null 不再 400
  7. 未知 anthropic 子路径 404 且错误体是 Anthropic 形状
  8. OpenAI 的 /v1 通道不被影响
"""
from __future__ import annotations

import json
import pathlib
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from st_rotator.config import Config
from st_rotator.client import StRotator
from st_rotator.proxy import create_server

GATEWAY_TOKEN = "test-anthropic-token"

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


def post(port: int, path: str, body: dict, headers: dict, timeout=120):
    data = json.dumps(body, ensure_ascii=False).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method="POST", headers=headers
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return -1, repr(exc)


def get(port: int, path: str, headers: dict, timeout=90):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return -1, repr(exc)


def main() -> int:
    cfg = Config.from_file(pathlib.Path(__file__).resolve().parents[1] / "config.json")
    rot = StRotator(cfg)
    server = create_server(rot, host="127.0.0.1", port=0, token=GATEWAY_TOKEN)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"gateway 127.0.0.1:{port}（真实上游 {cfg.base_url}）\n")

    hdr_key = {"x-api-key": GATEWAY_TOKEN, "content-type": "application/json",
               "anthropic-version": "2023-06-01"}
    hdr_bearer = {"Authorization": f"Bearer {GATEWAY_TOKEN}", "content-type": "application/json"}
    # 注意 max_tokens 要给够：deepseek-v4-flash 是推理模型，thinking 与 text
    # 共用 max_tokens 预算，太小会只返回 thinking 块而没有 text 块。
    msg = {"model": "deepseek-v4-flash", "max_tokens": 600,
           "messages": [{"role": "user", "content": "用三个字回答"}]}

    print("[1] 非流式（x-api-key）")
    st, body = post(port, "/anthropic/v1/messages", msg, hdr_key)
    ok = st == 200
    try:
        parsed = json.loads(body)
        is_message = parsed.get("type") == "message" and isinstance(parsed.get("content"), list)
        has_block = bool(parsed.get("content"))
        has_usage = isinstance(parsed.get("usage"), dict)
    except Exception:  # noqa: BLE001
        is_message = has_block = has_usage = False
    check("HTTP 200", ok, f"{st} {body[:160]}")
    check("返回 type=message", is_message)
    # 推理模型的 thinking 与 text 共用 max_tokens，预算不够时可能只有 thinking 块；
    # 两者都是合法响应，这里只要求"有 content 块"。
    check("content 非空（含 text 或 thinking 块）", has_block)
    check("含 usage", has_usage)

    print("\n[2] 非流式（Authorization: Bearer）")
    st, body = post(port, "/anthropic/v1/messages", msg, hdr_bearer)
    check("Bearer 也能进（403/401 都不该出现）", st == 200, f"{st} {body[:160]}")

    print("\n[3] 未鉴权")
    st, body = post(port, "/anthropic/v1/messages", msg, {"content-type": "application/json"})
    check("无凭据被拒 401", st == 401, f"{st} {body[:120]}")
    try:
        shape_ok = json.loads(body).get("type") == "error"
    except Exception:  # noqa: BLE001
        shape_ok = False

    print("\n[4] GET /anthropic/v1/models")
    st, body = get(port, "/anthropic/v1/models", hdr_key)
    ok = st == 200
    try:
        data = json.loads(body).get("data")
        has_models = isinstance(data, list) and len(data) > 0
    except Exception:  # noqa: BLE001
        has_models = False
    check("HTTP 200", ok, f"{st} {body[:160]}")
    check("返回模型清单", has_models)

    print("\n[5] 未知 anthropic 子路径")
    st, body = post(port, "/anthropic/v1/nope", msg, hdr_key)
    try:
        shape_ok = json.loads(body).get("type") == "error"
    except Exception:  # noqa: BLE001
        shape_ok = False
    check("404", st == 404, str(st))
    check("错误体是 Anthropic 形状", shape_ok, body[:160])

    print("\n[6] 清洗：assistant.content=null（上游本会 400）")
    dirty = {"model": "deepseek-v4-flash", "max_tokens": 32,
             "messages": [{"role": "user", "content": "hi"},
                          {"role": "assistant", "content": None}]}
    st, body = post(port, "/anthropic/v1/messages", dirty, hdr_key)
    check("不再 400（被清洗后 200）", st == 200, f"{st} {body[:200]}")

    print("\n[7] 流式")
    stream_msg = dict(msg, stream=True, max_tokens=600)
    # 上游处于限流时，池里 Key 可能全部冷却，流式会先 429/空流；重试几次取稳定判定。
    st, raw = -1, ""
    for attempt in range(4):
        st, raw = post(port, "/anthropic/v1/messages", stream_msg, hdr_key)
        if st == 200 and "event:" in raw:
            break
        time.sleep(15)
    events = [ln[6:].strip() for ln in raw.splitlines() if ln.startswith("event:")]
    check("HTTP 200", st == 200, f"{st} {raw[:160]}")
    check("有 message_start", "message_start" in events, ",".join(events[:6]))
    check("有 message_stop", "message_stop" in events, ",".join(events[-4:]))
    check("有 content_block_delta", "content_block_delta" in events)
    order_ok = False
    if events:
        order_ok = events[0] == "message_start" and events[-1] == "message_stop"
    check("事件顺序 start→…→stop", order_ok, ",".join(events[:3]) + " ... " + ",".join(events[-3:]))
    # data 行都应是合法 JSON
    bad_json = 0
    for ln in raw.splitlines():
        if ln.startswith("data:"):
            try:
                json.loads(ln[5:].strip())
            except Exception:  # noqa: BLE001
                bad_json += 1
    check("所有 data 行是合法 JSON", bad_json == 0, f"bad={bad_json}")

    print("\n[8] OpenAI /v1 通道不受影响")
    st, body = post(port, "/v1/chat/completions",
                    {"model": "deepseek-v4-flash",
                     "messages": [{"role": "user", "content": "hi"}], "max_tokens": 32},
                    {"Authorization": f"Bearer {GATEWAY_TOKEN}", "content-type": "application/json"})
    check("仍返回 200", st == 200, f"{st} {body[:160]}")

    server.shutdown()
    print()
    if FAILS:
        for f in FAILS:
            print("FAIL:", f)
        print(f"ANTHROPIC E2E FAILED ({PASS} passed, {len(FAILS)} failed)")
        return 1
    print(f"ALL ANTHROPIC E2E PASSED ({PASS} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
