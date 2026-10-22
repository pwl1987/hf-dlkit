"""端点策略：谁能收到令牌、失败时回落到哪。

两条铁律
--------
1. **令牌只发给官方主机**。``hf-mirror.com`` 之类的第三方镜像绝不在白名单里：
   令牌发过去等于泄露给镜像运营方；而且镜像根本不透传 ``Authorization``，
   带上也不生效（gated 仓库对镜像一律 401）。
2. **官方端点始终是回落**，不要求"有令牌才启用"。镜像整站 503
   （公司代理隧道失败）时，只有官网能解析出 CDN 签名 URL；
   而非 gated 仓库官网匿名也能用。
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

__all__ = ["Endpoint", "EndpointPolicy", "host_of"]


def host_of(url: str) -> str:
    """取 URL 的主机名（小写）。"""
    return (urlparse(url).hostname or "").lower()


@dataclass(frozen=True)
class Endpoint:
    """一个可尝试的 API 端点。"""

    base: str
    use_auth: bool

    def url(self, path: str) -> str:
        return f"{self.base}/{path.lstrip('/')}"


class EndpointPolicy:
    """决定"按什么顺序试哪些端点、每个端点带不带令牌"。"""

    def __init__(
        self,
        endpoint: str,
        fallback: str = "",
        token: str = "",
        token_hosts: tuple[str, ...] = ("huggingface.co",),
    ) -> None:
        self.token = token or ""
        self.token_hosts = {h.lower() for h in token_hosts}
        seen: list[Endpoint] = []
        for base in (endpoint, fallback):
            if not base:
                continue
            base = base.rstrip("/")
            if any(e.base == base for e in seen):
                continue
            seen.append(Endpoint(base=base, use_auth=self._may_carry_token(base)))
        self.endpoints: tuple[Endpoint, ...] = tuple(seen)

    def _may_carry_token(self, base: str) -> bool:
        """该主机是否允许接收 Authorization 头。"""
        return bool(self.token) and host_of(base) in self.token_hosts

    def candidates(self) -> tuple[Endpoint, ...]:
        return self.endpoints

    def auth_headers(self, ep: Endpoint) -> dict[str, str]:
        if ep.use_auth and self.token:
            return {"Authorization": f"Bearer {self.token}"}
        return {}

    def needs_auth_for(self, final_url: str) -> bool:
        """最终 URL 是否还需要 Authorization 头。

        载荷 CDN 的签名 URL 本身就带授权，**绝不能**再叠一个令牌
        （既没必要，也会把令牌泄露给 CDN 运营方）。
        只有最终 URL 仍停在官方 resolve 端点（小文件不跳 CDN 的情形）才需要。
        """
        return host_of(final_url) in self.token_hosts

    def describe(self) -> str:
        return ", ".join(f"{e.base}{'(带令牌)' if e.use_auth else ''}" for e in self.endpoints)
