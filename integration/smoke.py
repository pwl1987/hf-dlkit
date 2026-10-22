#!/usr/bin/env python
"""端到端冒烟测试：真的从 Hugging Face 下载一个 tiny 仓，校验完整性。

单独放在 ``integration/`` 而不是 ``tests/``，因为它**需要联网**，
单元测试套件不该被网络波动影响。CI 的 integration job 显式跑它。

用法::

    python integration/smoke.py [--use-curl] [--repo <owner/name>]

覆盖到的真实路径：API 元数据 -> resolve URL -> 取数（curl 或纯 Python）
-> 组装 -> sha256 校验 -> 写 .ok / .complete 标记 -> verify 复核。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

# 允许直接从源码目录跑（未安装时）
_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir():
    sys.path.insert(0, str(_SRC))

from hfdl.config import Config  # noqa: E402
from hfdl.endpoints import EndpointPolicy  # noqa: E402
from hfdl.hub import HubClient  # noqa: E402
from hfdl.integrity import has_complete_mark, has_ok_mark  # noqa: E402
from hfdl.manifest import Entry, Manifest  # noqa: E402
from hfdl.pipeline import build_plan, run_download, run_verify  # noqa: E402
from hfdl.transfer import CurlFetcher, PythonFetcher, TransferEngine  # noqa: E402

DEFAULT_REPO = "hf-internal-testing/tiny-random-gpt2"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--use-curl", action="store_true", default=True)
    ap.add_argument("--no-curl", dest="use_curl", action="store_false")
    ap.add_argument("--keep", action="store_true", help="保留下载目录（调试用）")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="hfdl-smoke-"))
    print(f"[smoke] 工作目录 {tmp}")
    print(f"[smoke] 取数后端 = {'curl' if args.use_curl else '纯 Python'}")

    try:
        cfg = Config(use_curl=args.use_curl)
        cfg.apply_no_proxy()
        token = cfg.token
        policy = EndpointPolicy(cfg.endpoint, cfg.fallback_endpoint, token, cfg.token_hosts)
        hub = HubClient(policy)
        print(f"[smoke] 端点 {policy.describe()}")
        print(f"[smoke] curl 版本探测 = "
              f"{CurlFetcher(token).version if args.use_curl else 'n/a'}")

        entry = Entry(path=("smoke",), repo=args.repo)
        manifest = Manifest(root=str(tmp / "models"), entries=[entry], source="smoke")

        plan = build_plan(manifest, cfg, hub, [], ignore_complete=True, log=print)
        if not plan.entries:
            print("[smoke] !! 没拿到任何条目")
            return 1
        if not plan.entries[0].files:
            print("[smoke] !! 文件列表为空")
            return 1
        total = plan.entries[0].total
        print(f"[smoke] 计划 {len(plan.entries[0].files)} 个文件，{total/1048576:.1f} MB")

        fetcher = None
        if not args.use_curl:
            fetcher = PythonFetcher(token)
        engine = TransferEngine(cfg, hub, log=print, fetcher=fetcher)
        try:
            report = run_download(plan, cfg, engine, log=print)
        finally:
            engine.close()

        print(f"[smoke] 下载：完成 {report.ok} 跳过 {report.skipped} "
              f"失败 {report.failed} 需令牌 {report.auth_failed}")
        if report.failed or report.auth_failed:
            print("[smoke] !! 下载有失败")
            return 1
        if report.ok == 0:
            print("[smoke] !! 一个文件都没下下来")
            return 1

        dest = manifest.dest_dir(entry)
        if not has_complete_mark(dest):
            print("[smoke] !! 没写 .complete 标记")
            return 1
        if not has_ok_mark(Path(dest) / "config.json"):
            print("[smoke] !! 没写 .ok 标记")
            return 1

        # 复核
        plan2 = build_plan(manifest, cfg, hub, [], ignore_complete=True, log=print)
        vrep = run_verify(plan2, log=print, workers=4)
        print(f"[smoke] 校验：OK {vrep.ok} BAD {len(vrep.bad)} "
              f"缺失 {len(vrep.missing)} 尺寸不符 {len(vrep.size_mismatch)} "
              f"无法校验 {len(vrep.unverified)}")
        if vrep.bad or vrep.missing or vrep.size_mismatch:
            print("[smoke] !! 校验不通过")
            return 1

        # 再跑一遍：应该全部命中 .ok 跳过（幂等性）
        plan3 = build_plan(manifest, cfg, hub, [], ignore_complete=True, log=print)
        engine2 = TransferEngine(cfg, hub, log=print, fetcher=fetcher)
        try:
            report2 = run_download(plan3, cfg, engine2, log=print)
        finally:
            engine2.close()
        print(f"[smoke] 重跑：跳过 {report2.skipped} 新下 {report2.ok}")
        if report2.ok != 0:
            print("[smoke] !! 重跑不该再下载任何东西")
            return 1

        print("[smoke] ✅ 全部通过")
        return 0
    finally:
        if args.keep:
            print(f"[smoke] 保留 {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
