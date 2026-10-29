"""本地控制台：网关的图形界面后端。

定位
----
把「命令行 + 手改 config.json」变成「打开一个窗口就能看、能点」。具体提供：

* 运行状态总览（Key 池、限速、吞吐、运行时长）
* Key 的增 / 删 / 单把体检 —— 不用再手改配置文件
* 对外用户 Key 的签发 / 管理 / 用量监控（每个用户 Key 独立统计输入输出 token、缓存命中）
* 模型选择（清单从上游 ``/models`` 实时拉，带缓存）
* 网关接入信息 + 各语言可直接复制的接入片段
* 实时日志（内存环形缓冲，游标增量拉取，不重复传输）

架构
----
控制台不是独立进程，而是**挂在同一个网关进程上的路由**：

    Edge(app 模式窗口) ──► http://127.0.0.1:8080/  ──► ConsoleState（读状态 / 改配置）
                                     │
                                     └─► /v1/*  ──► 轮换池 ──► 商汤

好处是一个端口同时提供「给上层应用用的 API」和「给人看的界面」，不需要第二个服务、
不需要额外的进程管理。改动立刻生效，因为改的就是当前正在跑的这份内存配置。

安全边界
--------
* 只监听 127.0.0.1（由 CLI 保证），不对外暴露。
* 若网关设了 ``--token``，``/api/*`` 一律要求 Bearer 鉴权；页面本身是空壳，不含密钥，
  所以可以免鉴权加载，Token 由用户在前端输入后存 localStorage。
* 页面展示的 Key 永远是脱敏值；对 Key 的操作走 ``key_id``（sha256 前 12 位），
  前端拿不到也不需要明文。
* 对外用户 Key（见 ``subkeys.py``）只能调用 ``/v1/*`` 接口，控制台接口与页面一律对其
  拒绝（401）；用户 Key 的完整密钥只在创建时返回一次，页面列表只展示脱敏值。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .client import StRotator
from .config import STRATEGIES, Config, ConfigStore, FlashLiteExchangeConfig, RateControlConfig, next_account_name
from .dashboard import DASHBOARD_HTML
from .errors import ConfigError, RotatorError
from .logs import LogBuffer
from .subkeys import SubKeyManager
from .version import __version__

# 控制台页面路径（免鉴权，内容只是空壳）
PAGE_PATHS = frozenset({"/", "/ui", "/ui/"})
# 控制台接口前缀（需要鉴权）
API_PREFIX = "/api/"

MAX_KEYS_PER_REQUEST = 20
_KEY_SPLIT = re.compile(r"[\s,;]+")


def parse_key_list(raw: str) -> list[str]:
    """把用户粘贴的一大坨文本拆成 Key 列表（逗号 / 分号 / 换行 / 空格都认）。"""
    seen: set[str] = set()
    result: list[str] = []
    for token in _KEY_SPLIT.split(raw or ""):
        token = token.strip().strip('"\'')
        if not token or token in seen:
            continue
        seen.add(token)
        result.append(token)
    return result


# ---------------------------------------------------------------- 指标


class GatewayMetrics:
    """网关侧计数。线程安全，读多写少。

    只统计"客户端视角"的量。上游尝试次数由 ``StRotator`` 自己维护——
    因为重试发生在客户端内部，网关层看不到轮换了几次。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.started_at = time.time()
        self.client_requests = 0
        self.stream_requests = 0
        self.errors = 0
        self.paused = False

    def note_request(self, *, stream: bool = False) -> None:
        with self._lock:
            self.client_requests += 1
            if stream:
                self.stream_requests += 1

    def note_error(self) -> None:
        with self._lock:
            self.errors += 1

    def set_paused(self, paused: bool) -> bool:
        with self._lock:
            self.paused = bool(paused)
            return self.paused

    @property
    def is_paused(self) -> bool:
        with self._lock:
            return self.paused

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "client_requests": self.client_requests,
                "stream_requests": self.stream_requests,
                "errors": self.errors,
                "paused": self.paused,
                "started_at": self.started_at,
                "uptime_seconds": round(time.time() - self.started_at, 1),
            }


# ---------------------------------------------------------------- 响应对象


