"""命令行入口。

    hfdl plan       列出将下载什么（不下）
    hfdl download   下载（切片并行 + 逐文件 sha256 校验）
    hfdl verify     全量 sha256 校验
    hfdl status     本地进度看板
    hfdl report     生成 Markdown 报表
    hfdl watch      看门狗：进程崩了自动拉起

退出码
------
0  成功
1  有文件下载/校验失败
2  有仓库需要 HF 令牌（gated）
3  参数或配置错误
4  有文件无法校验（拿不到权威元数据）
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from . import __version__
from .config import Config, ConfigError, load_config
from .endpoints import EndpointPolicy
from .hub import AuthError, HubClient
from .integrity import dir_size, has_complete_mark, has_gated_mark
from .manifest import Entry, Manifest, ManifestError, load_manifest, matches_selector, select_files
from .pipeline import build_plan, run_download, run_verify
from .reporter import Reporter
from .report import build_report, render_markdown
from .transfer import TransferEngine

__all__ = ["main", "build_parser"]

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_AUTH = 2
EXIT_USAGE = 3
EXIT_UNVERIFIED = 4


# --------------------------------------------------------------------------- #
# 参数
# --------------------------------------------------------------------------- #


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--manifest", "-m", default="manifest.json", help="清单路径")
    p.add_argument("--config", "-c", default="", help="TOML 配置路径")
    p.add_argument("--only", action="append", default=[], metavar="PAT",
                   help="只处理仓库名/分类路径命中该串（或 glob）的条目，可重复")
    p.add_argument("--root", default="", help="落盘根目录（覆盖清单里的 root）")
    p.add_argument("--endpoint", default="", help="主端点（默认镜像）")
    p.add_argument("--fallback", default="", help="回落端点（默认官网）")
    p.add_argument("--verbose", "-v", action="store_true", help="更详细的输出")
    p.add_argument("--quiet", "-q", action="store_true", help="只输出错误")
    p.add_argument("--log-file", default="", help="同时写入日志文件")


def _add_transfer(p: argparse.ArgumentParser) -> None:
    p.add_argument("--workers", type=int, default=None, help="并发连接数")
    p.add_argument("--tasks", type=int, default=None, help="任务线程数")
    p.add_argument("--chunk-size", type=int, default=None, help="每片字节数")
    p.add_argument("--chunk-threshold", type=int, default=None, help="超过该大小走切片并行")
    p.add_argument("--retries", type=int, default=None, help="单文件重试次数")
    p.add_argument("--no-curl", action="store_true", help="不用外部 curl，纯 Python 取数")
    p.add_argument("--force", action="store_true", help="忽略 .complete 标记，强制重做")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="hfdl",
        description="面向大文件的 Hugging Face 下载器：切片并行、端点回落、逐文件 sha256 校验",
    )
    ap.add_argument("--version", action="version", version=f"hfdl {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="列出将下载什么（不下）")
    _add_common(p)
    p.add_argument("--json", action="store_true", help="输出 JSON")

    p = sub.add_parser("download", help="下载")
    _add_common(p)
    _add_transfer(p)

    p = sub.add_parser("verify", help="全量 sha256 校验")
    _add_common(p)
    p.add_argument("--workers", type=int, default=16, help="校验并发数")

    p = sub.add_parser("status", help="本地进度看板")
    _add_common(p)
    p.add_argument("--remote", action="store_true", help="同时拉远端元数据（慢）")

    p = sub.add_parser("report", help="生成 Markdown 报表")
    _add_common(p)
    p.add_argument("--out", "-o", default="", help="输出文件（默认写到 <root>/README.md）")

    p = sub.add_parser("watch", help="看门狗：崩溃自动拉起")
    _add_common(p)
    _add_transfer(p)
    p.add_argument("--interval", type=int, default=60, help="两轮之间的间隔秒数")
    p.add_argument("--max-rounds", type=int, default=0, help="最多跑几轮（0 = 不限）")

    p = sub.add_parser("get", help="下载单个仓库（不需要清单）")
    p.add_argument("repo", help="仓库 id，例如 openai-community/gpt2")
    p.add_argument("--repo-type", default="model", choices=("model", "dataset", "space"))
    p.add_argument("--revision", default="main", help="版本（分支/标签/commit）")
    p.add_argument("--dir", default="", help="输出目录（默认 ./<owner>__<repo>）")
    p.add_argument("--include", action="append", default=[], metavar="GLOB",
                   help="只下匹配的文件，可重复")
    p.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                   help="排除匹配的文件，可重复")
    p.add_argument("--config", "-c", default="", help="TOML 配置路径")
    p.add_argument("--endpoint", default="", help="主端点")
    p.add_argument("--fallback", default="", help="回落端点")
    p.add_argument("--workers", type=int, default=None, help="并发连接数")
    p.add_argument("--tasks", type=int, default=None, help="任务线程数")
    p.add_argument("--chunk-size", type=int, default=None, help="每片字节数")
    p.add_argument("--chunk-threshold", type=int, default=None, help="超过该大小走切片并行")
    p.add_argument("--retries", type=int, default=None, help="单文件重试次数")
    p.add_argument("--no-curl", action="store_true", help="不用外部 curl，纯 Python 取数")
    p.add_argument("--dry-run", action="store_true", help="只列清单不下载")
    p.add_argument("--verbose", "-v", action="store_true", help="更详细的输出")
    p.add_argument("--quiet", "-q", action="store_true", help="只输出错误")
    p.add_argument("--log-file", default="", help="同时写入日志文件")

    return ap


# --------------------------------------------------------------------------- #
# 组装
# --------------------------------------------------------------------------- #


def _load(args) -> tuple[Config, Manifest, Reporter]:
    reporter = Reporter(quiet=getattr(args, "quiet", False),
                        verbose=getattr(args, "verbose", False),
                        log_file=getattr(args, "log_file", ""))
    try:
        cfg = load_config(
            args.config or None,
            overrides={
                "manifest": args.manifest,
                "root": args.root or None,
                "endpoint": args.endpoint or None,
                "fallback_endpoint": args.fallback or None,
                "chunk_size": getattr(args, "chunk_size", None),
                "chunk_threshold": getattr(args, "chunk_threshold", None),
                "retries": getattr(args, "retries", None),
                "use_curl": False if getattr(args, "no_curl", False) else None,
                "verbose": True if getattr(args, "verbose", False) else None,
                "quiet": True if getattr(args, "quiet", False) else None,
            },
        )
        manifest = load_manifest(args.manifest)
    except (ConfigError, ManifestError) as exc:
        reporter.error(str(exc))
        reporter.close()
        raise SystemExit(EXIT_USAGE)

    if args.root:
        manifest.root = args.root
    if not manifest.root:
        manifest.root = str(Path(args.manifest).resolve().parent / "models")

    cfg.manifest = args.manifest
    cfg.apply_no_proxy()
    return cfg, manifest, reporter


def _apply_transfer_overrides(cfg: Config, args) -> None:
    """把传输相关旗标写进配置。

    单独拎出来，因为 ``verify`` 子命令也有个 ``--workers``，
    那是**校验**并发数，不是下载连接数 —— 不能混。
    """
    if getattr(args, "workers", None):
        cfg.workers = int(args.workers)
    if getattr(args, "tasks", None):
        cfg.tasks = int(args.tasks)
    if cfg.tasks < cfg.workers:
        cfg.tasks = cfg.workers
    if getattr(args, "chunk_size", None):
        cfg.chunk_size = int(args.chunk_size)
    if getattr(args, "chunk_threshold", None):
        cfg.chunk_threshold = int(args.chunk_threshold)


def _hub(cfg: Config) -> HubClient:
    policy = EndpointPolicy(
        endpoint=cfg.endpoint,
        fallback=cfg.fallback_endpoint,
        token=cfg.token,
        token_hosts=cfg.token_hosts,
    )
    return HubClient(policy)


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #


def cmd_plan(args) -> int:
    cfg, manifest, rep = _load(args)
    hub = _hub(cfg)
    plan = build_plan(manifest, cfg, hub, args.only, ignore_complete=True, log=rep.info)

    if args.json:
        import json

        data = {
            "root": manifest.root,
            "total_bytes": plan.total_bytes,
            "repos": [
                {
                    "repo": pe.entry.repo,
                    "dest": pe.dest,
                    "files": len(pe.files),
                    "bytes": pe.total,
                    "files_list": [
                        {"name": m.rfilename, "size": m.size, "sha256": m.sha256}
                        for m in pe.files
                    ],
                }
                for pe in plan.entries
            ],
        }
        rep.raw(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        for pe in plan.entries:
            rep.raw(f"--- {pe.entry.repo} -> {pe.dest}")
            for m in pe.files:
                rep.raw(f"      {(m.size or 0)/1073741824:9.3f} GB  {m.rfilename}")
        rep.raw(f"[plan] 选中 {plan.file_count} 个文件，合计 "
                f"{plan.total_bytes/1073741824:.2f} GB")
    rep.close()
    return EXIT_OK


def cmd_download(args) -> int:
    cfg, manifest, rep = _load(args)
    _apply_transfer_overrides(cfg, args)
    hub = _hub(cfg)
    plan = build_plan(manifest, cfg, hub, args.only,
                      ignore_complete=getattr(args, "force", False), log=rep.info)
    if not plan.entries:
        rep.info("没有需要处理的仓库")
        rep.close()
        return EXIT_OK

    engine = TransferEngine(cfg, hub, log=rep.info)
    try:
        report = run_download(plan, cfg, engine, log=rep.info)
    finally:
        engine.close()

    rep.info(f"完成 {report.ok} 个 / 跳过 {report.skipped} 个 / "
             f"失败 {report.failed} 个 / 需令牌 {report.auth_failed} 个")
    rep.info(f"新下载 {report.bytes_downloaded/1073741824:.2f} GB")
    rep.close()

    if report.auth_failed or report.repos_gated:
        return EXIT_AUTH
    if report.has_failures:
        return EXIT_FAILED
    return EXIT_OK


def cmd_verify(args) -> int:
    cfg, manifest, rep = _load(args)
    hub = _hub(cfg)
    plan = build_plan(manifest, cfg, hub, args.only, ignore_complete=True, log=rep.info)
    report = run_verify(plan, log=rep.info, workers=max(1, args.workers))

    rep.info(f"总计 {report.total} 个文件：OK {report.ok}（其中仅尺寸校验 "
             f"{len(report.size_only)}），BAD {len(report.bad)}，"
             f"缺失 {len(report.missing)}，尺寸不符 {len(report.size_mismatch)}，"
             f"无法校验 {len(report.unverified)}")
    rep.close()
    if report.bad or report.missing or report.size_mismatch:
        return EXIT_FAILED
    if report.unverified:
        return EXIT_UNVERIFIED
    return EXIT_OK


def cmd_status(args) -> int:
    cfg, manifest, rep = _load(args)
    hub = _hub(cfg) if args.remote else None

    rows = []
    for entry in manifest.entries:
        if not matches_selector(entry, args.only):
            continue
        dest = manifest.dest_dir(entry)
        local = dir_size(dest) if Path(dest).exists() else 0
        expected = None
        if hub is not None:
            try:
                files = select_files(hub.list_blobs(entry.repo, revision=entry.revision),
                                     entry, cfg.glob_style)
                expected = sum(f.size or 0 for f in files)
            except Exception as exc:  # noqa: BLE001
                rep.detail(f"{entry.repo}: 元数据不可用 ({exc})")
        rows.append((entry, dest, local, expected))

    rep.raw(f"{'仓库':<52} {'本地':>12} {'远端':>12}  状态")
    for entry, _dest, local, expected in rows:
        if expected:
            pct = local / expected * 100 if expected else 0
            state = "✅ 完成" if has_complete_mark(_dest) else f"⏳ {pct:.1f}%"
        elif has_complete_mark(_dest):
            state = "✅ 完成"
        elif has_gated_mark(_dest):
            state = "🔒 gated"
        else:
            state = "—"
        exp_txt = f"{expected/1073741824:.2f} GB" if expected else "—"
        rep.raw(f"{entry.repo:<52} {local/1073741824:>9.2f} GB {exp_txt:>12}  {state}")
    total = sum(r[2] for r in rows)
    rep.raw("")
    rep.raw(f"合计本地 {total/1073741824:.2f} GB，覆盖 {len(rows)} 个仓库")
    rep.close()
    return EXIT_OK


def cmd_report(args) -> int:
    cfg, manifest, rep = _load(args)
    hub = _hub(cfg)
    rows = build_report(manifest, cfg, hub, log=rep.info)
    text = render_markdown(rows, manifest, cfg)
    out = args.out or str(Path(manifest.root) / "README.md")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(text, encoding="utf-8")
    rep.info(f"已写出 {out}（{os.path.getsize(out)} 字节）")
    rep.close()
    return EXIT_OK


def cmd_watch(args) -> int:
    """看门狗：反复跑 download 直到所有仓库收工。"""
    rounds = 0
    while True:
        rounds += 1
        rep = Reporter(quiet=args.quiet, verbose=args.verbose, log_file=args.log_file)
        try:
            cfg, manifest, _ = _load(args)
            _apply_transfer_overrides(cfg, args)
            hub = _hub(cfg)
            plan = build_plan(manifest, cfg, hub, args.only,
                              ignore_complete=getattr(args, "force", False), log=rep.info)
            if not plan.entries:
                rep.info("所有仓库都已收工")
                rep.close()
                return EXIT_OK
            engine = TransferEngine(cfg, hub, log=rep.info)
            try:
                report = run_download(plan, cfg, engine, log=rep.info)
            finally:
                engine.close()
            rep.info(f"第 {rounds} 轮：完成 {report.ok}，失败 {report.failed}，"
                     f"需令牌 {report.auth_failed}")
        finally:
            rep.close()
        if args.max_rounds and rounds >= args.max_rounds:
            return EXIT_OK
        time.sleep(max(1, args.interval))


def cmd_get(args) -> int:
    """下载单个仓库，不需要清单文件。"""
    rep = Reporter(quiet=args.quiet, verbose=args.verbose, log_file=args.log_file)
    try:
        cfg = load_config(args.config or None, overrides={
            "endpoint": args.endpoint or None,
            "fallback_endpoint": args.fallback or None,
            "workers": args.workers,
            "tasks": args.tasks,
            "chunk_size": args.chunk_size,
            "chunk_threshold": args.chunk_threshold,
            "retries": args.retries,
            "use_curl": False if args.no_curl else None,
            "verbose": True if args.verbose else None,
            "quiet": True if args.quiet else None,
        })
        cfg.apply_no_proxy()
        root = args.dir or str(Path.cwd() / args.repo.replace("/", "__"))
        entry = Entry(
            path=(),
            repo=args.repo,
            include=tuple(args.include),
            exclude=tuple(args.exclude),
            revision=args.revision,
            repo_type=args.repo_type,
        )
        manifest = Manifest(root=root, entries=[entry], source="<单仓库>")
        hub = _hub(cfg)
        plan = build_plan(manifest, cfg, hub, [], ignore_complete=True, log=rep.info)

        if args.dry_run:
            for pe in plan.entries:
                rep.raw(f"--- {pe.entry.repo} -> {pe.dest}")
                for m in pe.files:
                    rep.raw(f"      {(m.size or 0)/1073741824:9.3f} GB  {m.rfilename}")
            rep.raw(f"[dry-run] 选中 {plan.file_count} 个文件，"
                    f"合计 {plan.total_bytes/1073741824:.2f} GB")
            return EXIT_OK

        if not plan.entries:
            rep.error("没有可处理的条目")
            return EXIT_FAILED
        engine = TransferEngine(cfg, hub, log=rep.info)
        try:
            report = run_download(plan, cfg, engine, log=rep.info)
        finally:
            engine.close()
        rep.info(f"完成 {report.ok} 个 / 跳过 {report.skipped} 个 / "
                 f"失败 {report.failed} 个 / 需令牌 {report.auth_failed} 个")
        if report.auth_failed or report.repos_gated:
            return EXIT_AUTH
        if report.has_failures:
            return EXIT_FAILED
        return EXIT_OK
    except (ConfigError, ManifestError) as exc:
        rep.error(str(exc))
        return EXIT_USAGE
    finally:
        rep.close()


_COMMANDS = {
    "plan": cmd_plan,
    "download": cmd_download,
    "verify": cmd_verify,
    "status": cmd_status,
    "report": cmd_report,
    "watch": cmd_watch,
    "get": cmd_get,
}


def main(argv: list[str] | None = None) -> int:
    from .reporter import _force_utf8

    _force_utf8()
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        return _COMMANDS[args.cmd](args)
    except KeyboardInterrupt:
        print("\n已中断（已下载的文件与切片都保留，重跑会续）", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
