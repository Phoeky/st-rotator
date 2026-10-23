"""轮换客户端：在 Key 池之上封装 OpenAI 兼容 HTTP 调用。

关键设计
--------
* **错误分类先于重试**。429 不一定是限流——商汤有实测案例把鉴权失败也返回成 429。
  所以 429 会先看响应体里有没有鉴权类关键词，有就按"失效 Key"处理，避免在坏 Key
  上无限轮换。
* **重试换 Key，退避不叠加**。每次重试都从池里取当前最优 Key（被限流的自然被跳过），
  同时在客户端侧叠加一层短退避，防止把池里的 Key 一起打爆。
* **流式不重复输出**。流式响应一旦开始吐字就视为成功，中断时直接抛错而不是重试。
"""

from __future__ import annotations

import gc
import json
import random
import re
import threading
import time
from dataclasses import replace
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Iterator, Mapping, Sequence

from .config import AccountConfig, Config, ModelFilterConfig, RateControlConfig, STRATEGIES, next_account_name
from .errors import (
    AllKeysInvalid,
    ApiError,
    ConfigError,
    NoAvailableKey,
    RotationExhausted,
    RotatorError,
    StreamInterrupted,
)
from .keypool import ApiKey, KeyPool, mask_key
from .limiter import AdaptiveRateLimiter, RateLimiter
from .transport import HttpClient, NetworkError, Response, StreamResponse

# 值得换个 Key 重试的状态码
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 522, 524})
# 凭据类错误
AUTH_STATUS = frozenset({401, 403})
# 429 响应体里出现这些词，说明其实是鉴权/账号问题，而非限流
AUTH_HINTS = (
    "invalid api key", "invalid_api_key", "invalid apikey", "api key is invalid",
    "unauthorized", "authentication failed", "authentication_error",
    "invalid token", "token expired", "no permission", "permission denied",
    "account disabled", "account suspended", "arrears", "欠费", "鉴权失败", "密钥无效", "无权限",
)
# 响应体里出现这些词，说明是"请求的模型不在当前 Key 的套餐/token plan 里"，
# 而不是凭据失效。这类错误**不能把 Key 标记失效**：
# - 换 Key 也没用（同池子的套餐通常一致，白打 N 次 401/403）
# - 更致命的是误判会毒化整个池子——日志里 6 把好 Key 全部 [失效]、网关瘫痪，
#   而且切回可用模型后 Key 仍是 INVALID，只能等 invalid_ttl 或重启（这就是
#   "切换回可用 model 不能恢复"的根因）。
MODEL_UNAVAILABLE_HINTS = (
    "model is not available in the current token plan",
    "model is not available",
    "model not available",
    "model not found",
    "model does not exist",
    "no such model",
    "unknown model",
    "model not supported",
    "model is not in",
    "model not in",
    "token plan",
    "模型不存在",
    "模型不可用",
    "模型未开通",
    "模型无权限",
)

_RETRY_AFTER_BODY = re.compile(r'"retry_?after"?\s*[:=]\s*"?(\d+(?:\.\d+)?)', re.I)
_RETRY_AFTER_CN = re.compile(r"(\d+(?:\.\d+)?)\s*(?:秒|s)\s*(?:后|之后)", re.I)

# ------------------------------------------------------------ 智能一换一
# 模块级状态（进程内共享）：触发节流 + 全局并发护栏
_FX_LOCK = threading.Lock()
_FX_LAST: dict[str, float] = {}
_FX_ACTIVE = 0

# 噪声图按尺寸进程级共享——烧点请求体动辄数 MB，反复生成会让 glibc 堆碎片化、
# RSS 只涨不降（实测一波烧点 37MB→200MB 不回落）；同尺寸图生成一次重复用即可。
_FX_IMG_CACHE: dict[int, str] = {}
# 带图请求体每发约 1MB×N 图，是唯一的内存大头：全局限 8 个在飞，防多账号同烧时 OOM
_FX_IMG_SEM = threading.Semaphore(8)


def _noise_png_b64(size: int) -> str:
    """生成 size×size 随机噪声 PNG 的 base64（纯标准库；按尺寸缓存，避免重复分配 MB 级缓冲）。

    噪声几乎不可压缩，能保证视觉模型按高分辨率图片计费——这正是烧点的目的。
    """
    cached = _FX_IMG_CACHE.get(size)
    if cached is not None:
        return cached
    import base64
    import os
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + os.urandom(size * 3) for _ in range(size))
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 1))
        + chunk(b"IEND", b"")
    )
    b64 = base64.b64encode(png).decode("ascii")
    with _FX_LOCK:
        if len(_FX_IMG_CACHE) < 8:  # 最多缓存 8 种尺寸，防无界
            _FX_IMG_CACHE[size] = b64
        return _FX_IMG_CACHE.get(size, b64)


# ------------------------------------------------------------ 内存看门人
# CPython 持有已释放的 arena 不还给 OS（glibc 堆碎片化后 RSS 只涨不降，
# 烧点这类 MB 级瞬时分配会把网关进程的 RSS 顶上去——实测一波烧点 37MB→200MB）。
# 定期 gc + malloc_trim 可以把空闲堆页真正还给内核；非 glibc 平台自动静默跳过。
_JANITOR_STARTED = False


# ------------------------------------------------------------ 服务优先（烧点让路）
# 小内存设备上，烧点与用户请求抢 CPU/内存/网络会直接把 TTFT 拖到几十秒。
# 网关每处理一个用户请求就 +1，烧点在发请求前先看这个计数：有人在等就先让路。
_SERVE_LOCK = threading.Lock()
_SERVE_INFLIGHT = 0


def note_serve_begin() -> None:
    """网关开始处理一个用户请求（烧点据此让路）。"""
    global _SERVE_INFLIGHT
    with _SERVE_LOCK:
        _SERVE_INFLIGHT += 1


def note_serve_end() -> None:
    """用户请求处理结束。"""
    global _SERVE_INFLIGHT
    with _SERVE_LOCK:
        _SERVE_INFLIGHT = max(0, _SERVE_INFLIGHT - 1)


def serve_inflight() -> int:
    with _SERVE_LOCK:
        return _SERVE_INFLIGHT


