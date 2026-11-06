"""离线回归测试：网关行为契约（不联网）。

覆盖两条最容易回归、且代价极高的不变量：

1. **调用方凭据头绝不透传给上游**（x-api-key / authorization）。
   透传的后果实测是上游返回 ``401 {"error":{"code":16,"message":"Forbidden"}}``，
   被判定为「凭据失效」，一整池好 Key 全被打成 INVALID，只能等 invalid_ttl 或重启。

2. **Anthropic 端点路由与错误形状**（独立前缀，不与 /v1 混用）。
"""
from __future__ import annotations

import json
import pathlib
import sys
import threading
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from st_rotator.client import StRotator
from st_rotator.config import AccountConfig, CooldownConfig, Config
from st_rotator.keypool import KeyPool
from st_rotator.proxy import create_server
from st_rotator.transport import HttpClient, Request, Response

GATEWAY_TOKEN = "tk-regression"
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


class Trap:
    """假上游：记录收到的请求头，回一个成功响应。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.lock = threading.Lock()

    def __call__(self, req: Request) -> Response:
        with self.lock:
            self.calls.append({"path": req.path, "headers": dict(req.headers),
                               "body": (req.body or b"").decode("utf-8", "replace")})
        path = req.path or ""
        if path.endswith("messages"):
            payload = {
                "id": "msg_test", "type": "message", "role": "assistant",
                "model": "deepseek-v4-flash",
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1,
                          "cache_read_input_tokens": 0},
            }
        else:
            payload = {"object": "list", "data": [{"id": "deepseek-v4-flash", "object": "model"}]}
        return Response(200, {"Content-Type": "application/json"},
                        json.dumps(payload).encode())


def build(trap: Trap):
    cfg = Config(
        base_url="https://token.sensenova.cn/v1",
        default_model="deepseek-v4-flash",
        accounts=[AccountConfig(name="a1", api_keys=["sk-upstream-1"], max_concurrency=4)],
        cooldown=CooldownConfig(base=1.0, factor=2.0, max=8.0, jitter=0.0),
        max_attempts=2,
    )
    rot = StRotator(cfg, pool=KeyPool(cfg.accounts, cfg.cooldown),
                    client=HttpClient(cfg.base_url, handler=trap))
    return rot


def post(port: int, path: str, body: dict, headers: dict):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(),
        method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return -1, repr(exc)


def get(port: int, path: str, headers: dict):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return -1, repr(exc)


def main() -> int:
    trap = Trap()
    rot = build(trap)
    server = create_server(rot, host="127.0.0.1", port=0, token=GATEWAY_TOKEN)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"gateway 127.0.0.1:{port}（假上游）\n")

    msg = {"model": "deepseek-v4-flash", "max_tokens": 16,
           "messages": [{"role": "user", "content": "hi"}]}

    print("[1] 凭据头绝不透传（Anthropic 路径 / x-api-key）")
    with trap.lock:
        trap.calls.clear()
    st, body = post(port, "/anthropic/v1/messages", msg,
                    {"x-api-key": GATEWAY_TOKEN, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"})
    check("请求成功", st == 200, f"{st} {body[:160]}")
    with trap.lock:
        call = trap.calls[0] if trap.calls else None
    if call:
        hdrs = {k.lower(): v for k, v in call["headers"].items()}
        # Anthropic 路径按上游惯例用 x-api-key 承载**池 Key**（不是调用方凭据）。
        upstream_key = str(hdrs.get("x-api-key", ""))
        check("x-api-key 装的是池 Key（不是调用方凭据）",
              upstream_key.startswith("sk-upstream"),
              f"x-api-key={upstream_key[:12]}…")
        check("上游未收到网关 token", GATEWAY_TOKEN not in json.dumps(hdrs),
              "泄漏了调用方凭据")
        check("未同时带上 Authorization（避免双凭据冲突）",
              "authorization" not in hdrs, str(hdrs.get("authorization"))[:40])
    else:
        check("有上游调用", False, "没有发出任何上游请求")

    print("\n[2] 凭据头绝不透传（OpenAI 路径 / Bearer）")
    with trap.lock:
        trap.calls.clear()
    st, body = post(port, "/v1/chat/completions",
                    {"model": "deepseek-v4-flash",
                     "messages": [{"role": "user", "content": "hi"}]},
                    {"Authorization": f"Bearer {GATEWAY_TOKEN}",
                     "x-api-key": GATEWAY_TOKEN, "content-type": "application/json"})
    with trap.lock:
        call = trap.calls[0] if trap.calls else None
    if call:
        hdrs = {k.lower(): v for k, v in call["headers"].items()}
        check("上游未收到 x-api-key", "x-api-key" not in hdrs)
        check("上游用池 Key", "sk-upstream" in str(hdrs.get("authorization", "")))
    else:
        check("有上游调用", False)

    print("\n[3] 上游 401 才判失效；403 非凭据文案不算失效")
    from st_rotator.client import classify
    from st_rotator.transport import Response as R

    def cls(status: int, body_text: str) -> str:
        return classify(R(status, {}, body_text.encode()), body_text)[0]

    check("401 判 invalid", cls(401, '{"error":{"message":"invalid api key"}}') == "invalid")
    check("403 凭据文案判 invalid", cls(403, '{"error":{"message":"invalid api key"}}') == "invalid")
    check("403 Forbidden 不算失效", cls(403, '{"error":{"code":16,"message":"Forbidden"}}') != "invalid")
    check("429 限流判 retry", cls(429, '{"type":"error","error":{"type":"rate_limit_error","message":"inference exceeds tpm/rpm limit"}}') == "retry")

    print("\n[4] Anthropic 路由（独立前缀）")
    st, body = post(port, "/anthropic/v1/messages/", msg,
                    {"x-api-key": GATEWAY_TOKEN, "content-type": "application/json"})
    check("尾斜杠也能匹配", st == 200, f"{st} {body[:120]}")

    st, body = post(port, "/anthropic/unknown", msg,
                    {"x-api-key": GATEWAY_TOKEN, "content-type": "application/json"})
    try:
        shape = json.loads(body).get("type") == "error"
    except Exception:  # noqa: BLE001
        shape = False
    check("未知 anthropic 路径 404", st == 404, str(st))
    check("404 是 Anthropic 错误形状", shape, body[:120])

    print("\n[5] Anthropic 清洗生效")
    with trap.lock:
        trap.calls.clear()
    dirty = {"model": "deepseek-v4-flash", "max_tokens": 16,
             "messages": [{"role": "user", "content": "hi"},
                          {"role": "assistant", "content": None}]}
    st, body = post(port, "/anthropic/v1/messages", dirty,
                    {"x-api-key": GATEWAY_TOKEN, "content-type": "application/json"})
    with trap.lock:
        call = trap.calls[0] if trap.calls else None
    sent = json.loads(call["body"]) if call else {}
    check("assistant.content null 被补成 \"\"",
          sent.get("messages", [{}, {}])[1].get("content") == "", json.dumps(sent)[:160])
    check("请求成功", st == 200, str(st))

    print("\n[6] OpenAI /v1 通道不受影响")
    st, body = post(port, "/v1/chat/completions",
                    {"model": "deepseek-v4-flash",
                     "messages": [{"role": "user", "content": "hi"}]},
                    {"Authorization": f"Bearer {GATEWAY_TOKEN}",
                     "content-type": "application/json"})
    check("仍 200", st == 200, f"{st} {body[:120]}")

    server.shutdown()
    print()
    if FAILS:
        for f in FAILS:
            print("FAIL:", f)
        print(f"REGRESSION FAILED ({PASS} passed, {len(FAILS)} failed)")
        return 1
    print(f"ALL REGRESSION PASSED ({PASS} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
