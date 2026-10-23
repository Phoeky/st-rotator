"""配置加载与校验。

配置既可以写成 JSON 文件，也可以在代码里用 ``Config.from_dict`` 直接构造。
Key 支持 ``${ENV_VAR}`` / ``${ENV_VAR:-默认值}`` 占位符，避免把密钥写进仓库。
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .errors import ConfigError

# 支持的调度策略
STRATEGIES = ("round_robin", "least_inflight", "least_recent", "weighted")

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env(value: str) -> str:
    """展开 ``${VAR}`` 与 ``${VAR:-默认值}``；未定义且无默认值时抛 ConfigError。"""

    def _sub(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        current = os.environ.get(name)
        if current:
            return current
        if default is not None:
            return default
        raise ConfigError(f"环境变量 {name} 未设置，且未提供默认值")

    return _ENV_PATTERN.sub(_sub, value)


def next_account_name(existing: Iterable[str]) -> str:
    """给一个不与现有账号重名的默认账号名（``账号1``、``账号2``……）。

    命名约定：从「现有账号数 + 1」起算，若该名字已被占用则继续往后找。
    所以现有 ``[账号1, 账号3]`` 会得到 ``账号4``（而不是补空位的 ``账号2``）——
    保持"追加在末尾"的直觉，同时避免和已存在的名字撞车。

    ⚠️ 批量加 Key 时必须**先算一次名字、整批复用**。如果每把 Key 都现算一次，
    它们会各自拿到一个新名字、被拆成 N 个账号，于是账号级配额聚合与 429 联动冷却
    全部失效（每把 Key 独占一份配额，同账号雪崩照样发生）。
    """
    names = {str(name) for name in existing}
    index = len(names) + 1
    while f"账号{index}" in names:
        index += 1
    return f"账号{index}"


@dataclass
class RateControlConfig:
    """主动限速策略。

    Attributes:
        mode: ``off`` 不限速 / ``fixed`` 固定速率 / ``adaptive`` AIMD 自适应。
        qps: fixed 的目标速率；adaptive 的初始速率。
        min_qps / max_qps: adaptive 的速率上下界。
        decrease: adaptive 撞 429 时的乘性衰减系数。
        increase_step: adaptive 恢复期的加性增量。
        recovery_seconds: 距离上次 429 多久才允许提速。
    """

    mode: str = "off"
    qps: float = 0.0
    min_qps: float = 0.15
    max_qps: float = 5.0
    decrease: float = 0.85
    increase_step: float = 0.05
    recovery_seconds: float = 8.0

    MODES = ("off", "fixed", "adaptive")

    def __post_init__(self) -> None:
        if self.mode not in self.MODES:
            raise ConfigError(f"rate_control.mode 必须是 {self.MODES} 之一，当前为 {self.mode!r}")
        if self.qps < 0:
            raise ConfigError("rate_control.qps 不能为负")
        if self.mode != "off" and self.qps <= 0:
            raise ConfigError(f"rate_control.mode={self.mode} 时 qps 必须 > 0")
        if self.mode == "adaptive":
            if self.min_qps <= 0:
                raise ConfigError("rate_control.min_qps 必须 > 0")
            if self.max_qps < self.min_qps:
                raise ConfigError("rate_control.max_qps 不能小于 min_qps")
            if not 0 < self.decrease < 1:
                raise ConfigError("rate_control.decrease 必须落在 (0, 1) 区间")
            if self.increase_step < 0 or self.recovery_seconds < 0:
                raise ConfigError("rate_control.increase_step / recovery_seconds 不能为负")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "RateControlConfig":
        data = dict(data or {})
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise ConfigError(f"rate_control 存在未知字段: {sorted(unknown)}")
        return cls(**data)


@dataclass
class CooldownConfig:
    """冷却策略参数。

    Attributes:
        base: 首次 429 的冷却秒数。**应 ≈ 上游的限流恢复窗口**——
            填小了会让已经撞墙的 Key 在几秒后被反复重试，全是白打的请求
            （实测商汤的窗口是 60s，用 3s 会把重试放大 3 倍以上）。
        factor: 连续 429 时冷却的指数放大系数。
        max: 单次冷却上限秒数（防止退避到不可用）。
        jitter: 抖动比例，取 [0, jitter] 的随机比例叠加，避免多进程同时苏醒。
        invalid_ttl: Key 被判失效后的复活探测间隔；<=0 表示永久失效。
        server_error: 5xx / 网络超时的短冷却秒数（不归咎于 Key，不累计退避）。
        model_unavailable: "模型不在套餐 / 套餐额度耗尽"的冷却秒数。
            这类错误不是凭据失效，但立刻重试同一把 Key 必然再失败（白打），
            给一个中短冷却让调度跳过它、到期自动复探。0 表示不冷却（旧行为）。
    """

    base: float = 60.0
    factor: float = 1.5
    max: float = 120.0
    jitter: float = 0.5
    invalid_ttl: float = 600.0
    server_error: float = 2.0
    model_unavailable: float = 300.0

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "CooldownConfig":
        data = dict(data or {})
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise ConfigError(f"cooldown 存在未知字段: {sorted(unknown)}")
        cfg = cls(**data)
        if cfg.base <= 0:
            raise ConfigError("cooldown.base 必须 > 0")
        if cfg.factor < 1:
            raise ConfigError("cooldown.factor 必须 >= 1")
        if cfg.max < cfg.base:
            raise ConfigError("cooldown.max 不能小于 cooldown.base")
        return cfg

    def for_attempt(self, attempt: int, rng: Any = None) -> float:
        """按第 N 次连续 429 计算冷却秒数（含抖动）。"""
        attempt = max(1, int(attempt))
        delay = min(self.base * (self.factor ** (attempt - 1)), self.max)
        if self.jitter and rng is not None:
            delay += rng.uniform(0.0, delay * self.jitter)
        return delay


@dataclass
class FlashLiteExchangeConfig:
    """智能一换一：套餐耗尽时烧推广池积分换通用池额度。

    有的上游（如商汤日日新）一个账号有两套积分池：通用池 + 推广期模型的
    独立池（如 sensenova-6.8-flash-lite），官方活动期间烧 1 积分推广池
    可换 1 积分通用池。开启后：某账号因“套餐额度耗尽”进入冷却的那一刻，
    网关自动用该账号的 Key 并发调用推广池模型（长文 + 大图），推动额度
    按活动规则回补，而不是干等恢复窗口。

    Attributes:
        enabled: 总开关。
        model: 推广池模型名。
        concurrency: 每轮并发烧点请求数。
        requests_per_trigger: 一次触发会话（冷却窗口内）的总请求数上限；
            触发后会持续烧到通用池回补 / 冷却到期 / 到上限为止。
        min_interval_s: 同一账号两次触发之间的最小间隔（防止连续触发螺旋）。
        long_text_max_tokens: 长文请求的 max_tokens。
        long_text_prompt: 长文请求使用的提示词。
        image_enabled: 是否交替发送带图的请求（视觉输入烧得更多）。
        image_size: 生成的噪声大图边长（像素，纯标准库即时生成）。
        multi_image_count: 每个带图请求附带几张图片（1~9）。
        max_workers: 全局最多同时进行的烧点任务数（账号级任务，不是请求）。
        yield_to_serve: 服务优先——有用户请求在途时烧点主动让路，避免拖慢首字延迟。
        min_available_mb: 可用内存低于此值（MB）时暂停/跳过烧点；0 = 不检查。
    """

    enabled: bool = False
    model: str = "sensenova-6.8-flash-lite"
    concurrency: int = 24
    requests_per_trigger: int = 32
    min_interval_s: float = 300.0
    long_text_max_tokens: int = 4096
    long_text_prompt: str = (
        "请写一篇结构完整、细节丰富的深度综述文章，题目为《人工智能基础设施的演进："
        "从单机推理到全球调度》，包含引言、三个主体章节和总结，不少于1500字。"
    )
    image_enabled: bool = True
    image_size: int = 512
    multi_image_count: int = 3
    max_workers: int = 8
    yield_to_serve: bool = True
    min_available_mb: float = 300.0

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "FlashLiteExchangeConfig":
        data = dict(data or {})
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise ConfigError(f"flash_lite_exchange 存在未知字段: {sorted(unknown)}")
        cfg = cls(**data)
        if not (1 <= cfg.concurrency <= 64):
            raise ConfigError("flash_lite_exchange.concurrency 需在 1~64 之间")
        if not (1 <= cfg.requests_per_trigger <= 2048):
            raise ConfigError("flash_lite_exchange.requests_per_trigger 需在 1~2048 之间")
        if cfg.min_interval_s < 0:
            raise ConfigError("flash_lite_exchange.min_interval_s 不能为负")
        if cfg.long_text_max_tokens < 16:
            raise ConfigError("flash_lite_exchange.long_text_max_tokens 太小")
        if not (16 <= cfg.image_size <= 4096):
            raise ConfigError("flash_lite_exchange.image_size 需在 16~4096 之间")
        if not (1 <= cfg.multi_image_count <= 9):
            raise ConfigError("flash_lite_exchange.multi_image_count 需在 1~9 之间")
        if cfg.max_workers < 1:
            raise ConfigError("flash_lite_exchange.max_workers 必须 >= 1")
        return cfg


@dataclass
class ModelFilterConfig:
    """模型清单过滤：把"几乎不可用"的模型从清单里藏掉。

    有些上游会在 ``/v1/models`` 里列出实际几乎跑不通的模型（实测商汤的
    ``deepseek-v4.1-flash`` 就经常不可用）。把它们配进 ``hidden`` 后，
    **控制台的选择器和网关透传的 ``/v1/models`` 都不会再列出它们**，
    省得自己或上层 agent 误选。

    Attributes:
        hidden: 要隐藏的模型名列表，支持 ``*`` 通配（如 ``deepseek-v4.1-*``）。
            只影响"清单展示"，**不拦截显式请求**——上层点名要这个模型时，
            网关照常转发给上游（该报什么错就报什么错），不做额外限制。
    """

    hidden: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "ModelFilterConfig":
        if data is None:
            return cls()
        if not isinstance(data, Mapping):
            raise ConfigError("model_filter 必须是对象")
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise ConfigError(f"model_filter 存在未知字段: {sorted(unknown)}")
        raw = data.get("hidden") or []
        if isinstance(raw, str):
            raise ConfigError("model_filter.hidden 必须是数组，例如 [\"deepseek-v4.1-flash\"]")
        if not isinstance(raw, (list, tuple)):
            raise ConfigError("model_filter.hidden 必须是数组")
        hidden: list[str] = []
        for item in raw:
            if not isinstance(item, str):
                raise ConfigError(f"model_filter.hidden 只接受字符串，收到 {item!r}")
            name = item.strip()
            if name and name not in hidden:  # 去重但保持书写顺序
                hidden.append(name)
        return cls(hidden=hidden)

    def is_hidden(self, model_id: str) -> bool:
        """模型是否命中隐藏规则（精确匹配或 ``*`` 通配）。"""
        name = (model_id or "").strip()
        if not name:
            return False
        return any(fnmatch.fnmatchcase(name, pattern) for pattern in self.hidden)


@dataclass
class AccountConfig:
    """一个账号（可含多把 Key）。

    ``rpm_limit`` 是**账号级**配额：同一账号下的多把 Key 共享同一个 60s 滑动窗口
    （见 ``keypool.AccountState``），撞到上限时整个账号一起停，避免同账号的多把 Key
    连环送死。所以这里填的是**账号总配额**，不要再除以 Key 数量——除以 Key 数量是
    旧的"每把 Key 各自一个窗口"语义，会让闸门被收窄 N 倍。留空表示不做本地 RPM 限制，
    完全依赖上游 429 反馈。
    """

    name: str
    api_keys: list[str] = field(default_factory=list)
    rpm_limit: int | None = None
    max_concurrency: int = 4
    weight: float = 1.0
    tags: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.name:
            raise ConfigError("account.name 不能为空")
        # 允许空 api_keys：首次运行的零 Key 配置要能直接启动，
        # 空池请求会在 KeyPool.acquire 抛出友好错误（"Key 池为空"）。
        if self.rpm_limit is not None and self.rpm_limit <= 0:
            raise ConfigError(f"账号 {self.name} 的 rpm_limit 必须 > 0 或留空")
        if self.max_concurrency <= 0:
            raise ConfigError(f"账号 {self.name} 的 max_concurrency 必须 > 0")
        if self.weight <= 0:
            raise ConfigError(f"账号 {self.name} 的 weight 必须 > 0")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AccountConfig":
        data = dict(data)
        keys = data.pop("api_keys", None)
        if keys is None:
            keys = data.pop("keys", None)  # 兼容简写
        if isinstance(keys, str):
            keys = [keys]
        name = data.pop("name", None) or "default"
        known = {"rpm_limit", "max_concurrency", "weight", "tags"}
        unknown = set(data) - known
        if unknown:
            raise ConfigError(f"账号 {name} 存在未知字段: {sorted(unknown)}")
        keys = [expand_env(str(k)).strip() for k in (keys or []) if str(k).strip()]
        return cls(name=name, api_keys=keys, **data)


@dataclass
class Config:
    """整体配置。"""

    base_url: str = "https://token.sensenova.cn/v1"
    # 必须是上游 /v1/models 里真实存在的模型名。写错会在运行时报 404 model is not found，
    # 而且因为是流式（已经发过 200），错误只能以 SSE error 事件的形式出现，不好排查。
    # 以 GET /v1/models 的返回为准，或用控制台切换。
    default_model: str = "deepseek-v4-flash"
    accounts: list[AccountConfig] = field(default_factory=list)

    # 重试与超时
    max_attempts: int = 5
    timeout: float = 60.0
    connect_timeout: float = 10.0
    acquire_timeout: float = 120.0
    # 取 Key 的快速失败阈值：最快可用时刻若超过此秒数，立即返回 429 而不是干等。
    # 额度耗尽时所有 Key 会冷却数分钟，干等会把首字延迟拖到分钟级。
    acquire_fail_fast: float = 15.0
    max_total_wait: float = 90.0
    retry_backoff: float = 0.5
    max_retry_backoff: float = 8.0

    # 主动限流
    rate_control: RateControlConfig = field(default_factory=RateControlConfig)

    # 调度
    strategy: str = "round_robin"
    cooldown: CooldownConfig = field(default_factory=CooldownConfig)

    # 智能一换一（套餐耗尽时烧推广池换通用池）
    flash_lite_exchange: FlashLiteExchangeConfig = field(default_factory=FlashLiteExchangeConfig)

    # 模型清单过滤（把几乎不可用的模型从清单里藏掉）
    model_filter: ModelFilterConfig = field(default_factory=ModelFilterConfig)

    # 连接池
    max_connections: int = 100
    max_keepalive: int = 20

    extra_headers: dict[str, str] = field(default_factory=dict)

    # ---------------------------------------------------------------- 校验

    def __post_init__(self) -> None:
        # 允许 accounts 为空：首次运行的零 Key 配置要能直接启动，
        # 空池请求会在 KeyPool.acquire 抛出友好错误（"Key 池为空"）。
        if self.strategy not in STRATEGIES:
            raise ConfigError(f"strategy 必须是 {STRATEGIES} 之一，当前为 {self.strategy!r}")
        if self.max_attempts < 1:
            raise ConfigError("max_attempts 必须 >= 1")
        if self.timeout <= 0 or self.connect_timeout <= 0:
            raise ConfigError("timeout / connect_timeout 必须 > 0")
        if self.max_total_wait < 0:
            raise ConfigError("max_total_wait 不能为负")
        if self.acquire_fail_fast < 0:
            raise ConfigError("acquire_fail_fast 不能为负")
        if not self.base_url.startswith(("http://", "https://")):
            raise ConfigError(f"base_url 必须以 http(s):// 开头，当前为 {self.base_url!r}")
        self.base_url = self.base_url.rstrip("/")

    @property
    def total_keys(self) -> int:
        return sum(len(a.api_keys) for a in self.accounts)

    # ---------------------------------------------------------------- 构造

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Config":
        data = dict(data)
        if "accounts" not in data:
            raise ConfigError("配置缺少 accounts 字段")
        raw_accounts = data.pop("accounts") or []  # 空列表合法：首次运行的零 Key 配置
        cooldown = CooldownConfig.from_dict(data.pop("cooldown", None))
        rate_control = RateControlConfig.from_dict(data.pop("rate_control", None))
        flash_lite = FlashLiteExchangeConfig.from_dict(data.pop("flash_lite_exchange", None))
        model_filter = ModelFilterConfig.from_dict(data.pop("model_filter", None))
        headers = {str(k): expand_env(str(v)) for k, v in (data.pop("extra_headers", None) or {}).items()}
        known = set(cls.__dataclass_fields__) - {
            "accounts", "cooldown", "rate_control", "extra_headers",
            "flash_lite_exchange", "model_filter",
        }
        unknown = set(data) - known
        if unknown:
            raise ConfigError(f"配置存在未知字段: {sorted(unknown)}")
        accounts = [AccountConfig.from_dict(a) for a in raw_accounts]
        return cls(
            accounts=accounts,
            cooldown=cooldown,
            rate_control=rate_control,
            flash_lite_exchange=flash_lite,
            model_filter=model_filter,
            extra_headers=headers,
            **{k: expand_env(v) if isinstance(v, str) else v for k, v in data.items()},
        )

    @classmethod
    def from_file(cls, path: str | os.PathLike[str]) -> "Config":
        p = Path(path)
        if not p.is_file():
            raise ConfigError(f"配置文件不存在: {p}")
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"配置文件不是合法 JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError("配置文件根节点必须是对象")
        return cls.from_dict(raw)

    def to_dict(self, *, mask_keys: bool = True) -> dict[str, Any]:
        """导出为可序列化字典（默认脱敏，便于打日志）。"""
        from .keypool import mask_key

        return {
            "base_url": self.base_url,
            "default_model": self.default_model,
            "strategy": self.strategy,
            "max_attempts": self.max_attempts,
            "max_total_wait": self.max_total_wait,
            "rate_control": {
                "mode": self.rate_control.mode,
                "qps": self.rate_control.qps,
            },
            "accounts": [
                {
                    "name": a.name,
                    "api_keys": [mask_key(k) for k in a.api_keys] if mask_keys else list(a.api_keys),
                    "rpm_limit": a.rpm_limit,
                    "max_concurrency": a.max_concurrency,
                    "weight": a.weight,
                }
                for a in self.accounts
            ],
        }


# 可被 UI 直接改写的标量配置项（带类型转换）
_SCALAR_FIELDS: dict[str, type] = {
    "max_attempts": int,
    "max_total_wait": float,
    "timeout": float,
    "connect_timeout": float,
    "acquire_timeout": float,
    "acquire_fail_fast": float,
    "retry_backoff": float,
    "max_retry_backoff": float,
    "max_connections": int,
    "max_keepalive": int,
}


class ConfigStore:
    """配置文件读写器：保留原始结构，只改动用户真正改过的地方。

    为什么需要它
    ------------
    最直觉的做法是「内存 Config → 序列化 → 覆盖写文件」。但这会踩一个坑：
    配置文件里的 Key 常常写成 ``${SENSENOVA_KEY_1}`` 占位符。内存里的 Config 是
    **展开后**的值，全量重写就会把占位符替换成明文密钥落盘——本来是为了不把密钥
    写进文件才用的占位符，结果被工具自己写进去了。

    所以这里保留从磁盘读到的**原始 dict**，只做定点修改（加一把 Key、换一个模型…），
    其他字段（包括占位符、自定义字段顺序、无关配置）原样保留。

    写入采用「临时文件 + 原子替换」，避免写一半被中断导致配置文件损坏。
    """

    def __init__(self, path: str | os.PathLike[str], raw: dict[str, Any], config: Config) -> None:
        self.path = Path(path)
        self._raw = raw
        self.config = config

    # ------------------------------------------------------------ 读写

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "ConfigStore":
        p = Path(path)
        if not p.is_file():
            raise ConfigError(f"配置文件不存在: {p}")
        try:
            loaded = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"配置文件不是合法 JSON: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ConfigError("配置文件根节点必须是对象")
        return cls(p, loaded, Config.from_dict(loaded))

    def save(self) -> None:
        """原子落盘（先写临时文件再替换，避免写坏原文件）。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        text = json.dumps(self._raw, ensure_ascii=False, indent=2) + "\n"
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.path)

    def _accounts_raw(self) -> list[dict[str, Any]]:
        accounts = self._raw.get("accounts")
        if not isinstance(accounts, list):
            accounts = []
            self._raw["accounts"] = accounts
        return accounts

    @staticmethod
    def _matches(stored: Any, target: str) -> bool:
        """比较配置里存的 Key 与目标 Key，兼容 ``${ENV}`` 占位符写法。"""
        if not isinstance(stored, str):
            return False
        if stored == target:
            return True
        try:
            return expand_env(stored) == target
        except ConfigError:
            return False

    # ------------------------------------------------------------ 定点修改

    def add_key(
        self,
        key: str,
        account: str | None = None,
        *,
        rpm_limit: int | None = None,
        max_concurrency: int = 4,
        weight: float = 1.0,
    ) -> dict[str, Any]:
        """把一把 Key 写进配置文件；同名账号则追加，否则新建账号。"""
        key = (key or "").strip()
        if not key:
            raise ConfigError("api_key 不能为空")
        accounts = self._accounts_raw()
        name = account or next_account_name(
            item.get("name") for item in accounts if isinstance(item, dict)
        )
        for item in accounts:
            if isinstance(item, dict) and item.get("name") == name:
                keys = item.get("api_keys")
                if not isinstance(keys, list):
                    keys = []
                    item["api_keys"] = keys
                if any(self._matches(k, key) for k in keys):
                    raise ConfigError(f"账号 {name} 下已存在该 Key")
                keys.append(key)
                return item
        entry = {
            "name": name,
            "api_keys": [key],
            "rpm_limit": rpm_limit,
            "max_concurrency": max_concurrency,
            "weight": weight,
        }
        accounts.append(entry)
        return entry

    def account_names(self) -> list[str]:
        """当前配置里的账号名（按出现顺序）。"""
        return [
            str(item["name"])
            for item in self._accounts_raw()
            if isinstance(item, dict) and item.get("name")
        ]

    def remove_key(self, key: str) -> bool:
        """从配置文件里删掉一把 Key；账号空了就一并删掉该账号。"""
        accounts = self._accounts_raw()
        removed = False
        for item in list(accounts):
            if not isinstance(item, dict):
                continue
            keys = item.get("api_keys")
            if not isinstance(keys, list):
                continue
            kept = [k for k in keys if not self._matches(k, key)]
            if len(kept) != len(keys):
                removed = True
                item["api_keys"] = kept
                if not kept:
                    accounts.remove(item)
        return removed

    def set_default_model(self, model: str) -> str:
        model = (model or "").strip()
        if not model:
            raise ConfigError("模型名不能为空")
        self._raw["default_model"] = model
        return model

    def set_strategy(self, strategy: str) -> str:
        if strategy not in STRATEGIES:
            raise ConfigError(f"strategy 必须是 {STRATEGIES} 之一，当前为 {strategy!r}")
        self._raw["strategy"] = strategy
        return strategy

    def set_rate_control(self, **changes: Any) -> dict[str, Any]:
        """只把用户真正改动的字段写回文件，其余保持原样。"""
        cleaned = {k: v for k, v in changes.items() if v is not None}
        unknown = set(cleaned) - set(RateControlConfig.__dataclass_fields__)
        if unknown:
            raise ConfigError(f"未知的限速参数: {sorted(unknown)}")
        if not cleaned:
            return dict(self._raw.get("rate_control") or {})
        section = self._raw.get("rate_control")
        if not isinstance(section, dict):
            section = {}
            self._raw["rate_control"] = section
        section.update(cleaned)
        return dict(section)

    def set_scalar(self, field: str, value: Any) -> Any:
        """改写一个标量配置项（max_attempts / max_total_wait / timeout …）。"""
        if field not in _SCALAR_FIELDS:
            raise ConfigError(f"不支持的配置项: {field}")
        try:
            converted = _SCALAR_FIELDS[field](value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{field} 的值不合法: {value!r}") from exc
        self._raw[field] = converted
        return converted

    def set_section(self, name: str, values: Mapping[str, Any]) -> dict[str, Any]:
        """定点更新一个嵌套配置节（如 flash_lite_exchange），其余字段原样保留。"""
        if not isinstance(values, Mapping) or not values:
            raise ConfigError("set_section 需要非空的字段映射")
        section = self._raw.get(name)
        if not isinstance(section, dict):
            section = {}
            self._raw[name] = section
        section.update(dict(values))
        return dict(section)

    # ------------------------------------------------------------ 重载

    def reload(self) -> Config:
        """从当前 raw 重新构造 Config（用于校验改动是否合法）。"""
        self.config = Config.from_dict(self._raw)
        return self.config