def available_mb() -> float | None:
    """Linux 下返回 /proc/meminfo 的 MemAvailable（MB）；其他平台返回 None。"""
    try:
        with open("/proc/meminfo", "r", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024.0
    except (OSError, ValueError, IndexError):
        return None
    return None


def _start_janitor() -> None:
    global _JANITOR_STARTED
    if _JANITOR_STARTED:
        return
    _JANITOR_STARTED = True
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
    except OSError:
        libc = None  # Windows / macOS：obmalloc  arenas 保留，但无 trim 可用

    def loop() -> None:
        while True:
            time.sleep(60)
            try:
                gc.collect()
                if libc is not None:
                    libc.malloc_trim(64 * 1024)
            except Exception:
                pass

    threading.Thread(target=loop, daemon=True, name="mem-janitor").start()


def safe_text(response: Response | StreamResponse) -> str:
    """安全读取响应体文本（流式响应会顺带读完并归还连接）。"""
    try:
        return response.text
    except Exception:  # pragma: no cover - 极端编码/连接问题
        return ""


def parse_retry_after(response: Response | StreamResponse, body: str = "") -> float | None:
    """从响应头 / 响应体里解析服务端建议的等待秒数。"""
    raw = (
        response.headers.get("retry-after")
        or response.headers.get("x-ratelimit-reset-requests")
        or response.headers.get("x-ratelimit-reset")
    )
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            try:
                delta = parsedate_to_datetime(raw).timestamp() - time.time()
                return max(0.0, delta)
            except Exception:
                pass
    for pattern in (_RETRY_AFTER_BODY, _RETRY_AFTER_CN):
        match = pattern.search(body or "")
        if match:
            try:
                return max(0.0, float(match.group(1)))
            except ValueError:
                pass
    return None


def classify(response: Response | StreamResponse, body: str) -> tuple[str, float | None]:
    """把响应归类为 ok / retry / invalid / fatal / model。

    Returns:
        (动作, 服务端建议等待秒数)
    """
    status = response.status
    if status < 400:
        return "ok", None
    lowered = (body or "").lower()
    if any(hint in lowered for hint in MODEL_UNAVAILABLE_HINTS):
        # 请求的模型不在当前 Key 的套餐里：Key 本身是好的。
        # 不能标记失效——否则一把好 Key 被误判 INVALID，切回可用模型也恢复不了。
        # 归为 "model"：换下一把 Key 试试（不同账号套餐可能不同），
        # 全部不行再透传上游错误（见 _post_with_rotation 的 model 分支）。
        return "model", None
    if status in AUTH_STATUS:
        return "invalid", None
    if status == 429:
        lowered = (body or "").lower()
        if any(hint in lowered for hint in AUTH_HINTS):
            # 商汤部分网关会用 429 表达鉴权失败，必须识别出来
            return "invalid", None
        return "retry", parse_retry_after(response, body)
    if status in RETRYABLE_STATUS:
        return "retry", parse_retry_after(response, body)
    return "fatal", None


def extract_error(body: str) -> str:
    """从响应体里抽出可读的错误信息。"""
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return (body or "")[:300]
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("msg") or err)[:300]
        if isinstance(err, str):
            return err[:300]
        for field in ("message", "msg", "error_msg", "detail"):
            if data.get(field):
                return str(data[field])[:300]
    return (body or "")[:300]


_extract_error = extract_error  # 兼容旧名字


def _parse_model_list(payload: Any) -> list[dict[str, Any]]:
    """把 ``/models`` 的返回规整成 UI 好用的结构。

    兼容两种形态：``{"data": [{"id": ...}]}``（OpenAI 风格，商汤走这个）和
    ``{"data": ["model-name", ...]}``（部分网关只给字符串数组）。
    """
    if not isinstance(payload, dict):
        return []
    raw = payload.get("data")
    if not isinstance(raw, list):
        return []
    models: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str):
            models.append({"id": item})
            continue
        if not isinstance(item, dict):
            continue
        model_id = item.get("id") or item.get("name")
        if not model_id:
            continue
        models.append({
            "id": str(model_id),
            "name": str(item.get("name") or model_id),
            "context_length": item.get("context_length"),
            "max_output_length": item.get("max_output_length"),
            "input_modalities": item.get("input_modalities") or [],
            "output_modalities": item.get("output_modalities") or [],
            "features": item.get("supported_features") or [],
            "description": str(item.get("description") or "")[:400],
        })
    models.sort(key=lambda m: m["id"])
    return models


