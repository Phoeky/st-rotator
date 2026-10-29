"""对外用户 Key 管理：为上层应用 / 他人签发独立凭据，并监控其使用情况。

用户 Key 与本网关的“管理 Token”（--token）是两套独立凭据：

* 管理 Token：可以访问控制台（/ui、/api/*），也可以调用 /v1/* 接口；
* 用户 Key（sk-sr-...）：只能调用 /v1/* 与健康检查接口，永远不能访问控制台。

每个用户 Key 独立统计请求数、成功 / 失败、输入 / 输出 token 与缓存命中次数，
并保留最近的使用历史，供管理页面监控。数据保存在配置目录下的
``subkeys.json``（与 config.json、管理 Token 分开存储），零第三方依赖。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

_HISTORY_LIMIT = 1000
_EMPTY_USAGE = {
    "requests": 0,
    "successes": 0,
    "errors": 0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "cached_tokens": 0,
    "estimated_prompt_tokens": 0,
    "estimated_completion_tokens": 0,
    "last_used_at": None,
}


def _mask_key(key: str) -> str:
    """脱敏显示：sk-sr-abcdef...wxyz 样式（与 keypool.mask_key 规则一致）。"""
    if len(key) <= 6:
        return key[0] + "*" * (len(key) - 1)
    if len(key) <= 14:
        return f"{key[:2]}{'*' * (len(key) - 4)}{key[-2:]}"
    return f"{key[:6]}...{key[-4:]}"


def _key_id(key: str) -> str:
    """稳定、不泄露密钥内容的标识符（sha256 前 12 位）。"""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


@dataclass
class SubKey:
    """一个对外用户 Key 及其用量统计。字段变化均在 SubKeyManager 锁内进行。"""

    name: str
    key: str
    enabled: bool = True
    created_at: str = ""
    note: str = ""
    usage: Dict[str, Any] = field(default_factory=lambda: dict(_EMPTY_USAGE))

    @property
    def id(self) -> str:
        return _key_id(self.key)

    @property
    def masked(self) -> str:
        return _mask_key(self.key)


class SubKeyManager:
    """用户 Key 的存储、认证与用量统计。

    线程安全：所有读写操作在内部 RLock 保护下进行；修改会在后台线程定期
    原子写入 ``subkeys.json``（退出时调用 :meth:`close` 做最后落盘）。
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        clock: Callable[[], float] = time.time,
        logger: Optional[Callable[[str], None]] = None,
        autosave_interval: float = 60.0,
    ) -> None:
        self._path = Path(path)
        self._clock = clock
        self._logger = logger or (lambda message: None)
        self._lock = threading.RLock()
        self._keys: List[SubKey] = []
        self._history: Deque[Dict[str, Any]] = deque(maxlen=_HISTORY_LIMIT)
        self._dirty = False
        self._stop = threading.Event()
        self._autosave_interval = autosave_interval
        self._thread: Optional[threading.Thread] = None
        self._load()
        if autosave_interval and autosave_interval > 0:
            self._thread = threading.Thread(
                target=self._autosave_loop,
                name="subkeys-autosave",
                daemon=True,
            )
            self._thread.start()

    # ------------------------------------------------------------------ 持久化

    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            self._logger(f"读取用户 Key 数据失败（将使用空数据）：{exc}")
            return
        if not isinstance(data, dict) or data.get("version") != 1:
            self._logger("用户 Key 数据文件版本不识别，已忽略")
            return
        for item in data.get("keys", []):
            if not isinstance(item, dict):
                continue
            if not isinstance(item.get("name"), str) or not isinstance(item.get("key"), str):
                continue
            usage = dict(item.get("usage") or {})
            for key, value in _EMPTY_USAGE.items():
                usage.setdefault(key, value)
            self._keys.append(
                SubKey(
                    name=item["name"],
                    key=item["key"],
                    enabled=bool(item.get("enabled", True)),
                    created_at=str(item.get("created_at") or ""),
                    note=str(item.get("note") or ""),
                    usage=usage,
                )
            )
        history = data.get("history") or []
        for event in history[-_HISTORY_LIMIT:]:
            if isinstance(event, dict):
                self._history.append(event)

    def _to_dict(self) -> Dict[str, Any]:
        return {
            "version": 1,
            "keys": [
                {
                    "name": key.name,
                    "key": key.key,
                    "enabled": key.enabled,
                    "created_at": key.created_at,
                    "note": key.note,
                    "usage": dict(key.usage),
                }
                for key in self._keys
            ],
            "history": list(self._history),
        }

    def _save_locked(self) -> bool:
        directory = self._path.parent
        try:
            directory.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                prefix=".subkeys-", suffix=".tmp", dir=str(directory)
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(self._to_dict(), handle, ensure_ascii=False, indent=2)
                os.replace(tmp_path, self._path)
            except BaseException:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
            self._dirty = False
            return True
        except OSError as exc:
            self._logger(f"保存用户 Key 数据失败：{exc}")
            return False

    def save(self) -> bool:
        """立即落盘（幂等；内部有脏标记，无修改时不会重复写）。"""
        with self._lock:
            return self._save_locked()

    def close(self) -> None:
        """停止后台自动保存线程并做最后一次落盘。"""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        with self._lock:
            if self._dirty:
                self._save_locked()

    def _autosave_loop(self) -> None:
        while not self._stop.wait(self._autosave_interval):
            try:
                with self._lock:
                    if self._dirty:
                        self._save_locked()
            except Exception as exc:  # 自动保存失败不影响网关运行
                self._logger(f"用户 Key 自动保存失败：{exc}")

    # ------------------------------------------------------------------ 查询

    def count(self) -> int:
        with self._lock:
            return len(self._keys)

    def any_enabled(self) -> bool:
        with self._lock:
            return any(key.enabled for key in self._keys)

    def find(self, identifier: str) -> Optional[SubKey]:
        """按 id（sha256 前 12 位）、完整 Key 或名称查找用户 Key。"""
        if not identifier:
            return None
        with self._lock:
            for key in self._keys:
                if key.id == identifier or key.key == identifier or key.name == identifier:
                    return key
        return None

    def snapshot(self) -> List[Dict[str, Any]]:
        """全部用户 Key 的展示快照（新创建的在前）。"""
        with self._lock:
            items: List[Dict[str, Any]] = []
            for key in self._keys:
                usage = key.usage
                prompt = usage.get("prompt_tokens", 0)
                completion = usage.get("completion_tokens", 0)
                items.append(
                    {
                        "id": key.id,
                        "name": key.name,
                        "key": key.key,
                        "masked": key.masked,
                        "enabled": key.enabled,
                        "created_at": key.created_at,
                        "note": key.note,
                        "usage": {
                            "requests": usage.get("requests", 0),
                            "successes": usage.get("successes", 0),
                            "errors": usage.get("errors", 0),
                            "prompt_tokens": prompt,
                            "completion_tokens": completion,
                            "total_tokens": prompt + completion,
                            "cached_tokens": usage.get("cached_tokens", 0),
                            "estimated_prompt_tokens": usage.get("estimated_prompt_tokens", 0),
                            "estimated_completion_tokens": usage.get("estimated_completion_tokens", 0),
                            "last_used_at": usage.get("last_used_at"),
                        },
                    }
                )
            items.sort(key=lambda item: item["created_at"], reverse=True)
            return items

    def history_snapshot(self, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        """最近使用记录的分页切片（最新的在前）。

        ``offset`` 从最新一条算起；``limit`` 与 ``offset`` 都会被钳制为非负整数。
        """
        with self._lock:
            items = list(self._history)
        items.reverse()
        limit = max(0, int(limit))
        offset = max(0, int(offset))
        return items[offset : offset + limit]

    def history_count(self) -> int:
        """当前保留的调用记录总数（缓冲上限内）。"""
        with self._lock:
            return len(self._history)

    # ------------------------------------------------------------------ 认证

    def authenticate(self, supplied: Optional[str]) -> Optional[SubKey]:
        """校验调用方提供的凭据是否为某个已启用的用户 Key。

        返回命中的用户 Key；未命中或已停用时返回 None（绝不返回停用 Key）。
        """
        if not supplied:
            return None
        with self._lock:
            for key in self._keys:
                if key.enabled and hmac.compare_digest(supplied, key.key):
                    return key
        return None

    # ------------------------------------------------------------------ 用量统计

    def record(
        self,
        sub: SubKey,
        *,
        ok: bool = True,
        model: Optional[str] = None,
        stream: bool = False,
        usage: Optional[Tuple[int, int, int]] = None,
        estimated: Tuple[int, int] = (0, 0),
        latency_ms: Optional[float] = None,
    ) -> None:
        """记录一次用户 Key 调用。

        ``usage`` 为上游返回的 (prompt_tokens, completion_tokens, cached_tokens)；
        缺失时用 ``estimated``（(估计输入 token, 估计输出 token)）兜底，
        历史记录里以 ``estimated`` 标记区分。
        """
        if usage is None:
            prompt, completion, cached = 0, 0, 0
            est_prompt = int(estimated[0] or 0)
            est_completion = int(estimated[1] or 0)
        else:
            prompt = int(usage[0] or 0)
            completion = int(usage[1] or 0)
            cached = int(usage[2] or 0)
            est_prompt = est_completion = 0
        now = self._clock()
        with self._lock:
            u = sub.usage
            u["requests"] = u.get("requests", 0) + 1
            if ok:
                u["successes"] = u.get("successes", 0) + 1
            else:
                u["errors"] = u.get("errors", 0) + 1
            u["prompt_tokens"] = u.get("prompt_tokens", 0) + prompt
            u["completion_tokens"] = u.get("completion_tokens", 0) + completion
            u["cached_tokens"] = u.get("cached_tokens", 0) + cached
            u["estimated_prompt_tokens"] = u.get("estimated_prompt_tokens", 0) + est_prompt
            u["estimated_completion_tokens"] = u.get("estimated_completion_tokens", 0) + est_completion
            u["last_used_at"] = now
            self._history.append(
                {
                    "t": now,
                    "name": sub.name,
                    "model": model,
                    "stream": bool(stream),
                    "ok": bool(ok),
                    "prompt": est_prompt if usage is None else prompt,
                    "completion": est_completion if usage is None else completion,
                    "cached": cached,
                    "estimated": usage is None,
                    "latency_ms": latency_ms,
                }
            )
            self._dirty = True

    # ------------------------------------------------------------------ 管理操作

    def create(self, name: str, note: str = "") -> SubKey:
        """创建用户 Key，返回完整密钥（仅此一次可见，请调用方立即展示给管理员）。"""
        name = (name or "").strip()
        if not name:
            raise ValueError("名称不能为空")
        if len(name) > 64:
            raise ValueError("名称最长 64 个字符")
        with self._lock:
            if any(key.name == name for key in self._keys):
                raise ValueError(f"已存在同名用户 Key：{name}")
            sub = SubKey(
                name=name,
                key="sk-sr-" + secrets.token_urlsafe(24),
                created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                note=(note or "").strip(),
                usage=dict(_EMPTY_USAGE),
            )
            self._keys.append(sub)
            self._dirty = True
            return sub

    def update(self, identifier: str, *, name: Optional[str] = None, note: Optional[str] = None) -> SubKey:
        sub = self._require(identifier)
        with self._lock:
            if name is not None:
                name = (name or "").strip()
                if not name:
                    raise ValueError("名称不能为空")
                if len(name) > 64:
                    raise ValueError("名称最长 64 个字符")
                if any(key.name == name and key is not sub for key in self._keys):
                    raise ValueError(f"已存在同名用户 Key：{name}")
                sub.name = name
            if note is not None:
                sub.note = (note or "").strip()
            self._dirty = True
            return sub

    def set_enabled(self, identifier: str, enabled: bool) -> SubKey:
        """启用 / 停用用户 Key。停用后该 Key 立即无法通过认证。"""
        sub = self._require(identifier)
        with self._lock:
            sub.enabled = bool(enabled)
            self._dirty = True
            return sub

    def revoke(self, identifier: str) -> SubKey:
        """删除用户 Key（撤销后该 Key 永久失效，历史记录保留）。"""
        sub = self._require(identifier)
        with self._lock:
            self._keys.remove(sub)
            self._dirty = True
            return sub

    def reset_usage(self, identifier: str) -> SubKey:
        """清零该用户 Key 的用量统计（不影响历史记录）。"""
        sub = self._require(identifier)
        with self._lock:
            sub.usage = dict(_EMPTY_USAGE)
            self._dirty = True
            return sub

    def _require(self, identifier: str) -> SubKey:
        sub = self.find(identifier)
        if sub is None:
            raise KeyError("找不到指定的用户 Key")
        return sub
