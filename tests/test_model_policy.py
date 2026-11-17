"""模型策略离线回归：只使用假上游和回环网关，不访问外部服务。"""
from __future__ import annotations

import copy
import json
import pathlib
import sys
import threading
import unittest
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from st_rotator.client import StRotator
from st_rotator.config import AccountConfig, Config, ModelFilterConfig
from st_rotator.model_policy import apply_chat_model_policy, patch_model_metadata
from st_rotator.proxy import create_server, sanitize_chat_payload
from st_rotator.transport import HttpClient, Request, Response


MESSAGES = [{"role": "user", "content": "你好"}]
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
# 预期独立于实现常量，避免实现词表写错时测试随之通过。
EXPECTED_EFFORTS = {
    "glm-5.3": ("low", "low", "low", "low", "high", "max", "max"),
    "glm-5.2": EFFORTS,
    "deepseek-v4-flash": EFFORTS,
    "sensenova-6.8-flash": EFFORTS,
    "sensenova-6.8-flash-lite": EFFORTS,
    "kimi-k3": ("none", "minimal", "low", "medium", "high", "xhigh", "xhigh"),
    "deepseek-flash": ("none", "minimal", "low", "medium", "high", "max", "max"),
}


class ModelPolicyTests(unittest.TestCase):
    def test_known_model_effort_matrix(self):
        for model, expected in EXPECTED_EFFORTS.items():
            for effort, target in zip(EFFORTS, expected):
                with self.subTest(model=model, effort=effort):
                    payload = {"model": model, "reasoning_effort": effort,
                               "messages": copy.deepcopy(MESSAGES), "max_tokens": 123}
                    original = copy.deepcopy(payload)
                    self.assertIs(apply_chat_model_policy(payload), payload)
                    self.assertEqual(payload, {**original, "reasoning_effort": target})

    def test_off_alias_and_case_normalization(self):
        for model in EXPECTED_EFFORTS:
            for effort, target in ((" OFF ", "low" if model == "glm-5.3" else "none"),
                                   (" HIGH ", "high")):
                with self.subTest(model=model, effort=effort):
                    payload = {"model": " " + model.upper() + " ", "reasoning_effort": effort}
                    apply_chat_model_policy(payload)
                    self.assertEqual(payload["reasoning_effort"], target)
                    self.assertEqual(payload["model"], " " + model.upper() + " ")

    def test_known_models_drop_invalid_efforts(self):
        for model in EXPECTED_EFFORTS:
            for effort in (None, False, 0, 1, [], {}, "", "automatic", "ultra"):
                with self.subTest(model=model, effort=effort):
                    payload = {"model": model, "reasoning_effort": effort, "max_tokens": 17}
                    apply_chat_model_policy(payload)
                    self.assertEqual(payload, {"model": model, "max_tokens": 17})

    def test_unknown_models_are_completely_untouched(self):
        for model in ("future-model", "kimi-k3-preview", "glm-5.30", "deepseek-flash-next",
                      "sensenova-6.8-flash-preview", "", None, 42):
            for effort in (" OFF ", "max", "automatic", None, {"custom": True}):
                with self.subTest(model=model, effort=effort):
                    payload = {"model": model, "reasoning_effort": effort, "temperature": 0.2}
                    original = copy.deepcopy(payload)
                    self.assertIs(apply_chat_model_policy(payload), payload)
                    self.assertEqual(payload, original)
        payload = {"reasoning_effort": "off", "temperature": 0.2}
        self.assertEqual(apply_chat_model_policy(payload),
                         {"reasoning_effort": "off", "temperature": 0.2})

    def test_missing_parameters_are_not_invented(self):
        for model in EXPECTED_EFFORTS:
            with self.subTest(model=model):
                payload = {"model": model}
                self.assertEqual(apply_chat_model_policy(payload), {"model": model})

    def test_kimi_temperature_only_changes_supplied_value(self):
        for value in (0, 0.3, 1, 2, None, "0.5"):
            with self.subTest(value=value):
                payload = {"model": "kimi-k3", "temperature": value, "top_p": 0.8}
                self.assertEqual(apply_chat_model_policy(payload),
                                 {"model": "kimi-k3", "temperature": 1, "top_p": 0.8})
        for model in EXPECTED_EFFORTS.keys() - {"kimi-k3"}:
            payload = {"model": model, "temperature": 0.2}
            self.assertEqual(apply_chat_model_policy(payload)["temperature"], 0.2)

    def test_flash_metadata_is_a_corrected_copy(self):
        for key in ("id", "name"):
            with self.subTest(key=key):
                item = {key: "sensenova-6.8-flash", "context_length": 262144,
                        "max_output_length": 32768, "extra": {"retained": True}}
                original = copy.deepcopy(item)
                patched = patch_model_metadata(item)
                self.assertIsNot(patched, item)
                self.assertEqual(item, original)
                self.assertEqual(patched, {**original, "context_length": 1048576})
        self.assertEqual(patch_model_metadata({"id": " SENSENOVA-6.8-FLASH "})["context_length"],
                         1048576)

    def test_lite_and_unknown_metadata_are_not_changed(self):
        for model in ("sensenova-6.8-flash-lite", "sensenova-6.8-flash-preview", "future-model"):
            with self.subTest(model=model):
                item = {"id": model, "context_length": 262144, "extension": "保留"}
                patched = patch_model_metadata(item)
                self.assertIsNot(patched, item)
                self.assertEqual(patched, item)


