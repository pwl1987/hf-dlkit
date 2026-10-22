"""生成模型合集报表（Markdown）。

⚠ 这里曾经有个真实的坑：生成器只打镜像端点，镜像 503（公司代理隧道失败）时
它打印一行 ``! repo: 503`` 就继续，不报错、不退出 —— 于是产出一份
"看起来正常"但数据全空的表格（0 文件 / 0.00 GB 却标着"✅ 完成"）。
静默的错误产物比直接报错更糟。

现在所有 API 调用都走 :mod:`hfdl.hub` 的端点回落层，
镜像不可用时自动切官网；官网也不可用时该仓**明确标成"元数据不可用"**，
绝不写成 0。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .hub import AuthError, HubClient
from .integrity import dir_size, has_complete_mark, has_gated_mark
from .manifest import Manifest, select_files

__all__ = ["RepoRow", "build_report", "render_markdown"]


@dataclass
class RepoRow:
    repo: str
    category: str
    note: str = ""
    fmt: str = ""
    file_count: int | None = None
    total_bytes: int | None = None
    local_bytes: int = 0
    complete: bool = False
    gated: bool = False
    meta_error: str = ""

    @property
    def status(self) -> str:
        if self.gated:
            return "🔒 gated（需 HF 令牌）"
        if self.complete:
            return "✅ 完成"
        if self.meta_error:
            return f"⚠️ 元数据不可用（{self.meta_error}）"
        if self.local_bytes:
            return "⏳ 部分完成"
        return "⏳ 待下"


def build_report(manifest: Manifest, config: Config, hub: HubClient, log=None) -> list[RepoRow]:
    """逐仓拉元数据 + 读本地状态。"""
    log = log or (lambda msg: None)
    rows: list[RepoRow] = []
    for entry in manifest.entries:
        dest = manifest.dest_dir(entry)
        row = RepoRow(
            repo=entry.repo,
            category="/".join(entry.path) or "未分类",
            note=entry.note,
            fmt=entry.fmt,
            local_bytes=dir_size(dest) if Path(dest).exists() else 0,
            complete=has_complete_mark(dest),
            gated=has_gated_mark(dest),
        )
        try:
            all_files = hub.list_blobs(entry.repo, revision=entry.revision,
                                       repo_type=entry.repo_type)
        except AuthError as exc:
            row.meta_error = f"需令牌"
            log(f"  !! {entry.repo}: {exc}")
        except Exception as exc:  # noqa: BLE001
            row.meta_error = str(exc)[:60]
            log(f"  !! {entry.repo}: {exc}")
        else:
            files = select_files(all_files, entry, config.glob_style)
            row.file_count = len(files)
            row.total_bytes = sum(f.size or 0 for f in files)
        rows.append(row)
    return rows


def render_markdown(rows: list[RepoRow], manifest: Manifest, config: Config) -> str:
    """渲染成 Markdown。"""
    lines: list[str] = []
    title = manifest.source and Path(manifest.source).stem or "模型合集"
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"> 清单：`{manifest.source}`　根目录：`{manifest.root}`")
    lines.append(">")
    lines.append("> 清单驱动：加模型只改 JSON，不改代码。条目支持可选的 "
                 "`include` / `exclude`（glob，匹配仓库内相对路径）：")
    lines.append("> `exclude` 剔除不想要的量化档，`include` 只保留列出的几类。")
    lines.append("> `*` **不跨** `/`，`**` 才跨 —— 这样 `mmproj-*.gguf` 不会误伤 `mtp/mmproj-x.gguf`。")
    lines.append("")

    by_cat: dict[str, list[RepoRow]] = {}
    for row in rows:
        by_cat.setdefault(row.category, []).append(row)

    grand_bytes = 0
    for cat, items in by_cat.items():
        lines.append(f"## {cat}")
        lines.append("")
        lines.append("| 仓库 | 格式 | 用途 / 说明 | 文件数 | 体积 | 状态 |")
        lines.append("|---|---|---|---:|---:|---|")
        cat_total = 0
        for row in items:
            if row.total_bytes is None:
                n_txt, size_txt = "—", "—"
            else:
                n_txt = str(row.file_count)
                size_txt = f"{row.total_bytes/1073741824:.2f} GB"
                cat_total += row.total_bytes
            note = row.note.replace("|", "\\|")
            lines.append(
                f"| `{row.repo}` | {row.fmt} | {note} | {n_txt} | {size_txt} | {row.status} |"
            )
        lines.append("")
        lines.append(f"**{cat} 小计：{cat_total/1073741824:.2f} GB**")
        lines.append("")
        grand_bytes += cat_total

    lines.append("---")
    lines.append("")
    lines.append(f"**合计 {grand_bytes/1073741824:.2f} GB**")
    lines.append("")

    lines.append("## 完整性")
    lines.append("")
    lines.append("- 每个文件旁的 `<文件名>.ok` 标记 = 该文件已通过校验（续跑时不再重算哈希）")
    lines.append("- 每个仓库目录的 `.complete` 标记 = 整仓已完成（续跑时只做尺寸快检）")
    lines.append("- 仓库目录的 `.gated` 标记 = 该仓需要 HF 令牌，已跳过")
    lines.append("- 权威 sha256 来自 HF API 的 `siblings[].lfs.sha256`；"
                 "镜像/CDN 的 `ETag` **不是** sha256，不能拿来判完整性")
    lines.append("")
    lines.append("复核全部：")
    lines.append("")
    lines.append("```powershell")
    lines.append("hfdl verify")
    lines.append("```")
    lines.append("")

    lines.append("## 端点与令牌策略")
    lines.append("")
    lines.append("| 端点 | 用途 | 带 Authorization |")
    lines.append("|---|---|---|")
    lines.append(f"| `{config.endpoint}` | 默认，元数据 + 载荷 | "
                 "❌ 不带（第三方镜像，令牌发过去等于泄露，且它也不认） |")
    lines.append(f"| `{config.fallback_endpoint or '—'}` | 回落（镜像 503 / 超时自动切换） | "
                 "✅ 带（仅限官方主机） |")
    lines.append("")
    lines.append("> ⚠ 小文件（非 LFS）不跳 CDN，最终 URL 停在官方 `resolve/...`，")
    lines.append("> 这种情况下 **curl 必须自己带 `Authorization` 头**，否则下回来的是 148 字节的")
    lines.append("> \"Access to model ... is restricted\" 错误页。大文件跳签名 CDN URL 时"
                 "**绝不能**带令牌。")
    lines.append("")

    lines.append("## 工具")
    lines.append("")
    lines.append("| 文件 | 用途 |")
    lines.append("|---|---|")
    lines.append("| `hfdl plan` | 列出将下载什么（不下） |")
    lines.append("| `hfdl download` | 下载（切片并行 + 逐文件校验） |")
    lines.append("| `hfdl verify` | 全量 sha256 校验 |")
    lines.append("| `hfdl status` | 本地进度看板 |")
    lines.append("| `hfdl report` | 生成本报表 |")
    lines.append("| `hfdl watch` | 看门狗：崩溃自动拉起 |")
    lines.append("")
    return "\n".join(lines)
