"""编排：列清单 -> 下载 -> 校验 -> 写完成标记。

并发模型
--------
所有仓库的文件被**一次性铺成全局任务表**，交给一个线程池跑完 ——
仓库之间不再互相等待。（早期版本按仓串行，小仓只有 2~4 个分片，跑不满并发，
大仓结束后小仓才开始。）

完整性语义
----------
* 拿得到权威 sha256 -> 比对 sha256。
* 该文件不是 LFS 对象（HF 没发布 sha256）-> 只比尺寸，并在报表里单独计数。
* 整个仓的元数据都拿不到 -> **不写完成标记**，仓库标为失败。
  绝不能出现"没校验"却写成"✅ 完成"。
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from .config import Config
from .hub import AuthError, FileMeta, HubClient
from .integrity import (
    FileStatus,
    Verdict,
    clear_gated,
    dir_size,
    file_size,
    has_complete_mark,
    has_gated_mark,
    mark_ok,
    sha256_file,
    verify_file,
    write_complete,
    write_gated,
)
from .manifest import Entry, Manifest, matches_selector, select_files
from .transfer import FileOutcome, Outcome, TransferEngine

__all__ = ["Plan", "PlanEntry", "RunReport", "VerifyReport", "build_plan", "run_download", "run_verify"]


@dataclass
class PlanEntry:
    entry: Entry
    dest: str
    files: list[FileMeta]
    total: int


@dataclass
class Plan:
    entries: list[PlanEntry] = field(default_factory=list)
    total_bytes: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)   # (repo, 原因)
    failed_repos: list[tuple[str, str]] = field(default_factory=list)

    @property
    def file_count(self) -> int:
        return sum(len(e.files) for e in self.entries)


@dataclass
class RunReport:
    ok: int = 0
    skipped: int = 0
    failed: int = 0
    auth_failed: int = 0
    bytes_downloaded: int = 0
    repos_complete: list[str] = field(default_factory=list)
    repos_failed: list[tuple[str, list[str]]] = field(default_factory=list)
    repos_gated: list[str] = field(default_factory=list)

    @property
    def has_failures(self) -> bool:
        return bool(self.failed or self.auth_failed or self.repos_failed)


@dataclass
class VerifyReport:
    ok: int = 0
    bad: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    size_mismatch: list[str] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)
    size_only: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return (self.ok + len(self.bad) + len(self.missing)
                + len(self.size_mismatch) + len(self.unverified))


# --------------------------------------------------------------------------- #


def build_plan(
    manifest: Manifest,
    config: Config,
    hub: HubClient,
    selectors: list[str] | None = None,
    *,
    ignore_complete: bool = False,
    log=None,
) -> Plan:
    """拉元数据、按筛选规则裁剪、生成下载计划。"""
    log = log or (lambda msg: None)
    plan = Plan()
    for entry in manifest.entries:
        if not matches_selector(entry, selectors or []):
            continue
        dest = manifest.dest_dir(entry)
        try:
            all_files = hub.list_blobs(entry.repo, revision=entry.revision,
                                       repo_type=entry.repo_type)
        except AuthError as exc:
            log(f"  !! {entry.repo} 需要令牌: {exc}")
            plan.failed_repos.append((entry.repo, str(exc)))
            continue
        except Exception as exc:  # noqa: BLE001
            log(f"  !! {entry.repo} 元数据获取失败: {exc}")
            plan.failed_repos.append((entry.repo, str(exc)))
            continue

        files = select_files(all_files, entry, config.glob_style)
        skipped_n = len(all_files) - len(files)
        total = sum(f.size or 0 for f in files)
        log(f"=== {entry.repo} -> {dest}")
        log(f"    {len(files)} 个文件，共 {total/1073741824:.2f} GB")
        if skipped_n:
            log(f"    按 include/exclude 跳过 {skipped_n} 个文件"
                f"（整仓 {sum(f.size or 0 for f in all_files)/1073741824:.2f} GB）")

        if not ignore_complete and has_complete_mark(dest):
            bad = []
            for meta in files:
                p = os.path.join(dest, *meta.rfilename.split("/"))
                if not os.path.exists(p) or (meta.size is not None and file_size(p) != meta.size):
                    bad.append(meta.rfilename)
            if not bad:
                log("    已有完成标记且尺寸全部对上，跳过整仓")
                plan.skipped.append((entry.repo, "已完成"))
                continue
            log(f"    完成标记存在但 {len(bad)} 个文件尺寸异常，将重下: {bad[:3]}")

        plan.entries.append(PlanEntry(entry=entry, dest=dest, files=files, total=total))
        plan.total_bytes += total
    return plan


def run_download(
    plan: Plan,
    config: Config,
    engine: TransferEngine,
    log=None,
) -> RunReport:
    """执行下载。跨仓库并行。"""
    log = log or (lambda msg: None)
    report = RunReport()

    # 全局任务表：所有仓库的文件一次性铺开
    jobs: list[tuple[Entry, FileMeta, str]] = []
    for pe in plan.entries:
        for meta in pe.files:
            jobs.append((pe.entry, meta, pe.dest))

    if not jobs:
        log("没有待下载的文件")
        return report

    log(f"共 {len(jobs)} 个文件待处理；并发连接 {config.workers} 路，任务线程 {config.tasks}")

    outcomes: dict[str, list[FileOutcome]] = {}
    with ThreadPoolExecutor(max_workers=max(1, config.tasks)) as ex:
        futs = {
            ex.submit(engine.download_file, e.repo, meta, dest, e.revision): (e, meta)
            for e, meta, dest in jobs
        }
        done = 0
        for fut in as_completed(futs):
            entry, meta = futs[fut]
            try:
                res = fut.result()
            except Exception as exc:  # noqa: BLE001
                res = FileOutcome(meta.rfilename, Outcome.FAILED, 0, f"{type(exc).__name__}: {exc}")
            done += 1
            outcomes.setdefault(entry.repo, []).append(res)
            if res.outcome is Outcome.OK:
                report.ok += 1
                report.bytes_downloaded += res.size
            elif res.outcome is Outcome.SKIP:
                report.skipped += 1
            elif res.outcome is Outcome.AUTH:
                report.auth_failed += 1
                log(f"    AUTH  {res.name}: {res.message}")
            else:
                report.failed += 1
                log(f"    FAIL  {res.name}: {res.message}")
            log(f"    {res.outcome.value:<5} {res.name} ({res.size/1048576:.0f} MB)  [{done}/{len(jobs)}]")

    # 写完成标记
    for pe in plan.entries:
        res_list = outcomes.get(pe.entry.repo, [])
        auth = [r for r in res_list if r.outcome is Outcome.AUTH]
        failed = [r for r in res_list if r.outcome is Outcome.FAILED]
        if auth:
            write_gated(pe.dest, pe.entry.repo, "需要 HF 令牌（且站点需接受条款）")
            report.repos_gated.append(pe.entry.repo)
            log(f"    !! {pe.entry.repo} 被 gated（需 HF 令牌），已写 .gated 标记并跳过")
            continue
        if failed:
            report.repos_failed.append((pe.entry.repo, [r.name for r in failed]))
            log(f"    !! {pe.entry.repo} 有 {len(failed)} 个文件失败，不写完成标记")
            continue
        got = dir_size(pe.dest)
        log(f"    完成度 {got/1073741824:.2f} / {pe.total/1073741824:.2f} GB  {pe.entry.repo}")
        write_complete(pe.dest, pe.entry.repo, pe.total)
        report.repos_complete.append(pe.entry.repo)
        clear_gated(pe.dest)      # 补下成功后清掉历史 .gated
    return report


def run_verify(
    plan: Plan,
    log=None,
    workers: int = 16,
) -> VerifyReport:
    """逐文件校验 sha256 / 尺寸。"""
    log = log or (lambda msg: None)
    report = VerifyReport()

    def one(pe: PlanEntry, meta: FileMeta) -> FileStatus:
        p = os.path.join(pe.dest, *meta.rfilename.split("/"))
        return verify_file(p, meta.size, meta.sha256)

    jobs = [(pe, meta) for pe in plan.entries for meta in pe.files]
    if not jobs:
        return report
    log(f"开始校验 {len(jobs)} 个文件（并发 {workers} 路）")
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {ex.submit(one, pe, meta): (pe, meta) for pe, meta in jobs}
        done = 0
        for fut in as_completed(futs):
            pe, meta = futs[fut]
            try:
                st = fut.result()
            except Exception as exc:  # noqa: BLE001
                st = FileStatus(meta.rfilename, Verdict.BAD, 0, meta.size, str(exc))
            done += 1
            if st.verdict is Verdict.OK:
                report.ok += 1
                if "仅校验尺寸" in st.detail:
                    report.size_only.append(st.name)
            elif st.verdict is Verdict.MISSING:
                report.missing.append(st.name)
            elif st.verdict is Verdict.SIZE_MISMATCH:
                report.size_mismatch.append(st.name)
            elif st.verdict is Verdict.UNVERIFIED:
                report.unverified.append(st.name)
            else:
                report.bad.append(st.name)
            log(f"    {st.verdict.value:<10} {st.name} ({st.size/1048576:.0f} MB)  [{done}/{len(jobs)}]")
    return report
