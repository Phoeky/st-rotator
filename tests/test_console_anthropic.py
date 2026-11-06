"""验证控制台「网关接入信息」已包含 ANTHROPIC 端点，且速贴页签有 Anthropic 分支。"""
import json
import sys
import threading
import urllib.request

sys.path.insert(0, ".")

from st_rotator.config import Config
from st_rotator.client import StRotator
from st_rotator.proxy import create_server
from st_rotator.ui import ConsoleState

TOKEN = "tk"
cfg = Config.from_file("config.json")
rot = StRotator(cfg)
console = ConsoleState(cfg, rot, host="127.0.0.1", port=8899,
                       token=TOKEN, subkeys=None, log_file="")
server = create_server(rot, host="127.0.0.1", port=0, token=TOKEN, console=console)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()

info = console.gateway_info()
print("=== gateway_info 关键字段 ===")
for k in ("base_url", "chat_endpoint", "anthropic_base_url",
          "anthropic_messages_endpoint", "anthropic_models_endpoint"):
    print(f"  {k:30} = {info.get(k)}")

req = urllib.request.Request(
    f"http://127.0.0.1:{port}/api/state",
    headers={"Authorization": f"Bearer {TOKEN}"})
with urllib.request.urlopen(req, timeout=60) as r:
    state = json.loads(r.read().decode())
g = state.get("gateway", {})
print("\n=== /api/state 的 gateway ===")
print("  anthropic_base_url:", g.get("anthropic_base_url"))
print("  anthropic_messages_endpoint:", g.get("anthropic_messages_endpoint"))

# 页面源码里确认渲染逻辑
page = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=30).read().decode()
checks = [
    ("接入信息含 Anthropic Base URL 行", "Anthropic Base URL" in page),
    ("接入信息含 Anthropic 端点行", "Anthropic 端点" in page),
    ("速贴页签含 Anthropic / Claude", "Anthropic / Claude" in page),
    ("ANTHROPIC_BASE_URL 出现在片段里", "ANTHROPIC_BASE_URL" in page),
    ("from anthropic import Anthropic", "from anthropic import" in page),
]
print("\n=== 页面断言 ===")
bad = 0
for name, ok in checks:
    print(("  PASS  " if ok else "  FAIL  ") + name)
    bad += 0 if ok else 1
print("\n结果:", "ALL PASS" if bad == 0 else f"{bad} FAILED")
server.shutdown()
sys.exit(1 if bad else 0)
