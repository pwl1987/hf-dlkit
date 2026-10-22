"""Hugging Face API 客户端（纯标准库，零第三方依赖）。

只依赖一次 API 调用就能同时拿到**文件列表、字节数、权威 sha256**：

    GET {endpoint}/api/models/{repo}/revision/{revision}?blobs=true
        -> siblings[].rfilename / siblings[].size / siblings[].lfs.sha256

这样既不需要 ``huggingface_hub``（它只被用来拿元数据，却带来了 1.x/2.x
的一堆版本语义坑），也省掉一半的 API 往返。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .endpoints import Endpoint, EndpointPolicy

__all__ = [
    "FileMeta",
    "HubClient",
    "HubError",
    "AuthError",
    "NotFoundError",
    "EndpointUnavailable",
]

_USER_AGENT = "hf-dlkit/0.1.0 (+https://github.com/pwl1987/hf-dlkit)"


class HubError(Exception):
    """API 调用失败。"""


class AuthError(HubError):
    """仓库被 gated（需令牌 + 站点接受条款），或令牌无效。HTTP 401/403。"""


class NotFoundError(HubError):
    """仓库或文件不存在。HTTP 404。"""


class EndpointUnavailable(HubError):
    """所有端点都失败。"""


@dataclass(frozen=True)
class FileMeta:
    """仓库里一个文件的元信息。"""

    rfilename: str
    size: int | None = None
    sha256: str | None = None

    @property
    def is_lfs(self) -> bool:
        return self.sha256 is not None


class HubClient:
    """对 Hugging Face API 的最小封装，带端点回落。"""

    def __init__(self, policy: EndpointPolicy, timeout: int = 60) -> None:
        self.policy = policy
        self.timeout = timeout
        self._blobs_cache: dict[tuple[str, str], list[FileMeta]] = {}

    # ------------------------------------------------------------------ #

    def _request(self, ep: Endpoint, path: str, method: str = "GET"):
        url = ep.url(path)
        req = urllib.request.Request(url, method=method)
        req.add_header("User-Agent", _USER_AGENT)
        for k, v in self.policy.auth_headers(ep).items():
            req.add_header(k, v)
        try:
            return urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")[:200]
            except Exception:  # noqa: BLE001
                pass
            if exc.code in (401, 403):
                raise AuthError(f"{ep.base} -> {exc.code}（仓库 gated 或令牌无效）{body}") from exc
            if exc.code == 404:
                raise NotFoundError(f"{ep.base} -> 404: {path}") from exc
            raise HubError(f"{ep.base} -> {exc.code}: {path} {body}") from exc
        except urllib.error.URLError as exc:
            raise EndpointUnavailable(f"{ep.base} 请求失败: {exc.reason}") from exc
        except OSError as exc:
            raise EndpointUnavailable(f"{ep.base} 请求失败: {exc}") from exc

    def _try_endpoints(self, path: str, method: str = "GET"):
        """按策略顺序逐个端点试，全部失败则抛出最后一个异常。

        404 也继续试下一个端点 —— 镜像可能还没同步某个文件，
        而官方已经有。但如果所有端点都 404，抛 404（比后来的网络错误更准确）。
        """
        last: Exception | None = None
        not_found: NotFoundError | None = None
        for ep in self.policy.candidates():
            try:
                return self._request(ep, path, method)
            except NotFoundError as exc:
                not_found = exc
                last = exc
                continue
            except Exception as exc:  # noqa: BLE001
                last = exc
                continue
        if not_found is not None:
            raise not_found
        raise EndpointUnavailable(f"所有端点都失败（{path}）：{last}") from last

    # ------------------------------------------------------------------ #

    def list_blobs(
        self,
        repo: str,
        revision: str = "main",
        repo_type: str = "model",
    ) -> list[FileMeta]:
        """一次调用取回该仓所有文件的路径 / 字节数 / 权威 sha256。

        失败时**不缓存**空结果 —— 让下一次调用重新试，
        避免"一次网络抖动导致整仓都跳过完整性校验"。
        """
        key = (f"{repo_type}/{repo}", revision)
        hit = self._blobs_cache.get(key)
        if hit is not None:
            return hit

        path = f"api/{repo_type}s/{repo}/revision/{revision}?blobs=true"
        with self._try_endpoints(path) as resp:
            raw = resp.read()
        try:
            data: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HubError(f"{repo}: API 返回的不是合法 JSON: {exc}") from exc

        out: list[FileMeta] = []
        for sib in data.get("siblings", []):
            name = sib.get("rfilename")
            if not name:
                continue
            lfs = sib.get("lfs") or {}
            size = lfs.get("size")
            if size is None:
                size = sib.get("size")
            out.append(
                FileMeta(
                    rfilename=name,
                    size=int(size) if size is not None else None,
                    sha256=lfs.get("sha256"),
                )
            )
        if not out:
            raise HubError(f"{repo}: API 返回空文件列表（该端点可能不提供 gated 仓的 blob）")
        self._blobs_cache[key] = out
        return out

    def resolve_url(
        self,
        repo: str,
        filename: str,
        revision: str = "main",
        repo_type: str = "model",
    ) -> tuple[str, bool]:
        """解析出最终可下载 URL。

        返回 ``(url, need_auth)``：

        * 大文件（LFS）会 302 到带签名的 CDN URL —— 签名本身即授权，
          **绝不能**再叠令牌。
        * 小文件（非 LFS）不跳 CDN，最终 URL 停在官方 resolve 端点，
          这时 **必须**自带 Authorization，否则下回来的是 148 字节的
          "Access to model ... is restricted" 错误页。
        """
        quoted = urllib.request.quote(filename)
        # ⚠ resolve 路径**没有** "models/" 前缀：
        #   模型   -> /{repo}/resolve/{rev}/{file}
        #   数据集 -> /datasets/{repo}/resolve/{rev}/{file}
        #   空间   -> /spaces/{repo}/resolve/{rev}/{file}
        # （带前缀会 404 —— 那是 API 路径的写法，不是下载路径的。）
        prefix = "" if repo_type == "model" else f"{repo_type}s/"
        path = f"{prefix}{repo}/resolve/{revision}/{quoted}"
        with self._try_endpoints(path, method="HEAD") as resp:
            final = resp.geturl()
        return final, self.policy.needs_auth_for(final)

    def whoami(self) -> dict[str, Any]:
        """当前令牌对应哪个账号（用于自检）。"""
        last: Exception | None = None
        for ep in self.policy.candidates():
            try:
                with self._request(ep, "api/whoami-v2") as resp:
                    return json.loads(resp.read())
            except Exception as exc:  # noqa: BLE001
                last = exc
        raise EndpointUnavailable(f"无法确认令牌身份: {last}") from last


def default_token() -> str:
    """从环境变量取令牌。"""
    for name in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"):
        val = (os.environ.get(name) or "").strip()
        if val:
            return val
    return ""
