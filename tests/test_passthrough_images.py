"""离线回归测试：生图模型与 /v1/* 通用原样透传（同步自 dsh-sensenova-pool v1.3.0）。"""
from __future__ import annotations

import http.client
import json
import pathlib
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from st_rotator.client import StRotator, is_image_path
from st_rotator.config import AccountConfig, Config, CooldownConfig, DEFAULT_IMAGE_TIMEOUT, ModelFilterConfig
from st_rotator.errors import ConfigError
from st_rotator.keypool import KeyPool
from st_rotator.proxy import create_server, resolve_upstream_passthrough_path
from st_rotator.transport import HttpClient, Request, Response

GATEWAY_TOKEN = "tk-image-passthrough"


def raw_http_request(
    port: int,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 15.0,
) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        hdrs = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, hdrs, data
    finally:
        conn.close()


class UnitPassthroughHelpersTest(unittest.TestCase):
    def test_is_image_path(self) -> None:
        self.assertTrue(is_image_path("images"))
        self.assertTrue(is_image_path("/images"))
        self.assertTrue(is_image_path("images/generations"))
        self.assertTrue(is_image_path("/images/edits?a=1"))
        self.assertFalse(is_image_path("images2/generations"))
        self.assertFalse(is_image_path("chat/completions"))

    def test_resolve_upstream_passthrough_path_security(self) -> None:
        base = "https://token.sensenova.cn/v1"
        self.assertEqual(
            resolve_upstream_passthrough_path(base, "/v1/images/generations"),
            ("images/generations", "/images/generations"),
        )
        self.assertEqual(
            resolve_upstream_passthrough_path(base, "/v1/images/edits/", "?detail=1"),
            ("images/edits?detail=1", "/images/edits"),
        )
        for bad in (
            "/unknown",
            "/v1",
            "/v1/",
            "/v1///",
            "/v1/../secret",
            "/v1/%2e%2e/secret",
            "/v1/%2E%2E/secret",
            "/v1/.",
            "/v1/%2e",
            "/v1//evil.example/x",
            "/v1/a//b",
            "/v1/foo%2fbar",
            "/v1/foo%5cbar",
            "/v1/foo%00bar",
            "/v1/foo%zz",
            "/v1/foo\\bar",
            "/v1/foo\x01bar",
        ):
            self.assertIsNone(resolve_upstream_passthrough_path(base, bad), f"应拒绝非法路径: {bad!r}")

    def test_image_timeout_config_and_effective_timeout(self) -> None:
        cfg = Config(
            base_url="https://token.sensenova.cn/v1",
            timeout=30.0,
            accounts=[AccountConfig(name="a1", api_keys=["sk-1"])],
        )
        rot = StRotator(cfg, pool=KeyPool(cfg.accounts, cfg.cooldown))
        self.assertEqual(rot.effective_timeout("chat/completions"), 30.0)
        self.assertEqual(rot.effective_timeout("images/generations"), DEFAULT_IMAGE_TIMEOUT)
        self.assertEqual(rot.effective_timeout("images/edits?foo=1"), DEFAULT_IMAGE_TIMEOUT)

        cfg_override = Config(
            base_url="https://token.sensenova.cn/v1",
            timeout=30.0,
            image_timeout=240.0,
            accounts=[AccountConfig(name="a1", api_keys=["sk-1"])],
        )
        rot_override = StRotator(cfg_override, pool=KeyPool(cfg_override.accounts, cfg_override.cooldown))
        self.assertEqual(rot_override.effective_timeout("images/generations"), 240.0)

        with self.assertRaises(ConfigError):
            Config(timeout=30.0, image_timeout=0)
        with self.assertRaises(ConfigError):
            Config(timeout=30.0, image_timeout=-5)


