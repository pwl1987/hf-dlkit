"""端点策略与令牌边界的测试。

这些断言保护的是一条硬红线：**令牌绝不能发给第三方镜像**。
"""

from __future__ import annotations

import pytest

from hfdl.endpoints import EndpointPolicy, host_of


def test_mirror_never_receives_token() -> None:
    p = EndpointPolicy(
        endpoint="https://hf-mirror.com",
        fallback="https://huggingface.co",
        token="secret",
    )
    eps = {e.base: e.use_auth for e in p.candidates()}
    assert eps["https://hf-mirror.com"] is False
    assert eps["https://huggingface.co"] is True


def test_official_endpoint_as_primary_does_get_token() -> None:
    p = EndpointPolicy(endpoint="https://huggingface.co", fallback="", token="secret")
    assert p.candidates()[0].use_auth is True


def test_no_token_means_no_auth_anywhere() -> None:
    p = EndpointPolicy(endpoint="https://hf-mirror.com", fallback="https://huggingface.co", token="")
    assert all(e.use_auth is False for e in p.candidates())


def test_fallback_always_present_even_without_token() -> None:
    """镜像 503 时只有官网能解析 CDN 签名 URL —— 回落不要求有令牌。"""
    p = EndpointPolicy(endpoint="https://hf-mirror.com", fallback="https://huggingface.co", token="")
    assert len(p.candidates()) == 2


def test_duplicate_endpoint_is_not_repeated() -> None:
    p = EndpointPolicy(endpoint="https://huggingface.co", fallback="https://huggingface.co/", token="t")
    assert len(p.candidates()) == 1


def test_needs_auth_for_final_url() -> None:
    p = EndpointPolicy(endpoint="https://hf-mirror.com", fallback="https://huggingface.co", token="t")
    # 小文件不跳 CDN，最终 URL 停在官方 resolve 端点 -> 需要令牌
    assert p.needs_auth_for("https://huggingface.co/o/r/resolve/main/README.md") is True
    # 大文件跳到签名 CDN -> 签名本身即授权，绝不能再叠令牌
    assert p.needs_auth_for("https://us.aws.cdn.hf.co/xet-bridge-us/abc?sig=1") is False
    assert p.needs_auth_for("https://cdn-lfs.huggingface.co/repos/abc") is False


def test_auth_headers_only_for_authorised_endpoint() -> None:
    p = EndpointPolicy(endpoint="https://hf-mirror.com", fallback="https://huggingface.co", token="t")
    mirror, official = p.candidates()
    assert p.auth_headers(mirror) == {}
    assert p.auth_headers(official) == {"Authorization": "Bearer t"}


def test_host_of() -> None:
    assert host_of("https://US.AWS.cdn.hf.co/x") == "us.aws.cdn.hf.co"
    assert host_of("not a url") == ""


def test_custom_token_hosts() -> None:
    p = EndpointPolicy(
        endpoint="https://my-mirror.example",
        fallback="",
        token="t",
        token_hosts=("my-mirror.example",),
    )
    assert p.candidates()[0].use_auth is True