@dataclass
class UiResponse:
    """控制台路由的处理结果。

    用数据对象而不是直接往 socket 写，是为了让路由逻辑可以脱离真实 HTTP 连接做单测。
    """

    status: int = 200
    payload: Any = None
    raw: str | None = None
    content_type: str = "application/json; charset=utf-8"
    retry_after: float | None = None

    @classmethod
    def json(cls, payload: Any, status: int = 200) -> "UiResponse":
        return cls(status=status, payload=payload)

    @classmethod
    def error(cls, message: str, status: int = 400, code: str | None = None) -> "UiResponse":
        return cls(
            status=status,
            payload={"ok": False, "error": {"message": message, "code": code, "type": "console_error"}},
        )

    @classmethod
    def html(cls, text: str) -> "UiResponse":
        return cls(payload=None, raw=text, content_type="text/html; charset=utf-8")


# ---------------------------------------------------------------- 控制台状态


@dataclass
class ConsoleState:
    """控制台的读写入口：一份内存配置 + 一个配置文件 + 一个日志缓冲。

    Attributes:
        store: 配置文件读写器（保留原始结构，支持 ${ENV} 占位符）。
        rotator: 正在跑的轮换客户端（改它就是改运行中的服务）。
        host / port: 网关监听地址，用于拼接入信息。
        token: 本地鉴权 Token；为空表示不鉴权。
        buffer: 日志环形缓冲。
        log_file: 日志文件路径（仅用于展示）。
        subkeys: 对外用户 Key 管理器；为 None 表示未启用用户 Key 功能。
    """

    store: ConfigStore
    rotator: StRotator
    host: str = "127.0.0.1"
    port: int = 8080
    token: str | None = None
    buffer: LogBuffer = field(default_factory=LogBuffer)
    log_file: str | None = None
    metrics: GatewayMetrics = field(default_factory=GatewayMetrics)
    lock: threading.Lock = field(default_factory=threading.Lock)
    subkeys: SubKeyManager | None = None

    # ------------------------------------------------------------ 只读

    @property
    def config(self) -> Config:
        return self.rotator.config

    def gateway_info(self) -> dict[str, Any]:
        """给上层应用抄的接入信息。"""
        base = f"http://{self.host}:{self.port}"
        return {
            "listen": f"{self.host}:{self.port}",
            "base_url": f"{base}/v1",
            "chat_endpoint": f"{base}/v1/chat/completions",
            "models_endpoint": f"{base}/v1/models",
            "health_endpoint": f"{base}/healthz",
            "stats_endpoint": f"{base}/stats",
            "console_url": f"{base}/",
            "token": self.token or "",
            "model": self.config.default_model,
            "upstream": self.config.base_url,
        }

    def snapshot(self) -> dict[str, Any]:
        """整页刷新所需的全部状态。"""
        rate = self.rotator.limiter.stats()
        metrics = self.metrics.snapshot()
        metrics["upstream_attempts"] = self.rotator.upstream_attempts
        return {
            "version": __version__,
            "gateway": self.gateway_info(),
            "summary": self.rotator.pool.summary(),
            "keys": self.rotator.pool.snapshot(),
            "rate_control": rate,
            "metrics": metrics,
            "models": self.rotator.available_models(),
            "default_model": self.config.default_model,
            "available_models": (
                list(getattr(self.config, "model_filter", None).available)
                if getattr(self.config, "model_filter", None) is not None
                else []
            ),
            "log_file": self.log_file,
            "subkeys": self._subkeys_snapshot(),
            "options": {
                "strategy": self.config.strategy,
                "rate_mode": self.config.rate_control.mode,
                "qps": self.config.rate_control.qps,
                "min_qps": self.config.rate_control.min_qps,
                "max_qps": self.config.rate_control.max_qps,
                "max_total_wait": self.config.max_total_wait,
                "max_attempts": self.config.max_attempts,
                "strategies": list(STRATEGIES),
                "rate_modes": list(RateControlConfig.MODES),
                "flash_lite": {
                    "enabled": self.config.flash_lite_exchange.enabled,
                    "model": self.config.flash_lite_exchange.model,
                    "concurrency": self.config.flash_lite_exchange.concurrency,
                    "requests_per_trigger": self.config.flash_lite_exchange.requests_per_trigger,
                    "min_interval_s": self.config.flash_lite_exchange.min_interval_s,
                    "long_text_max_tokens": self.config.flash_lite_exchange.long_text_max_tokens,
                    "image_enabled": self.config.flash_lite_exchange.image_enabled,
                    "image_size": self.config.flash_lite_exchange.image_size,
                    "multi_image_count": self.config.flash_lite_exchange.multi_image_count,
                    "yield_to_serve": self.config.flash_lite_exchange.yield_to_serve,
                    "min_available_mb": self.config.flash_lite_exchange.min_available_mb,
                },
            },
        }

    def logs_since(self, cursor: int) -> dict[str, Any]:
        new_cursor, items = self.buffer.since(cursor)
        return {"cursor": new_cursor, "items": items}

    def _subkeys_snapshot(self) -> dict[str, Any] | None:
        """用户 Key 区块的状态；未启用时为 None（前端显示不可用提示）。"""
        if self.subkeys is None:
            return None
        return {
            "configured": True,
            "admin_token_set": bool(self.token),
            "keys": self.subkeys.snapshot(),
        }

    # ------------------------------------------------------------ 用户 Key 管理

    def _require_subkeys(self) -> SubKeyManager | None:
        if self.subkeys is None:
            raise ConfigError("未启用用户功能（缺少 subkeys 管理器）")
        return self.subkeys

    def create_subkey(self, name: str, note: str = "") -> UiResponse:
        """创建一个对外用户 Key；完整密钥只在创建响应里返回一次。"""
        try:
            manager = self._require_subkeys()
            sub = manager.create(name, note=note)
        except ConfigError:
            return UiResponse.error("未启用用户功能", status=404)
        except ValueError as exc:  # 名称空 / 太长 / 重名
            return UiResponse.error(str(exc))
        self._log(f"[用户] 创建 {sub.name}（{sub.masked}）")
        return UiResponse.json(
            {
                "ok": True,
                "id": sub.id,
                "name": sub.name,
                "key": sub.key,
                "message": f"已创建用户 Key：{sub.name}。完整密钥只显示这一次，请立即复制保存。",
            }
        )

    def update_subkey(self, identifier: str, *, name: str | None = None, note: str | None = None) -> UiResponse:
        try:
            manager = self._require_subkeys()
            sub = manager.update(identifier, name=name, note=note)
        except ConfigError:
            return UiResponse.error("未启用用户功能", status=404)
        except ValueError as exc:
            return UiResponse.error(str(exc))
        except KeyError as exc:
            return UiResponse.error(str(exc), status=404)
        self._log(f"[用户] 更新 {sub.name} 信息")
        return UiResponse.json({"ok": True, "message": f"已更新用户：{sub.name}"})

    def toggle_subkey(self, identifier: str, enabled: bool) -> UiResponse:
        try:
            manager = self._require_subkeys()
            sub = manager.set_enabled(identifier, enabled)
        except ConfigError:
            return UiResponse.error("未启用用户功能", status=404)
        except KeyError as exc:
            return UiResponse.error(str(exc), status=404)
        state = "启用" if sub.enabled else "停用"
        self._log(f"[用户] {state} {sub.name}（{sub.masked}）")
        return UiResponse.json({"ok": True, "enabled": sub.enabled, "message": f"已{state}用户：{sub.name}"})

    def revoke_subkey(self, identifier: str) -> UiResponse:
        try:
            manager = self._require_subkeys()
            sub = manager.revoke(identifier)
        except ConfigError:
            return UiResponse.error("未启用用户功能", status=404)
        except KeyError as exc:
            return UiResponse.error(str(exc), status=404)
        self._log(f"[用户] 删除 {sub.name}（{sub.masked}）")
        return UiResponse.json({"ok": True, "message": f"已删除用户：{sub.name}，该 Key 立即失效"})

    def reset_subkey_usage(self, identifier: str) -> UiResponse:
        try:
            manager = self._require_subkeys()
            sub = manager.reset_usage(identifier)
        except ConfigError:
            return UiResponse.error("未启用用户功能", status=404)
        except KeyError as exc:
            return UiResponse.error(str(exc), status=404)
        self._log(f"[用户] 清零用量：{sub.name}")
        return UiResponse.json({"ok": True, "message": f"已清零用户 {sub.name} 的用量统计"})

    def subkey_history(self, page: int = 1, page_size: int = 10) -> UiResponse:
        """用户用量明细分页：默认每页 10 条，最新在前。"""
        if self.subkeys is None:
            return UiResponse.error("未启用用户功能", status=404)
        page = max(1, int(page))
        page_size = max(1, min(100, int(page_size)))
        offset = (page - 1) * page_size
        return UiResponse.json(
            {
                "ok": True,
                "page": page,
                "page_size": page_size,
                "total": self.subkeys.history_count(),
                "history": self.subkeys.history_snapshot(limit=page_size, offset=offset),
            }
        )

    # ------------------------------------------------------------ 写操作

    def add_keys(
        self,
        raw: str,
        *,
        account: str | None = None,
        max_concurrency: int = 16,
        rpm_limit: int | None = None,
        verify: bool = True,
    ) -> UiResponse:
        """批量加 Key：先体检，再把好 Key 同时写进内存池和配置文件。

        「体检」的判定标准很讲究：只有 **401/403** 才拒收。429 不算——限流恰恰说明
        凭据有效，只是当下没额度；如果把它当成失效，批量导入时会误杀好 Key。
        """
        candidates = parse_key_list(raw)
        if not candidates:
            return UiResponse.error("没有解析出任何 Key")
        if len(candidates) > MAX_KEYS_PER_REQUEST:
            return UiResponse.error(f"一次最多添加 {MAX_KEYS_PER_REQUEST} 把 Key，当前 {len(candidates)} 把")

        added: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        warnings: list[str] = []

        # 整批 Key 落在同一个账号下。账号级 rpm_limit 与 429 冷却都是按账号聚合的
        # （见 keypool.AccountState），如果逐把 Key 现算名字，它们会被拆成 N 个账号，
        # 聚合与联动冷却就全失效了 —— 每把 Key 独占一份配额，同账号雪崩照旧。
        # 名字只算一次、整批复用；想让它们分属不同账号，请分多批并显式指定账号名。
        with self.lock:
            target_account = account or next_account_name(self.store.account_names())

        for key in candidates:
            if len(key) < 8:
                rejected.append({"key": _mask(key), "reason": "长度不足 8 位，不像有效 Key"})
                continue
            if self.rotator.pool.find_key(key) is not None:
                rejected.append({"key": _mask(key), "reason": "已在池中，跳过"})
                continue

            verdict, detail = ("ok", "未校验")
            if verify:
                verdict, detail = self.rotator.probe_key(key)
                if verdict == "invalid":
                    rejected.append({"key": _mask(key), "reason": f"凭据无效：{detail}"})
                    continue
                if verdict == "unknown":
                    warnings.append(f"{_mask(key)} 暂时无法确认（{detail}），已按可用处理")

            with self.lock:
                try:
                    self.store.add_key(key, target_account, max_concurrency=max_concurrency, rpm_limit=rpm_limit)
                except ConfigError as exc:
                    rejected.append({"key": _mask(key), "reason": str(exc)})
                    continue
                try:
                    item = self.rotator.add_key(
                        key, account=target_account, max_concurrency=max_concurrency, rpm_limit=rpm_limit
                    )
                except ConfigError as exc:
                    # 内存池拒绝 → 回滚刚才写进 store 的那一条，保持两边一致
                    self.store.remove_key(key)
                    rejected.append({"key": _mask(key), "reason": str(exc)})
                    continue
                self.store.reload()
                self.rotator.config.accounts = list(self.store.config.accounts)
            added.append({"id": item.key_id, "key": item.masked, "account": item.account})

        if added:
            self._save_and_log(f"通过控制台新增 {len(added)} 把 Key（账号 {target_account}）")
        if not added and not rejected:
            return UiResponse.error("没有可添加的 Key")
        message = f"新增 {len(added)} 把"
        if added:
            message += f"（归入账号 {target_account}，同账号共享配额与 429 冷却）"
        if rejected:
            message += f"，跳过 {len(rejected)} 把"
        return UiResponse.json({
            "ok": bool(added),
            "account": target_account,
            "added": added,
            "rejected": rejected,
            "warnings": warnings,
            "message": message,
        })

    def verify_one(self, identifier: str) -> UiResponse:
        """体检池中某一把 Key（identifier 可以是 key_id 或明文）。"""
        item = self.rotator.pool.find_by_id(identifier)
        if item is None:
            return UiResponse.error("池中找不到这把 Key（可能已被删除）", status=404)
        verdict, detail = self.rotator.probe_key(item.key)
        labels = {"ok": "凭据有效", "invalid": "凭据无效", "unknown": "暂时无法确认"}
        return UiResponse.json({
            "ok": verdict != "invalid",
            "verdict": verdict,
            "detail": detail,
            "message": f"{item.account} / {item.masked}：{labels.get(verdict, verdict)}（{detail}）",
        })

    def remove_one(self, identifier: str) -> UiResponse:
        """从内存池和配置文件里同时删除一把 Key。"""
        item = self.rotator.pool.find_by_id(identifier)
        if item is None:
            return UiResponse.error("池中找不到这把 Key（可能已被删除）", status=404)
        plain, label = item.key, f"{item.account} / {item.masked}"
        if self.rotator.pool.find_key(plain) is not None and len(self.rotator.pool) <= 1:
            return UiResponse.error("这是池里最后一把 Key，删掉后就无法提供服务了；请先添加新 Key")

        with self.lock:
            removed = self.rotator.remove_key(plain)
            self.store.remove_key(plain)
            self.store.reload()
            self.rotator.config.accounts = list(self.store.config.accounts)
        if not removed:
            return UiResponse.error("删除失败：Key 已不在池中", status=409)
        self._save_and_log(f"通过控制台删除 Key：{label}")
        return UiResponse.json({"ok": True, "message": f"已删除 {label}"})

    def set_model(self, model: str) -> UiResponse:
        model = (model or "").strip()
        if not model:
            return UiResponse.error("模型名不能为空")
        known = {m["id"] for m in self.rotator.available_models().get("models", [])}
        with self.lock:
            self.rotator.set_default_model(model)
            self.store.set_default_model(model)
        self._save_and_log(f"默认模型切换为 {model}")
        note = "" if not known or model in known else "（不在上游清单里，请确认拼写）"
        return UiResponse.json({"ok": True, "model": model, "message": f"默认模型已切换为 {model}{note}"})

    def set_available_models(self, payload: Mapping[str, Any]) -> UiResponse:
        """改可用模型白名单：非空时只有列出的模型能被调用（空 = 全部可用）。

        属于模型配置（控制台「默认模型」卡片里选择式设置），与「运行参数」无关。
        """
        raw = payload.get("models")
        if raw is None:
            return UiResponse.error("缺少 models 字段")
        if not isinstance(raw, (list, tuple)):
            return UiResponse.error("可用模型必须是模型名数组，例如 [\"deepseek-v4.1-flash\"]")
        cleaned: list[str] = []
        for item in raw:
            if not isinstance(item, str):
                return UiResponse.error(f"可用模型只接受字符串，收到 {item!r}")
            name = item.strip()
            if name and name not in cleaned:
                cleaned.append(name)
        flt = getattr(self.config, "model_filter", None)
        current = list(flt.available) if flt is not None else []
        if cleaned == current:
            return UiResponse.json({"ok": True, "message": "可用模型没有变化"})
        with self.lock:
            self.rotator.set_model_filter(cleaned)
            self.store.set_section("model_filter", {"available": cleaned})
            self.store.reload()
        label = "全部（不限）" if not cleaned else "、".join(cleaned)
        self._save_and_log(f"可用模型更新为 {label}")
        return UiResponse.json({"ok": True, "message": f"可用模型已更新：{label}"})

    def set_options(self, payload: Mapping[str, Any]) -> UiResponse:
        """改运行参数：调度策略 / 限速 / 重试预算。改动立即生效并落盘。"""
        changes: list[str] = []

        strategy = payload.get("strategy")
        if strategy is not None and strategy != self.config.strategy:
            if strategy not in STRATEGIES:
                return UiResponse.error(f"调度策略只能是 {list(STRATEGIES)} 之一")
            with self.lock:
                self.rotator.set_strategy(strategy)
                self.store.set_strategy(strategy)
            changes.append(f"策略={strategy}")

        rate_fields: dict[str, Any] = {}
        mode = payload.get("rate_mode")
        if mode is not None and mode != self.config.rate_control.mode:
            if mode not in RateControlConfig.MODES:
                return UiResponse.error(f"限速模式只能是 {list(RateControlConfig.MODES)} 之一")
            rate_fields["mode"] = mode
        qps = payload.get("qps")
        if qps is not None:
            try:
                qps = float(qps)
            except (TypeError, ValueError):
                return UiResponse.error(f"QPS 不是合法数字：{qps!r}")
            if qps < 0:
                return UiResponse.error("QPS 不能为负")
            effective_mode = rate_fields.get("mode") or self.config.rate_control.mode
            if effective_mode != "off" and qps <= 0:
                return UiResponse.error(f"限速模式为 {effective_mode} 时 QPS 必须大于 0")
            if qps != self.config.rate_control.qps:
                rate_fields["qps"] = qps
        if rate_fields:
            try:
                with self.lock:
                    self.rotator.set_rate_control(**rate_fields)
                    self.store.set_rate_control(**rate_fields)
            except ConfigError as exc:
                return UiResponse.error(str(exc))
            changes.append("限速=" + " ".join(f"{k}:{v}" for k, v in rate_fields.items()))

        for field_name, label in (("max_total_wait", "等待预算"), ("max_attempts", "最大重试")):
            value = payload.get(field_name)
            if value is None:
                continue
            try:
                converted = int(value) if field_name == "max_attempts" else float(value)
            except (TypeError, ValueError):
                return UiResponse.error(f"{label} 不是合法数字：{value!r}")
            if field_name == "max_attempts" and converted < 1:
                return UiResponse.error("最大重试次数必须 >= 1")
            if field_name == "max_total_wait" and converted < 0:
                return UiResponse.error("等待预算不能为负")
            if converted == getattr(self.config, field_name):
                continue
            with self.lock:
                setattr(self.config, field_name, converted)
                self.store.set_scalar(field_name, converted)
            changes.append(f"{label}={converted}")

        available_models = payload.get("available_models")
        if available_models is not None:
            if not isinstance(available_models, (list, tuple)):
                return UiResponse.error("可用模型必须是模型名数组，例如 [\"deepseek-v4.1-flash\"]")
            cleaned: list[str] = []
            for item in available_models:
                if not isinstance(item, str):
                    return UiResponse.error(f"可用模型只接受字符串，收到 {item!r}")
                name = item.strip()
                if name and name not in cleaned:
                    cleaned.append(name)
            flt = getattr(self.config, "model_filter", None)
            current = list(flt.available) if flt is not None else []
            if cleaned != current:
                with self.lock:
                    self.rotator.set_model_filter(cleaned)
                    self.store.set_section("model_filter", {"available": cleaned})
                    self.store.reload()
                changes.append("可用模型=" + ("全部（不限）" if not cleaned else "、".join(cleaned)))

        fx_payload = payload.get("flash_lite")
        if fx_payload is not None:
            if not isinstance(fx_payload, dict):
                return UiResponse.error("flash_lite 必须是对象")
            fx_cfg = self.config.flash_lite_exchange
            fx_changes: dict[str, Any] = {}
            for f in ("enabled", "image_enabled", "yield_to_serve"):
                if f in fx_payload:
                    fx_changes[f] = bool(fx_payload[f])
            fx_num_fields = (
                ("concurrency", int, 1, 64, "一换一并发"),
                ("requests_per_trigger", int, 1, 2048, "一换一单轮上限"),
                ("min_interval_s", float, 60.0, 86400.0, "一换一触发间隔"),
                ("long_text_max_tokens", int, 128, 16384, "一换一长文预算"),
                ("image_size", int, 256, 2048, "一换一大图边长"),
                ("multi_image_count", int, 1, 9, "一换一每请求图片数"),
                ("min_available_mb", float, 0.0, 8192.0, "一换一内存下限(MB)"),
            )
            for f, typ, lo, hi, label in fx_num_fields:
                if f not in fx_payload:
                    continue
                try:
                    v = typ(fx_payload[f])
                except (TypeError, ValueError):
                    return UiResponse.error(f"{label}不是合法数字：{fx_payload[f]!r}")
                if not lo <= v <= hi:
                    return UiResponse.error(f"{label}需在 {lo:g}~{hi:g} 之间")
                fx_changes[f] = v
            fx_changes = {k: v for k, v in fx_changes.items() if getattr(fx_cfg, k) != v}
            if fx_changes:
                # 先在纯数据上整体校验（不改内存态），通过后再落盘 + 生效
                candidate = {f: getattr(fx_cfg, f) for f in FlashLiteExchangeConfig.__dataclass_fields__}
                candidate.update(fx_changes)
                try:
                    FlashLiteExchangeConfig.from_dict(candidate)
                except ConfigError as exc:
                    return UiResponse.error(str(exc))
                with self.lock:
                    for k, v in fx_changes.items():
                        setattr(fx_cfg, k, v)
                    self.store.set_section("flash_lite_exchange", fx_changes)
                changes.append("一换一 " + "，".join(f"{k}:{v}" for k, v in fx_changes.items()))

        if not changes:
            return UiResponse.json({"ok": True, "message": "没有需要改动的参数"})
        self._save_and_log("运行参数已更新：" + "，".join(changes))
        return UiResponse.json({"ok": True, "message": "已更新：" + "，".join(changes)})

    def set_paused(self, paused: bool) -> UiResponse:
        """暂停 / 恢复对外服务（网关进程和控制台都还活着）。"""
        state = self.metrics.set_paused(paused)
        self._log(f"[控制台] {'暂停' if state else '恢复'}对外接入")
        return UiResponse.json({
            "ok": True,
            "paused": state,
            "message": "已暂停接入，上层会收到 503" if state else "已恢复接入",
        })

    def refresh_models(self) -> UiResponse:
        try:
            catalog = self.rotator.available_models(refresh=True)
        except RotatorError as exc:
            return UiResponse.error(f"拉取模型清单失败：{exc}", status=502)
        if catalog.get("error") and not catalog.get("models"):
            return UiResponse.error(f"拉取模型清单失败：{catalog['error']}", status=502)
        # 计数按「放行 + 被白名单拦截」= 完整上游清单，避免白名单生效时只报放行数。
        total = len(catalog.get("models", [])) + len(catalog.get("blocked", []))
        return UiResponse.json({
            "ok": True,
            "count": total,
            "message": f"已拉取 {total} 个模型",
        })

    # ------------------------------------------------------------ 内部

    def _save_and_log(self, note: str) -> None:
        """落盘 + 记一条日志。落盘失败不该让操作回滚（内存里已经生效了）。"""
        try:
            self.store.save()
        except OSError as exc:
            self._log(f"[警告] 配置写盘失败：{exc}（本次改动只在内存中生效）")
        self._log(f"[控制台] {note}")

    def _log(self, message: str) -> None:
        self.buffer.append(message)
        try:
            self.rotator._log(message)  # noqa: SLF001 - 复用同一套日志出口
        except Exception:  # pragma: no cover
            pass

    # ------------------------------------------------------------ 路由

    @staticmethod
    def is_console_path(path: str) -> bool:
        return path in PAGE_PATHS or path.startswith(API_PREFIX)

    def handle(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, Sequence[str]] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> UiResponse | None:
        """处理控制台请求；路径不属于控制台时返回 None，交回网关处理。"""
        if method == "GET" and path in PAGE_PATHS:
            return UiResponse.html(DASHBOARD_HTML)
        if not path.startswith(API_PREFIX):
            return None

        try:
            if method == "GET":
                if path == "/api/state":
                    return UiResponse.json(self.snapshot())
                if path == "/api/logs":
                    cursor = _first_int(query, "cursor", 0)
                    return UiResponse.json(self.logs_since(cursor))
                if path == "/api/subkeys/history":
                    return self.subkey_history(
                        _clamp_int(_first_int(query, "page", 1), 1, 1, 100000),
                        _clamp_int(_first_int(query, "page_size", 10), 10, 1, 100),
                    )
                return UiResponse.error(f"未知接口 {path}", status=404)

            if method == "POST":
                payload = dict(body or {})
                if path == "/api/keys/add":
                    rpm_limit_raw = payload.get("rpm_limit")
                    rpm_limit: int | None = None
                    if rpm_limit_raw is not None and str(rpm_limit_raw).strip() != "":
                        try:
                            rpm_limit = _clamp_int(rpm_limit_raw, 30, 1, 10000)
                        except Exception:
                            rpm_limit = None
                    return self.add_keys(
                        str(payload.get("keys") or payload.get("key") or ""),
                        account=(str(payload.get("account")).strip() or None) if payload.get("account") else None,
                        max_concurrency=_clamp_int(payload.get("max_concurrency"), 16, 1, 256),
                        rpm_limit=rpm_limit,
                    )
                if path == "/api/keys/verify":
                    return self.verify_one(str(payload.get("id") or payload.get("key") or ""))
                if path == "/api/keys/remove":
                    return self.remove_one(str(payload.get("id") or payload.get("key") or ""))
                if path == "/api/model":
                    return self.set_model(str(payload.get("model") or ""))
                if path == "/api/model/available":
                    return self.set_available_models(payload)
                if path == "/api/models/refresh":
                    return self.refresh_models()
                if path == "/api/options":
                    return self.set_options(payload)
                if path == "/api/pause":
                    return self.set_paused(bool(payload.get("paused")))
                if path == "/api/subkeys/create":
                    return self.create_subkey(
                        str(payload.get("name") or ""), note=str(payload.get("note") or "")
                    )
                if path == "/api/subkeys/update":
                    return self.update_subkey(
                        str(payload.get("id") or ""),
                        name=payload.get("name"),
                        note=payload.get("note"),
                    )
                if path == "/api/subkeys/toggle":
                    return self.toggle_subkey(
                        str(payload.get("id") or ""), bool(payload.get("enabled"))
                    )
                if path == "/api/subkeys/revoke":
                    return self.revoke_subkey(str(payload.get("id") or ""))
                if path == "/api/subkeys/reset":
                    return self.reset_subkey_usage(str(payload.get("id") or ""))
                return UiResponse.error(f"未知接口 {path}", status=404)

            return UiResponse.error(f"不支持的方法 {method}", status=405)
        except ConfigError as exc:
            return UiResponse.error(str(exc))
        except RotatorError as exc:
            return UiResponse.error(f"{type(exc).__name__}: {exc}", status=502)
        except Exception as exc:  # pragma: no cover - 控制台不该把网关搞崩
            return UiResponse.error(f"控制台内部错误：{type(exc).__name__}: {exc}", status=500)