class PassthroughGatewayHandlerTest(unittest.TestCase):
    def _start_gateway(self, cfg: Config, handler) -> tuple[ThreadingHTTPServer, int]:
        rot = StRotator(
            cfg,
            pool=KeyPool(cfg.accounts, cfg.cooldown),
            client=HttpClient(cfg.base_url, handler=handler),
        )
        server = create_server(rot, host="127.0.0.1", port=0, token=GATEWAY_TOKEN)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server, port

    def test_images_json_multipart_rotation_and_security(self) -> None:
        seen: list[Request] = []
        gen_hits = 0
        multi_hits = 0

        def fake_upstream(req: Request) -> Response:
            nonlocal gen_hits, multi_hits
            seen.append(req)
            if req.method == "POST" and req.path == "images/generations":
                gen_hits += 1
                if gen_hits == 1:
                    return Response(
                        429,
                        {"content-type": "application/json"},
                        json.dumps({"error": {"message": "rate limited"}}).encode(),
                    )
                return Response(
                    200,
                    {"content-type": "application/json"},
                    json.dumps({"created": 1, "data": [{"b64_json": "AAAA"}]}).encode(),
                )
            if req.method == "POST" and req.path == "images/edits":
                ct = next((v for k, v in req.headers.items() if k.lower() == "content-type"), "")
                if "multipart/form-data" in ct:
                    multi_hits += 1
                    if multi_hits == 1:
                        return Response(502, {"content-type": "application/json"}, b'{"error":{"message":"bad gw"}}')
                return Response(
                    200,
                    {"content-type": "application/json"},
                    json.dumps({"created": 2, "data": [{"url": "https://example.invalid/out.png"}]}).encode(),
                )
            if req.method == "GET" and req.path == "images/tasks/t-1?detail=1":
                return Response(
                    200,
                    {"content-type": "application/json"},
                    json.dumps({"id": "t-1", "status": "succeeded"}).encode(),
                )
            if req.method == "GET" and req.path == "images/raw/1":
                return Response(200, {"content-type": "image/png"}, b"\x89PNG\r\n\x1a\nRAWBYTES")
            if req.method == "DELETE" and req.path == "files/f-1":
                return Response(204, {}, b"")
            if req.method == "HEAD" and req.path == "files/f-1":
                return Response(200, {"content-type": "application/octet-stream", "content-length": "1234"}, b"")
            if req.method in ("PUT", "PATCH") and req.path == "files/f-1":
                return Response(201, {"content-type": "application/json"}, req.body or b"{}")
            return Response(404, {"content-type": "application/json"}, b'{"error":{"message":"not found"}}')

        cfg = Config(
            base_url="https://token.sensenova.cn/v1",
            timeout=10.0,
            max_attempts=3,
            retry_backoff=0.01,
            accounts=[
                AccountConfig(name="a1", api_keys=["sk-img-1"], max_concurrency=4),
                AccountConfig(name="a2", api_keys=["sk-img-2"], max_concurrency=4),
            ],
            cooldown=CooldownConfig(base=5.0, factor=2.0, max=20.0, jitter=0.0),
        )
        _, port = self._start_gateway(cfg, fake_upstream)
        auth = {"Authorization": f"Bearer {GATEWAY_TOKEN}"}

        # 1) 文生图 JSON 透传 + 429 自动换 Key 重发 + 180s 默认生图超时
        gen_payload = json.dumps(
            {
                "model": "sensenova-u1.5-lite",
                "prompt": "a red panda in watercolor",
                "size": "1024x1024",
                "watermark": False,
                "prompt_extend": True,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        st, _, body = raw_http_request(
            port,
            "POST",
            "/v1/images/generations",
            headers={**auth, "Content-Type": "application/json"},
            body=gen_payload,
        )
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body)["data"][0]["b64_json"], "AAAA")
        self.assertEqual(len(seen), 2)
        self.assertNotEqual(seen[0].headers["Authorization"], seen[1].headers["Authorization"])
        self.assertNotIn(GATEWAY_TOKEN, seen[0].headers["Authorization"])
        self.assertEqual(seen[0].body, gen_payload)
        self.assertEqual(seen[1].body, gen_payload)
        self.assertEqual(seen[0].timeout, DEFAULT_IMAGE_TIMEOUT)

        # 2) 商汤图生图 JSON 原样字节透传（保留 images / image_url / watermark）
        edit_json = json.dumps(
            {
                "model": "sensenova-u1.5-lite",
                "prompt": "make sky sunset",
                "images": [{"image_url": "data:image/png;base64,iVBORw0KGgo="}],
                "watermark": False,
            },
            separators=(", ", " : "),
        ).encode("utf-8")
        st, _, _ = raw_http_request(
            port,
            "POST",
            "/v1/images/edits",
            headers={**auth, "Content-Type": "application/json"},
            body=edit_json,
        )
        self.assertEqual(st, 200)
        self.assertEqual(seen[-1].body, edit_json)

        # 3) multipart/form-data 二进制上传 + 502 自动换 Key 重发无损
        boundary = "----StRotatorTestBoundary98765"
        png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\xff\x00\xfe\x01"
        multipart_body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="model"\r\n\r\n'
            f"sensenova-u1.5-lite\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="image"; filename="ref.png"\r\n'
            f"Content-Type: image/png\r\n\r\n"
        ).encode("utf-8") + png_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")
        st, _, _ = raw_http_request(
            port,
            "POST",
            "/v1/images/edits",
            headers={**auth, "Content-Type": f"multipart/form-data; boundary={boundary}"},
            body=multipart_body,
        )
        self.assertEqual(st, 200)
        # 两次尝试都完整保留 Content-Type boundary 与原始二进制字节
        for req in seen[-2:]:
            ct = next((v for k, v in req.headers.items() if k.lower() == "content-type"), "")
            self.assertEqual(ct, f"multipart/form-data; boundary={boundary}")
            self.assertEqual(req.body, multipart_body)

        # 4) GET 查询参数、二进制响应、DELETE 204、HEAD、PUT/PATCH 201
        st, _, body = raw_http_request(port, "GET", "/v1/images/tasks/t-1?detail=1", headers=auth)
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body)["status"], "succeeded")

        st, hdrs, body = raw_http_request(port, "GET", "/v1/images/raw/1", headers=auth)
        self.assertEqual(st, 200)
        self.assertEqual(hdrs.get("content-type"), "image/png")
        self.assertEqual(body, b"\x89PNG\r\n\x1a\nRAWBYTES")

        st, _, body = raw_http_request(port, "DELETE", "/v1/files/f-1", headers=auth)
        self.assertEqual(st, 204)
        self.assertEqual(body, b"")

        st, hdrs, body = raw_http_request(port, "HEAD", "/v1/files/f-1", headers=auth)
        self.assertEqual(st, 200)
        self.assertEqual(hdrs.get("content-length"), "1234")
        self.assertEqual(body, b"")

        st, _, body = raw_http_request(
            port,
            "PATCH",
            "/v1/files/f-1",
            headers={**auth, "Content-Type": "application/json"},
            body=b'{"label":"v1"}',
        )
        self.assertEqual(st, 201)
        self.assertEqual(body, b'{"label":"v1"}')

        # 5) 安全边界：非法路径全部返回 404 且绝不打向上游
        before_bad = len(seen)
        for method, bad_path in (
            ("GET", "/unknown"),
            ("GET", "/v1/"),
            ("POST", "/v1/../secret"),
            ("POST", "/v1/%2e%2e/secret"),
            ("POST", "/v1//evil.example/x"),
            ("GET", "/v1/foo%2fbar"),
            ("GET", "/v1/foo%zz"),
        ):
            st, _, _ = raw_http_request(port, method, bad_path, headers=auth)
            self.assertEqual(st, 404, f"{method} {bad_path} 应返回 404，实际为 {st}")
        self.assertEqual(len(seen), before_bad)

    def test_all_keys_429_returns_retry_after(self) -> None:
        def always_429(_req: Request) -> Response:
            return Response(
                429,
                {"content-type": "application/json"},
                json.dumps({"error": {"message": "image rpm exhausted", "type": "rate_limit_error"}}).encode(),
            )

        cfg = Config(
            base_url="https://token.sensenova.cn/v1",
            max_attempts=2,
            acquire_fail_fast=0.05,
            accounts=[AccountConfig(name="a1", api_keys=["sk-only"], max_concurrency=4)],
            cooldown=CooldownConfig(base=5.0, factor=2.0, max=20.0, jitter=0.0),
        )
        _, port = self._start_gateway(cfg, always_429)
        st, hdrs, body = raw_http_request(
            port,
            "POST",
            "/v1/images/generations",
            headers={"Authorization": f"Bearer {GATEWAY_TOKEN}", "Content-Type": "application/json"},
            body=json.dumps({"model": "sensenova-u1.5-lite", "prompt": "cat"}).encode(),
        )
        self.assertEqual(st, 429)
        self.assertIn("retry-after", hdrs)
        self.assertIsInstance(json.loads(body).get("error", {}).get("message"), str)

    def test_model_filter_blocks_json_and_multipart(self) -> None:
        seen: list[Request] = []

        def ok_upstream(req: Request) -> Response:
            seen.append(req)
            return Response(200, {"content-type": "application/json"}, b'{"data":[]}')

        cfg = Config(
            base_url="https://token.sensenova.cn/v1",
            accounts=[AccountConfig(name="a1", api_keys=["sk-1"])],
            model_filter=ModelFilterConfig(available=["sensenova-u1.5-lite"]),
        )
        _, port = self._start_gateway(cfg, ok_upstream)
        auth = {"Authorization": f"Bearer {GATEWAY_TOKEN}"}

        # JSON 白名单拦截
        st, _, _ = raw_http_request(
            port,
            "POST",
            "/v1/images/generations",
            headers={**auth, "Content-Type": "application/json"},
            body=json.dumps({"model": "blocked-model", "prompt": "cat"}).encode(),
        )
        self.assertEqual(st, 400)
        self.assertEqual(len(seen), 0)

        # multipart 白名单拦截
        b = "----FilterBoundary"
        bad_mp = (
            f"--{b}\r\n"
            f'Content-Disposition: form-data; name="model"\r\n\r\n'
            f"blocked-model\r\n"
            f"--{b}--\r\n"
        ).encode()
        st, _, _ = raw_http_request(
            port,
            "POST",
            "/v1/images/edits",
            headers={**auth, "Content-Type": f"multipart/form-data; boundary={b}"},
            body=bad_mp,
        )
        self.assertEqual(st, 400)
        self.assertEqual(len(seen), 0)

        # 允许的模型正常放行
        ok_mp = bad_mp.replace(b"blocked-model", b"sensenova-u1.5-lite")
        st, _, _ = raw_http_request(
            port,
            "POST",
            "/v1/images/edits",
            headers={**auth, "Content-Type": f"multipart/form-data; boundary={b}"},
            body=ok_mp,
        )
        self.assertEqual(st, 200)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].body, ok_mp)


