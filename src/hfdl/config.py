"""配置：TOML 文件 + 环境变量覆盖。

设计原则
--------
1. **一切可配** —— 端点、代理绕过、并发、切片阈值都不写死。
2. **令牌不入配置** —— 令牌只从环境变量读（默认 ``HF_TOKEN``），
   避免密钥落进被提交的配置文件。
3. **配置层单一** —— CLI 旗标 > 环境变量 > TOML 文件 > 默认值。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

__all__ = ["Config", "ConfigError", "load_config", "DEFAULT_ENDPOINT", "DEFAULT_FALLBACK"]

DEFAULT_ENDPOINT = "https://hf-mirror.com"
DEFAULT_FALLBACK = "https://huggingface.co"

#: 载荷 CDN 域应当绕开公司代理直连（实测直连比走代理快 25~70 倍）。
#: 匹配规则同 curl：``hf.co`` 命中其所有子域，但**不**命中 ``huggingface.co``。
DEFAULT_NO_PROXY = ("hf.co", "cdn-lfs.huggingface.co")

#: 允许接收 Authorization 头的主机。第三方镜像绝不在此列 ——
#: 把令牌发给镜像等于泄露给镜像运营方，而且镜像也不认这个头。
DEFAULT_TOKEN_HOSTS = ("huggingface.co",)


class ConfigError(Exception):
    """配置不合法。"""


@dataclass
class Config:
    """一次运行所需的全部参数。"""

    # --- 端点与鉴权 ---
    endpoint: str = DEFAULT_ENDPOINT
    fallback_endpoint: str = DEFAULT_FALLBACK
    token_env: str = "HF_TOKEN"
    token_hosts: tuple[str, ...] = DEFAULT_TOKEN_HOSTS

    # --- 清单与落盘 ---
    manifest: str = "manifest.json"
    root: str = ""

    # --- 并发模型 ---
    #: 同时在跑的传输连接数。HF 的 CDN 对**每条连接**限速，
    #: 所以真正的杠杆是连接数，不是"文件数"。
    workers: int = 24
    #: 任务线程数（传输 + 校验的总并发上限）。
    #: 校验必须跑在传输槽**之外**，否则算哈希会把下载槽占满，
    #: 端到端速率会塌掉（实测 75 MB/s -> 23.7 MB/s）。
    tasks: int = 32

    # --- 切片并行 ---
    #: 大于等于该字节数的文件走切片并行。
    chunk_threshold: int = 1_000_000_000
    #: 每片字节数。
    chunk_size: int = 400_000_000

    # --- 传输细节 ---
    retries: int = 4
    connect_timeout: int = 20
    max_time: int = 3600
    speed_limit: int = 20_000
    speed_time: int = 45
    #: 优先用外部 curl（实测比纯 Python 快），不可用时自动回落纯 Python 实现。
    use_curl: bool = True

    # --- 代理绕过 ---
    no_proxy: tuple[str, ...] = DEFAULT_NO_PROXY

    # --- 筛选语义 ---
    #: ``pathseg`` = ``*`` 不跨目录分隔符（推荐）；``fnmatch`` = 传统语义，``*`` 跨 ``/``。
    glob_style: str = "pathseg"

    # --- 运行期 ---
    verbose: bool = False
    quiet: bool = False

    # ------------------------------------------------------------------ #

    @property
    def token(self) -> str:
        """从环境变量读取令牌。**不**从配置文件读。"""
        for name in (self.token_env, "HUGGING_FACE_HUB_TOKEN"):
            val = (os.environ.get(name) or "").strip()
            if val:
                return val
        return ""

    def has_token(self) -> bool:
        return bool(self.token)

    def to_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    # ------------------------------------------------------------------ #

    def apply_no_proxy(self) -> list[str]:
        """把 CDN 域**合并**进 ``NO_PROXY``。

        ⚠ 必须合并而不是覆盖 —— 用户通常已经配了 localhost / 内网地址绕代理，
        覆盖掉会连带让内网流量走进代理。

        返回最终生效的条目列表。
        """
        existing = [x.strip() for x in os.environ.get("NO_PROXY", "").split(",") if x.strip()]
        merged = list(dict.fromkeys(existing + [d for d in self.no_proxy if d]))
        os.environ["NO_PROXY"] = ",".join(merged)
        os.environ["no_proxy"] = os.environ["NO_PROXY"]
        return merged


# --------------------------------------------------------------------------- #
# 载入
# --------------------------------------------------------------------------- #

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _coerce(name: str, value: Any) -> Any:
    """把 TOML / 环境变量里的值转成字段声明的类型。"""
    if not isinstance(value, str):
        return value
    if isinstance(value, bool):
        return value
    low = value.strip().lower()
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    if isinstance(value, str):
        return value
    return value


def _parse_scalar(raw: str, kind: str) -> Any:
    if kind == "bool":
        return raw.strip().lower() in _TRUE
    if kind == "int":
        try:
            return int(raw)
        except ValueError as exc:
            raise ConfigError(f"期望整数，得到 {raw!r}") from exc
    if kind == "tuple[str, ...]":
        return tuple(x.strip() for x in raw.split(",") if x.strip())
    return raw


def _apply_mapping(cfg: Config, data: dict[str, Any], source: str) -> None:
    known = {f.name: f for f in fields(cfg)}
    for key, raw in data.items():
        key = key.replace("-", "_")
        if key not in known:
            raise ConfigError(f"{source}: 未知配置项 {key!r}")
        f = known[key]
        current = getattr(cfg, key)
        if isinstance(current, tuple):
            if isinstance(raw, str):
                raw = tuple(x.strip() for x in raw.split(",") if x.strip())
            elif isinstance(raw, list):
                raw = tuple(str(x) for x in raw)
            else:
                raise ConfigError(f"{source}: {key} 需要字符串或列表")
            if f.type and "tuple[str, ...]" in str(f.type):
                setattr(cfg, key, raw)
                continue
            setattr(cfg, key, raw)
            continue
        if isinstance(current, bool):
            if isinstance(raw, str):
                raw = raw.strip().lower() in _TRUE
            setattr(cfg, key, bool(raw))
            continue
        if isinstance(current, int):
            try:
                setattr(cfg, key, int(raw))
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"{source}: {key} 需要整数，得到 {raw!r}") from exc
            continue
        setattr(cfg, key, str(raw))


def load_config(
    path: str | os.PathLike[str] | None = None,
    *,
    env_prefix: str = "HFDL_",
    overrides: dict[str, Any] | None = None,
) -> Config:
    """按 ``CLI > 环境变量 > TOML > 默认值`` 的顺序合成配置。

    环境变量命名：``HFDL_<字段名大写>``，例如 ``HFDL_WORKERS=32``、
    ``HFDL_CHUNK_SIZE=800000000``。``HFDL_NO_PROXY`` 用逗号分隔多个域。
    """
    cfg = Config()

    # 1) TOML 文件
    if path:
        p = Path(path)
        if p.is_file():
            try:
                with p.open("rb") as fh:
                    data = tomllib.load(fh)
            except tomllib.TOMLDecodeError as exc:
                raise ConfigError(f"{p}: TOML 解析失败: {exc}") from exc
            section = data.get("hfdl", data)
            if not isinstance(section, dict):
                raise ConfigError(f"{p}: [hfdl] 段必须是表")
            _apply_mapping(cfg, section, str(p))
        elif p.exists():
            raise ConfigError(f"{p}: 不是文件")

    # 2) 环境变量
    known = {f.name for f in fields(cfg)}
    env_data: dict[str, Any] = {}
    for key, raw in os.environ.items():
        if not key.startswith(env_prefix):
            continue
        name = key[len(env_prefix):].lower()
        if name in known:
            env_data[name] = raw
    if env_data:
        _apply_mapping(cfg, env_data, f"env {env_prefix}*")

    # 3) 兼容旧变量名
    legacy = os.environ.get("HF_ENDPOINT")
    if legacy and not env_data.get("endpoint"):
        cfg.endpoint = legacy.rstrip("/")

    # 4) 显式覆盖（CLI 旗标）
    if overrides:
        clean = {k.replace("-", "_"): v for k, v in overrides.items() if v is not None}
        _apply_mapping(cfg, clean, "cli")

    if not cfg.endpoint:
        raise ConfigError("endpoint 不能为空")
    cfg.endpoint = cfg.endpoint.rstrip("/")
    if cfg.fallback_endpoint:
        cfg.fallback_endpoint = cfg.fallback_endpoint.rstrip("/")
    if cfg.workers < 1:
        raise ConfigError("workers 必须 >= 1")
    if cfg.tasks < cfg.workers:
        # 校验线程必须跑在传输槽之外，否则会挤占下载并发
        cfg.tasks = cfg.workers
    if cfg.chunk_size < 1:
        raise ConfigError("chunk_size 必须 >= 1")
    if cfg.glob_style not in ("pathseg", "fnmatch"):
        raise ConfigError(f"glob_style 只能是 pathseg / fnmatch，得到 {cfg.glob_style!r}")
    return cfg