class RecordingUpstream:
    """记录最终发出的请求，同时提供模型目录、普通 JSON 和两种 SSE。"""

    def __init__(self):
        self.calls = []
        self.catalog = {"object": "list", "extension": "保留", "data": [
            {"id": "sensenova-6.8-flash", "context_length": 262144,
             "max_output_length": 32768, "extension": "保留"},
            {"id": "sensenova-6.8-flash-lite", "context_length": 262144},
            {"id": "future-model", "context_length": 12345},
        ]}

    def __call__(self, request: Request) -> Response:
        payload = json.loads(request.body) if request.body else None
        self.calls.append((request.path, payload))
        path = request.path.split("?", 1)[0].rstrip("/")
        if path.endswith("models"):
            result = self.catalog
        elif payload and payload.get("stream"):
            if path.endswith("messages"):
                data = 'event: message_stop\ndata: {"type":"message_stop"}\n\n'
            else:
                data = ('data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
                        'data: [DONE]\n\n')
            return Response(200, {"Content-Type": "text/event-stream"}, data.encode())
        elif path.endswith("messages"):
            result = {"id": "msg_test", "type": "message", "role": "assistant",
                      "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}
        else:
            result = {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        return Response(200, {"Content-Type": "application/json"}, json.dumps(result).encode())


def make_rotator(upstream, *, default_model="glm-5.3", available=()):
    config = Config(base_url="https://offline.invalid/v1", default_model=default_model,
                    accounts=[AccountConfig(name="offline", api_keys=["sk-offline"])],
                    max_attempts=1, model_filter=ModelFilterConfig(available=list(available)))
    return StRotator(config, client=HttpClient(config.base_url, handler=upstream))


class ClientPolicyTests(unittest.TestCase):
    def setUp(self):
        self.upstream = RecordingUpstream()
        self.rotator = make_rotator(self.upstream)
        self.addCleanup(self.rotator.close)

    def test_build_payload_uses_final_default_or_explicit_model(self):
        params = {"reasoning_effort": "medium", "temperature": 0.2, "max_tokens": 99}
        original = copy.deepcopy(params)
        for model, target, temperature in ((None, "low", 0.2),
                                           ("kimi-k3", "medium", 1),
                                           ("future-model", "medium", 0.2)):
            with self.subTest(model=model):
                payload = self.rotator._build_payload(MESSAGES, model, params)
                self.assertEqual(payload["model"], model or "glm-5.3")
                self.assertEqual(payload["reasoning_effort"], target)
                self.assertEqual(payload["temperature"], temperature)
                self.assertEqual(payload["max_tokens"], 99)
                self.assertEqual(params, original)
                self.assertEqual(MESSAGES, [{"role": "user", "content": "你好"}])

    def test_all_chat_entrypoints_send_policy_adjusted_payload(self):
        for method in ("chat", "chat_stream", "chat_stream_raw"):
            for model, target in ((None, "max"), ("kimi-k3", "xhigh")):
                with self.subTest(method=method, model=model):
                    result = getattr(self.rotator, method)(MESSAGES, model=model,
                                                          reasoning_effort="max", temperature=0.2)
                    if method != "chat":
                        self.assertTrue(list(result))
                    path, payload = self.upstream.calls[-1]
                    self.assertTrue(path.endswith("chat/completions"))
                    self.assertEqual(payload["reasoning_effort"], target)
                    self.assertEqual(payload["temperature"], 1 if model else 0.2)
                    self.assertEqual(bool(payload.get("stream")), method != "chat")

    def test_anthropic_entrypoints_do_not_apply_chat_policy(self):
        original = {"model": "kimi-k3", "messages": copy.deepcopy(MESSAGES),
                    "max_tokens": 32, "temperature": 0.2, "reasoning_effort": "max",
                    "thinking": {"type": "enabled", "budget_tokens": 16}}
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                payload = copy.deepcopy(original)
                if streaming:
                    list(self.rotator.anthropic_message_stream(payload))
                else:
                    self.rotator.anthropic_message(payload)
                self.assertEqual(payload, original)
                self.assertEqual(self.upstream.calls[-1][1],
                                 {**original, **({"stream": True} if streaming else {})})

    def test_non_chat_client_apis_do_not_apply_policy(self):
        self.rotator.embeddings("输入", model="kimi-k3", temperature=0.2, reasoning_effort="max")
        self.assertEqual(self.upstream.calls[-1][1],
                         {"model": "kimi-k3", "input": "输入", "temperature": 0.2,
                          "reasoning_effort": "max"})
        body = {"model": "glm-5.3", "reasoning_effort": "medium", "temperature": 0.2}
        self.rotator.request("responses", json_body=body)
        self.assertEqual(self.upstream.calls[-1][1], body)

    def test_console_catalog_patches_flash_without_mutating_upstream(self):
        original = copy.deepcopy(self.upstream.catalog)
        catalog = self.rotator.available_models(refresh=True)
        self.assertEqual(catalog["error"], "")
        contexts = {item["id"]: item["context_length"] for item in catalog["models"]}
        self.assertEqual(contexts, {"sensenova-6.8-flash": 1048576,
                                    "sensenova-6.8-flash-lite": 262144,
                                    "future-model": 12345})
        self.assertEqual(self.upstream.catalog, original)
        self.assertEqual(self.rotator.available_models()["models"], catalog["models"])


class GatewayPolicyTests(unittest.TestCase):
    def setUp(self):
        self.upstream = RecordingUpstream()
        self.rotator = make_rotator(self.upstream)
        self.addCleanup(self.rotator.close)
        self.server = create_server(self.rotator, host="127.0.0.1", port=0, token="offline-token")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.base_url = "http://127.0.0.1:" + str(self.server.server_address[1])

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base_url + path, data=data,
                                     headers={"Authorization": "Bearer offline-token",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as response:
            self.assertEqual(response.status, 200)
            return response.read().decode()

    def test_model_lists_match_console_and_preserve_extra_fields(self):
        original = copy.deepcopy(self.upstream.catalog)
        console = self.rotator.available_models(refresh=True)["models"]
        expected = {item["id"]: item["context_length"] for item in console}
        for path in ("/v1/models", "/anthropic/v1/models"):
            with self.subTest(path=path):
                catalog = json.loads(self.request(path))
                self.assertEqual({item["id"]: item["context_length"] for item in catalog["data"]}, expected)
                self.assertEqual(catalog["extension"], "保留")
                flash = next(item for item in catalog["data"] if item["id"] == "sensenova-6.8-flash")
                self.assertEqual(flash["extension"], "保留")
                self.assertEqual(flash["max_output_length"], 32768)
        self.assertEqual(self.upstream.catalog, original)

    def test_model_filter_still_applies_to_both_protocols(self):
        self.rotator.config.model_filter.available = ["sensenova-6.8-flash"]
        console = self.rotator.available_models(refresh=True)["models"]
        self.assertEqual([item["id"] for item in console], ["sensenova-6.8-flash"])
        for path in ("/v1/models", "/anthropic/v1/models"):
            catalog = json.loads(self.request(path))
            self.assertEqual([item["id"] for item in catalog["data"]], ["sensenova-6.8-flash"])
            self.assertEqual(catalog["data"][0]["context_length"], 1048576)

    def test_chat_gateway_default_explicit_and_streaming(self):
        for streaming in (False, True):
            for model, effort, temperature in ((None, "low", 0.2), ("kimi-k3", "medium", 1)):
                with self.subTest(streaming=streaming, model=model):
                    payload = {"messages": copy.deepcopy(MESSAGES), "reasoning_effort": "medium",
                               "temperature": 0.2, "stream": streaming}
                    if model:
                        payload["model"] = model
                    self.request("/v1/chat/completions", payload)
                    sent = self.upstream.calls[-1][1]
                    self.assertEqual(sent["model"], model or "glm-5.3")
                    self.assertEqual(sent["reasoning_effort"], effort)
                    self.assertEqual(sent["temperature"], temperature)

    def test_anthropic_gateway_does_not_apply_chat_policy(self):
        payload = {"model": "kimi-k3", "messages": copy.deepcopy(MESSAGES), "max_tokens": 32,
                   "temperature": 0.2, "thinking": {"type": "enabled", "budget_tokens": 16}}
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                body = {**payload, "stream": streaming}
                self.request("/anthropic/v1/messages", body)
                self.assertEqual(self.upstream.calls[-1][1], body)

    def test_other_gateway_endpoints_do_not_apply_chat_policy(self):
        for path in ("/v1/embeddings", "/v1/responses"):
            with self.subTest(path=path):
                payload = {"model": "kimi-k3", "input": "输入", "temperature": 0.2,
                           "reasoning_effort": "max"}
                self.request(path, payload)
                self.assertEqual(self.upstream.calls[-1][1], payload)

    def test_sanitizer_policy_can_be_disabled_for_other_apis(self):
        payload = {"model": "kimi-k3", "temperature": 0.2, "reasoning_effort": "max"}
        self.assertEqual(sanitize_chat_payload(dict(payload)),
                         {**payload, "temperature": 1, "reasoning_effort": "xhigh"})
        self.assertEqual(sanitize_chat_payload(dict(payload), model_policy=False), payload)


if __name__ == "__main__":
    unittest.main()