class RealSocketTimeoutAndSseTest(unittest.TestCase):
    def test_image_timeout_and_sse_passthrough_over_real_http(self) -> None:
        class SlowAndSseUpstream(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args: object) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                if self.path == "/v1/images/generations":
                    # 超过 chat 的 0.1s 短超时，但远低于生图默认 180s
                    time.sleep(0.22)
                    payload = json.dumps({"data": [{"url": "https://example.invalid/ok.png"}]}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if self.path == "/v1/embeddings":
                    time.sleep(0.22)
                    payload = b'{"data":[]}'
                    try:
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(payload)))
                        self.end_headers()
                        self.wfile.write(payload)
                    except OSError:
                        pass
                    return
                if self.path == "/v1/responses":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    for chunk in (b'data: {"delta":"he"}\n\n', b'data: {"delta":"llo"}\n\n', b"data: [DONE]\n\n"):
                        self.wfile.write(b"%X\r\n" % len(chunk) + chunk + b"\r\n")
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                    return
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), SlowAndSseUpstream)
        up_port = upstream.server_address[1]
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        self.addCleanup(upstream.server_close)
        self.addCleanup(upstream.shutdown)

        cfg = Config(
            base_url=f"http://127.0.0.1:{up_port}/v1",
            timeout=0.1,
            max_attempts=1,
            max_total_wait=0.5,
            accounts=[AccountConfig(name="a1", api_keys=["sk-real-1"], max_concurrency=4)],
            cooldown=CooldownConfig(base=0.1, factor=1.0, max=0.5, jitter=0.0, server_error=0.05),
        )
        rot = StRotator(cfg)
        self.addCleanup(rot.close)
        gateway = create_server(rot, host="127.0.0.1", port=0, token=GATEWAY_TOKEN)
        gw_port = gateway.server_address[1]
        threading.Thread(target=gateway.serve_forever, daemon=True).start()
        self.addCleanup(gateway.server_close)
        self.addCleanup(gateway.shutdown)

        auth = {"Authorization": f"Bearer {GATEWAY_TOKEN}", "Content-Type": "application/json"}

        # 1) 生图超过 chat 的 0.1s 超时仍正常 200
        st, _, body = raw_http_request(
            gw_port,
            "POST",
            "/v1/images/generations",
            headers=auth,
            body=json.dumps({"model": "sensenova-u1.5-lite", "prompt": "4k landscape"}).encode(),
        )
        self.assertEqual(st, 200)
        self.assertIn("ok.png", body.decode("utf-8"))

        # 2) SSE 透传完整且流结束后释放 Key 并发占位
        st, hdrs, body = raw_http_request(
            gw_port,
            "POST",
            "/v1/responses",
            headers=auth,
            body=json.dumps({"model": "deepseek-v4-flash", "stream": True, "input": "hi"}).encode(),
        )
        self.assertEqual(st, 200)
        self.assertIn("text/event-stream", hdrs.get("content-type", ""))
        self.assertIn(b'{"delta":"he"}', body)
        self.assertIn(b"data: [DONE]", body)
        # 验证 Key 并发占位已归零
        self.assertEqual(rot.pool.keys[0].inflight, 0)

        # 3) 非生图接口依然遵循 0.1s 短超时（被超时拦截）
        st, _, _ = raw_http_request(
            gw_port,
            "POST",
            "/v1/embeddings",
            headers=auth,
            body=json.dumps({"model": "text-embedding", "input": "hi"}).encode(),
        )
        self.assertEqual(st, 502)


if __name__ == "__main__":
    unittest.main()