def _mask(key: str) -> str:
    from .keypool import mask_key

    return mask_key(key)


def _first_int(query: Mapping[str, Sequence[str]] | None, name: str, default: int) -> int:
    if not query:
        return default
    values = query.get(name)
    if not values:
        return default
    try:
        return int(values[0])
    except (TypeError, ValueError, IndexError):
        return default


def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


# ---------------------------------------------------------------- 开窗


EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)
CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)


def find_app_browser(explicit: str | None = None) -> str | None:
    """找一个支持 ``--app`` 模式的浏览器（Edge 优先，其次 Chrome）。"""
    if explicit:
        return explicit if Path(explicit).is_file() else None
    for name in ("msedge", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    for candidate in EDGE_CANDIDATES + CHROME_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


def default_profile_dir() -> Path:
    """独立浏览器配置目录。

    用独立 profile 有两个好处：一是窗口不受用户现有浏览器会话/插件影响，真正像
    一个独立应用；二是 localStorage（存的访问 Token）能跨次启动保留。
    """
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "st-rotator" / "console-profile"


def open_console_window(
    url: str,
    *,
    browser: str | None = None,
    profile_dir: str | os.PathLike[str] | None = None,
    size: str = "1380,900",
    detach: bool = True,
) -> tuple[bool, str]:
    """用浏览器 app 模式开一个无地址栏的独立窗口。

    Returns:
        ``(是否成功, 说明)``
    """
    executable = find_app_browser(browser)
    if not executable:
        return False, "未找到 Edge / Chrome，请手动用浏览器打开该地址"
    profile = Path(profile_dir) if profile_dir else default_profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    command = [
        executable,
        f"--app={url}",
        f"--window-size={size}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,MSImplicitSignin",
    ]
    kwargs: dict[str, Any] = {"close_fds": True}
    if detach:
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
    try:
        subprocess.Popen(command, **kwargs)  # noqa: S603 - 路径来自固定候选/用户显式指定
    except OSError as exc:
        return False, f"启动 {Path(executable).name} 失败：{exc}"
    return True, f"已用 {Path(executable).name} 打开控制台窗口"


def open_in_default_browser(url: str) -> bool:
    import webbrowser

    try:
        return webbrowser.open(url)
    except Exception:  # pragma: no cover
        return False