class StRotator:
    """带多 Key 轮换与限流自愈的 OpenAI 兼容客户端。

    用法::

        rotator = StRotator(Config.from_file("config.json"))
        resp = rotator.chat([{"role": "user", "content": "你好"}])
        print(resp["choices"][0]["message"]["content"])
    """

    def __init__(
        self,
        config: Config,
        *,
        pool: KeyPool | None = None,
        client: HttpClient | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.pool = pool or KeyPool(
            config.accounts, config.cooldown, strategy=config.strategy, clock=clock
        )
        self.limiter = self._build_limiter(config, clock, sleeper)
        self._clock = clock
        self._sleep = sleeper
        self._rng = random.Random()
        self._log = logger or (lambda _msg: None)
        self._owns_client = client is None
        self._client = client or HttpClient(
            config.base_url,
            timeout=config.timeout,
            connect_timeout=config.connect_timeout,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "st-rotator/1.0",
                **config.extra_headers,
            },
            max_connections=config.max_connections,
            max_keepalive=config.max_keepalive,
        )
        # 模型清单缓存：UI 的模型选择器用，避免每刷一次页面就打一次上游
        self._models_lock = threading.Lock()
        self._models_cache: list[dict[str, Any]] = []
        self._models_fetched_at = 0.0
        self._models_error = ""
        # 真正打到上游的请求次数（含重试）。和"客户端请求数"不是一回事：
        # 一个客户端请求可能因为 429 变成好几次上游尝试，这个比值就是轮换的成本。
        self._attempts_lock = threading.Lock()
        self._upstream_attempts = 0
        _start_janitor()

    # ------------------------------------------------------------ 生命周期（内存）

    # ------------------------------------------------------------ 生命周期

    @staticmethod
    def _build_limiter(
        config: Config,
        clock: Callable[[], float],
        sleeper: Callable[[float], None],
    ) -> RateLimiter | AdaptiveRateLimiter:
        """按 rate_control.mode 选择限速器。"""
        rc = config.rate_control
        burst = max(1.0, float(len(config.accounts) if config.accounts else 1))
        if rc.mode == "adaptive":
            return AdaptiveRateLimiter(
                rc.qps,
                burst=burst,
                min_rate=rc.min_qps,
                max_rate=rc.max_qps,
                decrease=rc.decrease,
                increase_step=rc.increase_step,
                recovery_seconds=rc.recovery_seconds,
                clock=clock,
                sleeper=sleeper,
            )
        if rc.mode == "fixed":
            return RateLimiter(rc.qps, burst=burst, clock=clock, sleeper=sleeper)
        return RateLimiter(0.0, burst=burst, clock=clock, sleeper=sleeper)  # off

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "StRotator":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------ 对外接口

    def chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model: str | None = None,
        attempts: int | None = None,
        headers: Mapping[str, str] | None = None,
        **params: Any,
    ) -> dict[str, Any]:
        """非流式对话补全，自动轮换 Key 直到成功。"""
        payload = self._build_payload(messages, model, params)
        return self._post_with_rotation("chat/completions", payload, attempts=attempts, headers=headers)

    def chat_stream(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model: str | None = None,
        attempts: int | None = None,
        headers: Mapping[str, str] | None = None,
        **params: Any,
    ) -> Iterator[str]:
        """流式对话补全，逐段 yield 文本增量（适合直接打印给人看）。"""
        payload = self._build_payload(messages, model, params)
        payload["stream"] = True
        yield from self._stream_with_rotation("chat/completions", payload, attempts=attempts, headers=headers)

    def chat_stream_raw(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model: str | None = None,
        attempts: int | None = None,
        headers: Mapping[str, str] | None = None,
        **params: Any,
    ) -> Iterator[dict[str, Any]]:
        """流式对话补全，逐块透传**原始 chunk**。

        与 ``chat_stream`` 的区别：这里不做文本抽取，``tool_calls`` / ``finish_reason`` /
        ``usage`` 等字段原样保留。**做 OpenAI 兼容网关必须用这个**，否则 Agent 的工具
        调用会被吃掉。
        """
        payload = self._build_payload(messages, model, params)
        payload["stream"] = True
        yield from self._stream_with_rotation("chat/completions", payload, attempts=attempts, raw=True, headers=headers)

    def embeddings(
        self, inputs: Any, *, model: str, attempts: int | None = None, **params: Any
    ) -> dict[str, Any]:
        payload = {"model": model, "input": inputs, **params}
        return self._post_with_rotation("embeddings", payload, attempts=attempts)

    def request(
        self,
        path: str,
        *,
        method: str = "POST",
        json_body: Mapping[str, Any] | None = None,
        attempts: int | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """通用轮换请求，方便接未封装的端点。"""
        return self._post_with_rotation(
            path, dict(json_body) if json_body is not None else None, attempts=attempts, method=method, headers=headers
        )

    def models(self) -> dict[str, Any]:
        return self._post_with_rotation("models", None, method="GET")

    def verify_key(self, key: ApiKey) -> tuple[bool, str]:
        """单独校验一把 Key（用于 ``check`` 命令），不走池调度。

        只回答"能不能用"；分不清"Key 坏了"和"暂时被限流"的场景请用 ``probe_key``。
        """
        verdict, detail = self.probe_key(key.key)
        return verdict != "invalid", detail

    def probe_key(self, key: str) -> tuple[str, str]:
        """探测一把裸 Key，返回 ``(verdict, detail)``。

        ``verdict`` 有三种，这个区分很关键：

        * ``"ok"``      —— 凭据有效。
        * ``"invalid"`` —— 401/403，凭据确实坏了，应该拒收。
        * ``"unknown"`` —— 429 / 5xx / 网络错误。**不能据此判定 Key 有问题**：
          限流恰恰说明凭据是有效的，只是当下没额度。批量导入 Key 时如果把 429 当成
          失效，会把好 Key 一起拒掉。
        """
        headers = {"Authorization": f"Bearer {key}"}
        try:
            response = self._client.get("models", headers=headers)
            status = response.status
            if status < 400:
                return "ok", "ok"
            detail = f"{status} {extract_error(safe_text(response))}"
            if status in AUTH_STATUS:
                lowered = (safe_text(response) or "").lower()
                if any(hint in lowered for hint in MODEL_UNAVAILABLE_HINTS):
                    # 探测用的是默认模型；模型不在套餐不等于 Key 失效
                    return "unknown", f"默认模型不可用（{detail}）"
                return "invalid", detail
            if status in RETRYABLE_STATUS:
                return "unknown", detail
            if status in (404, 405):
                # 网关不支持 /models，退回最小对话探测
                probe = self._client.post(
                    "chat/completions",
                    headers=headers,
                    json_body={
                        "model": self.config.default_model,
                        "messages": [{"role": "user", "content": "ping"}],
                        "max_tokens": 1,
                        "stream": False,
                    },
                )
                if probe.status < 400:
                    return "ok", "ok"
                probe_detail = f"{probe.status} {extract_error(safe_text(probe))}"
                if probe.status in AUTH_STATUS:
                    lowered = (safe_text(probe) or "").lower()
                    if any(hint in lowered for hint in MODEL_UNAVAILABLE_HINTS):
                        return "unknown", f"默认模型不可用（{probe_detail}）"
                    return "invalid", probe_detail
                if probe.status in RETRYABLE_STATUS:
                    return "unknown", probe_detail
                return "invalid", probe_detail
            return "invalid", detail
        except NetworkError as exc:
            return "unknown", f"网络错误: {exc}"

    def status(self) -> dict[str, Any]:
        """运行状态快照，便于接入监控。"""
        return {
            "summary": self.pool.summary(),
            "keys": self.pool.snapshot(),
            "rate_control": self.limiter.stats(),
            "upstream_attempts": self.upstream_attempts,
        }

    @property
    def upstream_attempts(self) -> int:
        """累计打到上游的请求次数（含重试）。"""
        with self._attempts_lock:
            return self._upstream_attempts

    def _note_upstream_attempt(self) -> None:
        with self._attempts_lock:
            self._upstream_attempts += 1

    # ------------------------------------------------------------ 运行时控制
    #
    # 下面这些方法给控制台（UI）用：不改进程、不重启网关就能调整运行参数。
    # 注意它们只改**内存中的 Config**，落盘由 UI 层的 ConfigStore 负责。

    def add_key(
        self,
        key: str,
        *,
        account: str | None = None,
        rpm_limit: int | None = None,
        max_concurrency: int = 4,
        weight: float = 1.0,
    ) -> ApiKey:
        """运行中加一把 Key，立刻参与轮换。"""
        acct_name = account or next_account_name(a.name for a in self.config.accounts)
        item = self.pool.add_key(
            key,
            acct_name,
            rpm_limit=rpm_limit,
            max_concurrency=max_concurrency,
            weight=weight,
        )
        # 同步更新 self.config.accounts，避免配置状态脱节
        found = False
        for acct_cfg in self.config.accounts:
            if acct_cfg.name == acct_name:
                if key not in acct_cfg.api_keys:
                    acct_cfg.api_keys.append(key)
                found = True
                break
        if not found:
            self.config.accounts.append(
                AccountConfig(
                    name=acct_name,
                    api_keys=[key],
                    rpm_limit=rpm_limit,
                    max_concurrency=max_concurrency,
                    weight=weight,
                )
            )
        return item

    def remove_key(self, key: str) -> bool:
        """运行中移除一把 Key；返回是否真的移除了。"""
        removed = self.pool.remove_key(key)
        if removed is not None:
            # 同步更新 self.config.accounts
            for acct_cfg in list(self.config.accounts):
                if removed.key in acct_cfg.api_keys:
                    acct_cfg.api_keys.remove(removed.key)
                if not acct_cfg.api_keys:
                    self.config.accounts.remove(acct_cfg)
            return True
        return False

    def set_default_model(self, model: str) -> str:
        """切换默认模型。下一次请求即生效（``_build_payload`` 每次现读配置）。

        切换模型时顺带把被误判失效的 Key 全部复活：旧版会把"模型不在套餐"
        错当成凭据失效，导致切回可用模型后 Key 依然是 INVALID、池子瘫痪。
        现在切换模型就是一次"重新验证"的机会，让好 Key 立刻恢复参与轮换。
        """
        model = (model or "").strip()
        if not model:
            raise ConfigError("模型名不能为空")
        # 只复活被"模型不在套餐"误判失效的 Key；真失效（401/403 凭据问题）的不碰，
        # 避免复活后又白打一次请求。
        revived = self.pool.revive_invalid(MODEL_UNAVAILABLE_HINTS)
        self.config.default_model = model
        if revived:
            self._log(f"[配置] 默认模型切换为 {model}，已复活 {revived} 把被误判失效的 Key")
        else:
            self._log(f"[配置] 默认模型切换为 {model}")
        return model

    def set_strategy(self, strategy: str) -> str:
        """切换调度策略（round_robin / least_inflight / least_recent / weighted）。"""
        if strategy not in STRATEGIES:
            raise ConfigError(f"strategy 必须是 {STRATEGIES} 之一，当前为 {strategy!r}")
        self.config.strategy = strategy
        self.pool.strategy = strategy
        self._log(f"[配置] 调度策略切换为 {strategy}")
        return strategy

    def set_rate_control(self, **changes: Any) -> dict[str, Any]:
        """调整主动限速参数，并按新模式重建限速器。

        可改字段：``mode`` / ``qps`` / ``min_qps`` / ``max_qps`` / ``decrease`` /
        ``increase_step`` / ``recovery_seconds``。只传要改的字段。

        重建会**丢掉 AIMD 已经收敛到的速率**（回到 ``qps`` 起点）。这是刻意的：
        用户改了参数，就应该从新起点重新探测。
        """
        unknown = set(changes) - set(RateControlConfig.__dataclass_fields__)
        if unknown:
            raise ConfigError(f"未知的限速参数: {sorted(unknown)}")
        cleaned = {k: v for k, v in changes.items() if v is not None}
        if not cleaned:
            return self.limiter.stats()
        # 先构造新对象做校验，校验通过再赋值——避免把配置改坏后无法回滚
        updated = replace(self.config.rate_control, **cleaned)
        self.config.rate_control = updated
        self.limiter = self._build_limiter(self.config, self._clock, self._sleep)
        self._log(
            f"[配置] 限速重建: mode={updated.mode} qps={updated.qps} "
            f"区间={updated.min_qps}~{updated.max_qps}"
        )
        return self.limiter.stats()

    def model_filter(self) -> Any:
        """当前的模型清单过滤器（``config.model_filter``；老配置对象可能没有该字段）。"""
        return getattr(self.config, "model_filter", None)

    def hidden_model(self, model_id: str) -> bool:
        """该模型是否被配置为"从清单里藏掉"。"""
        flt = self.model_filter()
        return bool(flt is not None and flt.is_hidden(model_id))

    def set_model_filter(self, hidden: Sequence[str]) -> None:
        """更新模型屏蔽清单（控制台「屏蔽模型」配置）。

        只影响 ``/v1/models`` 与控制台模型选择器的清单展示，不拦截显式请求；
        老配置对象（无 ``model_filter`` 字段）直接补一个过滤对象挂上去。
        """
        cleaned: list[str] = []
        for name in hidden:
            name = (name or "").strip()
            if name and name not in cleaned:
                cleaned.append(name)
        flt = self.model_filter()
        if flt is None:
            self.config.model_filter = ModelFilterConfig(hidden=cleaned)
        else:
            flt.hidden = cleaned

    def filter_models_payload(self, payload: Any) -> Any:
        """过滤 OpenAI 风格 ``{"data": [...]}`` 模型清单，其余字段原样保留。

        用于网关把 ``/v1/models`` 透传给上层应用时保持与控制台一致——
        被隐藏的模型不该出现在任何一份清单里。
        """
        if not isinstance(payload, dict):
            return payload
        data = payload.get("data")
        if not isinstance(data, list):
            return payload
        kept: list[Any] = []
        for item in data:
            if isinstance(item, dict):
                model_id = item.get("id") or item.get("name")
            elif isinstance(item, str):
                model_id = item
            else:
                kept.append(item)
                continue
            if self.hidden_model(str(model_id or "")):
                continue
            kept.append(item)
        if len(kept) == len(data):
            return payload
        filtered = dict(payload)
        filtered["data"] = kept
        return filtered

    def available_models(self, *, refresh: bool = False, ttl: float = 300.0) -> dict[str, Any]:
        """取上游模型清单（带缓存 + 按配置过滤），供 UI 的模型选择器使用。

        过滤在**返回时**做（而不是抓取时），所以改配置后不必等缓存过期即可生效。

        这里**故意吞掉所有异常**：模型清单是锦上添花的东西，而上游连不上/限流时，
        恰恰是用户最需要打开控制台排查的时刻。如果让网络错误冒出去，整个状态接口会
        502，界面直接白屏——那就本末倒置了。失败信息记在返回值的 ``error`` 里，
        由界面负责显示。

        Returns:
            ``{"models": [...], "fetched_at": 时间戳, "cached": bool,
            "error": str, "hidden": [被隐藏的模型名...]}``
        """
        with self._models_lock:
            # fresh 以「至少拉过一次」的时间戳判断，而不是 cache 真值：失败时 cache 恒为空
            # （falsy），用真值会让每次轮询都重拉重记，零 Key/上游不可达时警告刷屏。
            fresh = (
                self._models_fetched_at > 0.0
                and (time.monotonic() - self._models_fetched_at) < ttl
            )
            if fresh and not refresh:
                return self._models_result(cached=True)
            try:
                payload = self.models()
                self._models_cache = _parse_model_list(payload)
                self._models_error = ""
            except Exception as exc:  # noqa: BLE001 - 清单拉取失败绝不能影响主流程
                # 故意宽catch。上游不可达时 _post_with_rotation 会把 NetworkError 包装成
                # RotationExhausted 抛出，但真实环境里还可能冒出别的类型（JSON 解析异常、
                # 上游返回结构变化导致的 KeyError…）。模型清单只是锦上添花，
                # 绝不能让它把状态接口带崩——上游出故障时用户正需要打开控制台排查。
                self._models_error = f"{type(exc).__name__}: {exc}"
                self._log(f"[警告] 拉取模型清单失败: {self._models_error}")
            self._models_fetched_at = time.monotonic()
            return self._models_result(cached=False)

    def _models_result(self, *, cached: bool) -> dict[str, Any]:
        """把缓存里的模型清单按配置过滤后打包返回（调用方需持 ``_models_lock``）。"""
        kept: list[dict[str, Any]] = []
        hidden: list[str] = []
        for model in self._models_cache:
            if self.hidden_model(model.get("id", "")):
                hidden.append(str(model.get("id", "")))
            else:
                kept.append(model)
        return {
            "models": kept,
            "fetched_at": self._models_fetched_at,
            "cached": cached,
            "error": self._models_error,
            "hidden": hidden,
        }

    # ------------------------------------------------------------ 内部：载荷

    def _build_payload(
        self,
        messages: Sequence[Mapping[str, Any]],
        model: str | None,
        params: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not messages:
            raise ValueError("messages 不能为空")
        payload: dict[str, Any] = {
            "model": model or self.config.default_model,
            "messages": list(messages),
        }
        payload.update(params)
        return payload

    def _backoff(self, attempt: int) -> float:
        delay = min(self.config.retry_backoff * (2 ** (attempt - 1)), self.config.max_retry_backoff)
        return delay + self._rng.uniform(0.0, delay * 0.3)

    @staticmethod
    def _auth(key: ApiKey) -> dict[str, str]:
        """把当前租约对应的 Key 注入请求头。"""
        return {"Authorization": f"Bearer {key.key}"}

    # ------------------------------------------------------------ 智能一换一

    def _maybe_flash_lite_exchange(self, key: ApiKey, detail: str = "") -> None:
        """账号被判"套餐耗尽冷却"后，自动烧该账号的推广池积分换通用池额度。

        只在错误明确是"额度耗尽"（entitlement/quota/耗尽）时触发；"模型不在套餐"
        （账号从未开过该模型）烧了也白烧，不触发。
        同账号有最小触发间隔 + 全局并发护栏，防止多请求并发把触发打成螺旋。
        """
        fx = getattr(self.config, "flash_lite_exchange", None)
        if fx is None or not fx.enabled:
            return
        text = (detail or "").lower()
        if text and not any(k in text for k in ("entitlement", "quota", "exhaust", "额度", "耗尽")):
            return
        # 内存护栏：小内存设备上宁可晚点换额度，也不能把整机拖死
        avail = available_mb()
        if avail is not None and 0 < fx.min_available_mb and avail < fx.min_available_mb:
            self._log(f"[一换一] {key.account} 可用内存仅 {avail:.0f}MB < {fx.min_available_mb:.0f}MB，跳过烧点")
            return
        global _FX_ACTIVE
        now = time.time()
        with _FX_LOCK:
            last = _FX_LAST.get(key.account, 0.0)
            if now - last < fx.min_interval_s:
                return
            if _FX_ACTIVE >= max(1, fx.max_workers):
                self._log(f"[一换一] {key.account} 触发过频（全局 {_FX_ACTIVE} 个任务在烧），跳过")
                return
            _FX_LAST[key.account] = now
        threading.Thread(
            target=self._flash_lite_burn, args=(key, fx), daemon=True, name=f"fx-{key.account}"
        ).start()

    def _flash_lite_burn(self, key: ApiKey, fx: Any) -> None:
        """烧点任务本体（后台线程，不阻塞正常流量）。

        循环烧到三种结局之一：
        1. 探测请求确认通用池已回补 → 立即复活账号、收工（目标达成）
        2. 套餐冷却窗口（cooldown.model_unavailable）到期 → 收工，交给正常复探
        3. 推广池积分也耗尽（429 entitlement）→ 收工，不硬烧
        """
        global _FX_ACTIVE
        with _FX_LOCK:
            _FX_ACTIVE += 1
        try:
            cooldown_s = max(60.0, float(self.config.cooldown.model_unavailable))
            deadline = time.time() + cooldown_s
            self._log(
                f"[一换一] {key.masked}({key.account}) 套餐耗尽，开始持续烧 {fx.model} 换通用池"
                f"（窗口 {cooldown_s:.0f}s / 上限 {fx.requests_per_trigger} 发 / 并发 {fx.concurrency}）…"
            )
            stop = threading.Event()  # 硬停止信号（推广池耗尽 / 网络异常）
            done = 0
            lock = threading.Lock()
            images_b64: list[str] | None = None  # 本任务的图组（首次用到时一次性生成）

            def ensure_images() -> list[str]:
                nonlocal images_b64
                with lock:
                    if images_b64 is None:
                        n = max(1, fx.multi_image_count)
                        images_b64 = [_noise_png_b64(fx.image_size) for _ in range(n)]
                    return images_b64

            def one(i: int) -> str:
                """单发烧点。返回 'ok' / 'soft429' / 'stop' / 'err'。"""
                nonlocal done
                if stop.is_set():
                    return "stop"
                if fx.image_enabled and i % 2 == 1:
                    imgs = ensure_images()
                    content: Any = [{"type": "text", "text": "请逐一详细描述这些图片的内容，不少于300字。"}]
                    content += [
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}}
                        for b64 in imgs
                    ]
                    max_tok = min(fx.long_text_max_tokens, 1024)  # 图请求输出小，省时间
                else:
                    content = fx.long_text_prompt
                    max_tok = fx.long_text_max_tokens
                payload = {
                    "model": fx.model,
                    "messages": [{"role": "user", "content": content}],
                    "max_tokens": max_tok,
                    "stream": False,
                }
                is_img = isinstance(content, list)
                # 服务优先：有用户请求在途就先让路（最多等 8s，避免烧点被彻底饿死）
                if fx.yield_to_serve:
                    waited = 0.0
                    while serve_inflight() > 0 and not stop.is_set() and waited < 8.0:
                        time.sleep(0.3)
                        waited += 0.3
                if is_img:
                    _FX_IMG_SEM.acquire()  # 大图请求进限流闸，防瞬时内存峰值
                try:
                    resp = self._client.request("POST", "/chat/completions", json_body=payload, headers=self._auth(key))
                    if resp.status == 200:
                        try:
                            usage = (resp.json() or {}).get("usage") or {}
                        except Exception:
                            usage = {}
                        with lock:
                            done += 1
                        n_img = len(content) - 1 if isinstance(content, list) else 0
                        self._log(
                            f"[一换一] {key.account} 第 {i + 1} 发烧点完成"
                            f"（{'%d图' % n_img if n_img else '长文'}，总耗≈{usage.get('total_tokens', '?')} tokens，累计 {done}）"
                        )
                        return "ok"
                    if resp.status == 429:
                        low = safe_text(resp).lower()
                        if any(k in low for k in ("entitlement", "quota", "exhaust", "额度", "耗尽")):
                            self._log(f"[一换一] {key.account} 推广池额度也耗尽（429 entitlement），本轮停止")
                            stop.set()
                            return "stop"
                        return "soft429"  # 分钟级 TPM 窗口：额度还在，放慢即可
                    self._log(f"[一换一] {key.account} 烧点失败 HTTP {resp.status}: {safe_text(resp)[:120]}")
                    return "err"
                except Exception as exc:  # 后台任务，异常不外抛
                    self._log(f"[一换一] {key.account} 烧点异常: {type(exc).__name__}: {exc}")
                    stop.set()
                    return "stop"
                finally:
                    if is_img:
                        _FX_IMG_SEM.release()

            def probe_recovered() -> bool:
                """用默认模型发一个极小请求，探测通用池是否已回补。"""
                payload = {
                    "model": self.config.default_model,
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 8,
                    "stream": False,
                }
                try:
                    resp = self._client.request("POST", "/chat/completions", json_body=payload, headers=self._auth(key))
                    return resp.status == 200
                except Exception:
                    return False

            round_i = 0
            recovered = False
            last_mem_warn = 0.0
            while not stop.is_set() and time.time() < deadline and done < fx.requests_per_trigger:
                # 内存护栏：内存吃紧就先歇着（小内存设备保命优先，别把整机拖死）
                avail = available_mb()
                if avail is not None and 0 < fx.min_available_mb and avail < fx.min_available_mb:
                    if time.time() - last_mem_warn > 60:  # 每分钟最多提示一次，别刷日志
                        self._log(f"[一换一] {key.account} 可用内存 {avail:.0f}MB 偏低，暂停烧点等待内存回收")
                        last_mem_warn = time.time()
                    time.sleep(10)
                    continue
                # 服务优先：有用户请求在途就让路，避免抢 CPU/网络把首字延迟拖长
                if fx.yield_to_serve and serve_inflight() > 0:
                    time.sleep(1.0)
                    continue
                # 每轮并发打一小批
                batch = min(max(1, fx.concurrency), fx.requests_per_trigger - done)
                threads = []
                for _ in range(batch):
                    if stop.is_set() or time.time() >= deadline:
                        break
                    t = threading.Thread(target=one, args=(round_i,), daemon=True)
                    threads.append(t)
                    t.start()
                    round_i += 1
                for t in threads:
                    t.join()

                # 每轮结束探测一次：通用池回补了就提前收工 + 立即复活账号
                if probe_recovered():
                    self._log(f"[一换一] {key.account} 通用池已回补，提前结束烧点 ✅")
                    try:
                        self.pool.report_success(key, latency=0.0)  # 顺带清掉冷却，立刻可用
                    except Exception:
                        pass
                    recovered = True
                    break
                # 暴力烧点：正常时立刻下一轮；只有撞推广池窗口限流（soft429）才在 one() 里自行降速

            if not recovered and time.time() >= deadline:
                self._log(f"[一换一] {key.account} 冷却窗口到期，结束烧点（成功 {done} 发，等待正常复探）")
            elif not recovered:
                self._log(f"[一换一] {key.account} 结束烧点，成功 {done} 发")
        finally:
            images_b64 = None  # 确定性释放 MB 级图组，别等 GC
            gc.collect()
            with _FX_LOCK:
                _FX_ACTIVE -= 1


    def _remaining(self, started_at: float, budget: float | None) -> float | None:
        """单请求剩余等待预算；None 表示不设上限。"""
        if budget is None:
            return None
        return budget - (self._clock() - started_at)

    def _acquire_timeout(self, remaining: float | None) -> float:
        """等 Key 的超时不能超过剩余预算，否则会白白挂死。"""
        if remaining is None:
            return self.config.acquire_timeout
        return max(0.001, min(self.config.acquire_timeout, remaining))

    def _sleep_between(self, attempt: int, started_at: float, budget: float | None) -> None:
        delay = self._backoff(attempt)
        remaining = self._remaining(started_at, budget)
        if remaining is not None:
            if remaining <= 0:
                return
            delay = min(delay, remaining)
        self._sleep(delay)

    def _warn_if_reasoning_ate_budget(self, result: Any) -> None:
        """推理模型的坑：max_tokens 太小会被 reasoning_content 吃光，content 返回空串。

        ``deepseek-v4-flash`` 这类推理模型先输出 ``reasoning_content`` 再输出 ``content``，
        两者共用 ``max_tokens`` 预算。如果预算不够，就会拿到空回复 + ``finish_reason=length``。
        这不算错误，但排查起来很费时间，所以主动提示一句。
        """
        if not isinstance(result, dict):
            return
        choices = result.get("choices") or []
        if not choices:
            return
        first = choices[0] or {}
        message = first.get("message") or {}
        if message.get("content"):
            return
        if first.get("finish_reason") != "length":
            return
        details = (result.get("usage") or {}).get("completion_tokens_details") or {}
        reasoning = details.get("reasoning_tokens")
        if reasoning:
            self._log(
                f"[警告] content 为空：{reasoning} 个 token 全部被 reasoning_content 消耗，"
                f"请调大 max_tokens（推理模型需要额外预算）"
            )

    # ------------------------------------------------------------ 内部：非流式

    def _post_with_rotation(
        self,
        path: str,
        payload: dict[str, Any] | None,
        *,
        attempts: int | None = None,
        method: str = "POST",
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        max_attempts = attempts or self.config.max_attempts
        excluded: set[str] = set()
        last_status: int | None = None
        last_body = ""
        last_exc: Exception | None = None
        started_at = self._clock()
        budget = self.config.max_total_wait or None

        for attempt in range(1, max_attempts + 1):
            remaining = self._remaining(started_at, budget)
            if remaining is not None and remaining <= 0:
                last_exc = last_exc or TimeoutError(f"超出单请求等待预算 {budget:g}s")
                self._log(f"[放弃] 超出等待预算 {budget:g}s")
                break

            # 先过限速闸门（不持 Key 状态，避免排队休眠时霸占并发配额与锁死）
            try:
                self.limiter.acquire(timeout=remaining)
            except TimeoutError as exc:
                last_exc = exc
                self._log(f"[放弃] 限速等待超时: {exc}")
                break

            remaining = self._remaining(started_at, budget)
            try:
                key = self.pool.acquire(
                    exclude=excluded,
                    timeout=self._acquire_timeout(remaining),
                    fail_fast=self.config.acquire_fail_fast,
                )
            except AllKeysInvalid:
                self.limiter.refund()
                raise
            except NoAvailableKey as exc:
                self.limiter.refund()
                last_exc = exc
                self._log(f"[放弃] 等待可用 Key 超时: {exc}")
                break

            retryable = False
            try:
                started = self._clock()
                self._note_upstream_attempt()
                req_headers = dict(headers) if headers else {}
                req_headers.update(self._auth(key))
                response = self._client.request(
                    method, path, json_body=payload, headers=req_headers
                )
                body = safe_text(response) if response.status >= 400 else ""
                action, retry_after = classify(response, body)

                if action == "ok":
                    self.pool.report_success(key, latency=self._clock() - started)
                    self.limiter.on_success()
                    result = response.json()
                    self._warn_if_reasoning_ate_budget(result)
                    return result

                last_status, last_body = response.status, body
                if action == "invalid":
                    self.pool.report_invalid(key, extract_error(body))
                    excluded.add(key.key)
                    self._log(f"[失效] {key.masked}({key.account}) 凭据无效，已排除: {extract_error(body)}")
                    retryable = True
                elif action == "model":
                    # 模型不在当前 Key 的套餐：Key 本身是好的，绝不能标记失效。
                    # 本轮排除这把、换下一把试试（不同账号套餐可能不同）；
                    # 同时给该账号一个中短冷却，避免后续请求反复白打（额度按小时补充，
                    # 冷却到期自动复探）；若开启智能一换一，后台烧推广池加速回补。
                    # 若所有 Key 都报模型不可用，直接透传上游错误，不空等。
                    err_detail = extract_error(body)
                    delay = self.pool.report_model_unavailable(key, err_detail)
                    self._maybe_flash_lite_exchange(key, err_detail)
                    excluded.add(key.key)
                    self._log(f"[模型] {key.masked}({key.account}) 套餐不含该模型，冷却 {delay:.0f}s 换下一把: {err_detail}")
                    if all(k.key in excluded for k in self.pool.keys):
                        raise ApiError(response.status, body)
                    # 池子已整体不可用（都耗尽/冷却）→ 立即失败，别把剩下的 Key 一把把白打一遍
                    if not self.pool.any_usable():
                        wait = self.pool.soonest_wait()
                        last_exc = NoAvailableKey(
                            f"所有 Key 的套餐额度都不可用（最快 {wait:.0f}s 后复探）",
                            retry_after=wait or None,
                        )
                        break
                    retryable = True
                elif action == "retry":
                    if response.status == 429:
                        delay = self.pool.report_rate_limit(key, retry_after)
                        self.limiter.on_rate_limited()
                        self._log(
                            f"[限流] {key.masked}({key.account}) 429，冷却 {delay:.1f}s "
                            f"(第 {key.consecutive_failures} 次)"
                        )
                    else:
                        delay = self.pool.report_server_error(key, f"HTTP {response.status}")
                        self._log(f"[异常] {key.masked}({key.account}) {response.status}，冷却 {delay:.1f}s")
                    retryable = True
                else:
                    self.pool.report_client_error(key, extract_error(body))
                    raise ApiError(response.status, body)
            except NetworkError as exc:
                delay = self.pool.report_server_error(key, f"{type(exc).__name__}")
                last_exc = exc
                self._log(f"[网络] {key.masked}({key.account}) {exc}，冷却 {delay:.1f}s")
                retryable = True
            finally:
                self.pool.release(key)

            if retryable and attempt < max_attempts:
                self._sleep_between(attempt, started_at, budget)

        raise self._exhausted(max_attempts, last_status, last_body, last_exc)

    # ------------------------------------------------------------ 内部：流式

    def _stream_with_rotation(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        attempts: int | None = None,
        raw: bool = False,
        headers: Mapping[str, str] | None = None,
    ) -> Iterator[Any]:
        max_attempts = attempts or self.config.max_attempts
        excluded: set[str] = set()
        last_status: int | None = None
        last_body = ""
        last_exc: Exception | None = None
        started_at = self._clock()
        budget = self.config.max_total_wait or None

        for attempt in range(1, max_attempts + 1):
            remaining = self._remaining(started_at, budget)
            if remaining is not None and remaining <= 0:
                last_exc = last_exc or TimeoutError(f"超出单请求等待预算 {budget:g}s")
                break

            # 先过限速闸门（不持 Key 状态，避免排队休眠时霸占并发配额与锁死）
            try:
                self.limiter.acquire(timeout=remaining)
            except TimeoutError as exc:
                last_exc = exc
                self._log(f"[放弃] 限速等待超时: {exc}")
                break

            remaining = self._remaining(started_at, budget)
            try:
                key = self.pool.acquire(
                    exclude=excluded,
                    timeout=self._acquire_timeout(remaining),
                    fail_fast=self.config.acquire_fail_fast,
                )
            except AllKeysInvalid:
                self.limiter.refund()
                raise
            except NoAvailableKey as exc:
                self.limiter.refund()
                last_exc = exc
                break

            retryable = False
            try:
                started = self._clock()
                self._note_upstream_attempt()
                req_headers = dict(headers) if headers else {}
                req_headers.update(self._auth(key))
                response = self._client.request(
                    "POST", path, json_body=payload, headers=req_headers, stream=True
                )
                body = safe_text(response) if response.status >= 400 else ""
                action, retry_after = classify(response, body)

                if action == "ok":
                    emitted = False
                    try:
                        for chunk in self._iter_sse_chunks(response):
                            if raw:
                                emitted = True
                                yield chunk
                                continue
                            piece = self._chunk_text(chunk)
                            if not piece:
                                continue
                            emitted = True
                            yield piece
                    except NetworkError as exc:
                        last_exc = exc
                        if emitted:
                            # 已经吐字了，重试会导致内容重复
                            raise StreamInterrupted(f"流式响应中途断开: {exc}") from exc
                        self.pool.report_server_error(key, str(exc))
                        retryable = True
                    else:
                        self.pool.report_success(key, latency=self._clock() - started)
                        self.limiter.on_success()
                        return
                elif action == "invalid":
                    self.pool.report_invalid(key, extract_error(body))
                    excluded.add(key.key)
                    self._log(f"[失效] {key.masked}({key.account}) 凭据无效: {extract_error(body)}")
                    retryable = True
                elif action == "model":
                    # 同非流式：模型不在套餐 ≠ Key 失效，换下一把试试；
                    # 同时给该账号中短冷却，并按需触发一换一烧点。
                    # 所有 Key 都报模型不可用时直接透传上游错误。
                    last_status, last_body = response.status, body
                    err_detail = extract_error(body)
                    delay = self.pool.report_model_unavailable(key, err_detail)
                    self._maybe_flash_lite_exchange(key, err_detail)
                    excluded.add(key.key)
                    self._log(f"[模型] {key.masked}({key.account}) 套餐不含该模型，冷却 {delay:.0f}s 换下一把: {err_detail}")
                    if all(k.key in excluded for k in self.pool.keys):
                        raise ApiError(response.status, body)
                    if not self.pool.any_usable():
                        wait = self.pool.soonest_wait()
                        last_exc = NoAvailableKey(
                            f"所有 Key 的套餐额度都不可用（最快 {wait:.0f}s 后复探）",
                            retry_after=wait or None,
                        )
                        break
                    retryable = True
                elif action == "retry":
                    last_status, last_body = response.status, body
                    if response.status == 429:
                        delay = self.pool.report_rate_limit(key, retry_after)
                        self.limiter.on_rate_limited()
                        self._log(f"[限流] {key.masked}({key.account}) 429，冷却 {delay:.1f}s")
                    else:
                        delay = self.pool.report_server_error(key, f"HTTP {response.status}")
                        self._log(f"[异常] {key.masked}({key.account}) {response.status}，冷却 {delay:.1f}s")
                    retryable = True
                else:
                    self.pool.report_client_error(key, extract_error(body))
                    raise ApiError(response.status, body)
            except NetworkError as exc:
                delay = self.pool.report_server_error(key, type(exc).__name__)
                last_exc = exc
                self._log(f"[网络] {key.masked}({key.account}) {exc}，冷却 {delay:.1f}s")
                retryable = True
            finally:
                self.pool.release(key)

            if retryable and attempt < max_attempts:
                self._sleep_between(attempt, started_at, budget)

        raise self._exhausted(max_attempts, last_status, last_body, last_exc)

    @staticmethod
    def _iter_sse_chunks(response: Response | StreamResponse) -> Iterator[dict[str, Any]]:
        """解析 OpenAI 风格 SSE，逐块 yield 原始 JSON 对象（保留全部字段）。"""
        for line in response.iter_lines():
            if not line:
                continue
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                return
            if not data:
                continue
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            if isinstance(chunk, dict):
                yield chunk

    @staticmethod
    def _chunk_text(chunk: Mapping[str, Any]) -> str | None:
        """从 chunk 里抽出文本增量；没有文本（如纯 tool_calls 块）返回 None。"""
        choices = chunk.get("choices") or []
        if not choices:
            return None
        first = choices[0] or {}
        delta = first.get("delta") or {}
        return delta.get("content") or first.get("text") or None

    @classmethod
    def _iter_sse(cls, response: Response | StreamResponse) -> Iterator[str]:
        """解析 OpenAI 风格 SSE，只 yield 文本增量。"""
        for chunk in cls._iter_sse_chunks(response):
            piece = cls._chunk_text(chunk)
            if piece:
                yield piece

    # ------------------------------------------------------------ 内部：异常

    def _exhausted(
        self,
        attempts: int,
        last_status: int | None,
        last_body: str,
        last_exc: Exception | None,
    ) -> RotatorError:
        # 池子整体不可用（额度耗尽/全在冷却）时保留 NoAvailableKey 语义：
        # 网关会据此返回 429 + Retry-After，上层立刻改道，而不是当成上游故障（502）。
        if isinstance(last_exc, NoAvailableKey):
            return last_exc
        detail = extract_error(last_body) if last_body else (str(last_exc) if last_exc else "未知原因")
        return RotationExhausted(
            f"已尝试 {attempts} 次仍失败，最后一次状态 {last_status or 'N/A'}：{detail}",
            attempts=attempts,
            last_status=last_status,
            last_body=last_body,
        )
