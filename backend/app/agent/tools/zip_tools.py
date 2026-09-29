"""zip_extract / zip_pack 工具（2026-08-21）：会话级 zip 解压与打包。

- zip_extract：agent 主动解压 zip（上传自动解压之外的二次场景，如 zip 内的 zip）到
  白名单目录内子文件夹；源过读白名单，写目标手写 containment（读校验不拦写）。
- zip_pack：把白名单内文件/目录打包成 zip 落到产出目录（write=True 交付语义），
  走现有 outputs 下载链路（get_output 零改动）。
- 两工具恒注入主链（skill_router always_inject + 白名单豁免），不进子代理 ROLE_TOOLS。
"""
from __future__ import annotations

import asyncio
import posixpath
import uuid
from pathlib import Path

from app.agent.tools import ToolContext, ToolSpec, register_tool, resolve_output_url, validate_readable_path
from app.core.config import get_settings
from app.core.file_utils import sanitize_filename
from app.core.url_utils import output_url

_settings = get_settings()


def _write_target_ok(target: Path, ctx: ToolContext) -> str | None:
    """写目标 containment：仅允许当前会话上传根 / 产出根 / 沙盒根（读校验不拦写，此处手写）。"""
    try:
        p = target.resolve()
    except OSError:
        return f"非法路径: {str(target)[:80]}"
    upload_root = (Path(_settings.upload_dir) / "users" / str(ctx.user_id) / ctx.session_id).resolve()
    out_root = Path(ctx.output_dir).resolve()
    sb_root = (Path(_settings.sandbox_dir) / str(ctx.session_id)).resolve()
    if p.is_relative_to(upload_root) or p.is_relative_to(out_root) or p.is_relative_to(sb_root):
        return None
    return "无权写入该目录（仅可写入当前会话上传/产出/沙盒目录）"


async def run_zip_extract(args: dict, ctx: ToolContext) -> dict:
    from app.services.zip_utils import safe_extract_zip

    file_path = str(args.get("file_path") or "").strip()
    if not file_path:
        return {"error": "zip_extract 需要 file_path 参数（zip 文件服务器路径）"}
    # 2026-09-18 走查：**先解析成磁盘路径再用**。validate_readable_path 内部已把产出 URL
    # （/api/v1/outputs/...）解析后校验，但本函数随后拿**原串**去 Path() 开文件 → 一律"文件不存在"。
    # 实测触发路径：交付类工具返回的 file_path 正是 URL 形式（前端下载用），
    # agent 原样传回来 → 解压 zip 必失败。
    # 与 file_parse / media_tools / av_tools 同口径（那几处 2026-08-20 已修，zip 这两处漏了）。
    file_path = resolve_output_url(file_path, ctx) or file_path
    err = validate_readable_path(file_path, ctx)
    if err:
        return {"error": err}
    src = Path(file_path)
    if not src.exists() or not src.is_file():
        return {"error": f"文件不存在: {file_path[:120]}"}
    if src.suffix.lower() != ".zip":
        return {"error": "zip_extract 仅支持 .zip 文件"}
    # 目标目录：显式参数优先；缺省 {源目录}/{stem}_unzip/。2026-09-08 防重复解压：
    # 默认目录已存在（先前解压过）→ 不重解，直接返回既有清单（原行为递增成 _unzip2 再解一遍，
    # 浪费且 Agent 易读到旧副本；同参拦截又只挡"完全相同参数"）
    target_str = str(args.get("target_dir") or "").strip()
    if target_str:
        target = Path(target_str)
    else:
        target = src.parent / f"{src.stem}_unzip"
        if target.is_dir():
            existing = sorted(str(p) for p in target.rglob("*") if p.is_file())[:100]
            return {
                "target_dir": str(target),
                "unzipped": existing,
                "count": len(existing),
                "note": f"该压缩包此前已解压至 {target.name}/（{len(existing)} 个文件，本次未重复解压）；"
                        "读取其中文件时把返回的绝对路径原样传给 file_parse；确需重新解压请显式传 target_dir。",
            }
        n = 2
        while target.exists():
            target = target.with_name(f"{src.stem}_unzip{n}")
            n += 1
    werr = _write_target_ok(target, ctx)
    if werr:
        return {"error": werr}
    try:
        data = await asyncio.to_thread(Path.read_bytes, src)
        unzipped = await asyncio.to_thread(
            safe_extract_zip, data, target,
            _settings.session_zip_max_total_bytes,
            _settings.session_zip_max_files,
            _settings.session_zip_max_depth,
        )
    except ValueError as e:
        return {"error": f"zip 解压失败: {str(e)[:200]}"}
    except OSError as e:
        return {"error": f"zip 读取失败: {str(e)[:200]}"}
    # 2026-08-31（信息完备性 G5）：unzipped 直接返回绝对路径（原相对路径需 agent 自行拼接 target_dir，
    # 拼错即失败）；绝对路径可原样传给 file_parse/zip_pack
    return {
        "target_dir": str(target),
        "unzipped": [str(target / u) for u in unzipped[:100]],
        "count": len(unzipped),
        "note": "已解压到 target_dir；读取其中文件时把返回的绝对路径原样传给 file_parse。",
    }


def _valid_arcname(name: str) -> bool:
    """归档内路径校验：拒绝绝对路径 / .. 段（zip 内路径注入防护）。"""
    norm = name.replace("\\", "/")
    if not norm or norm.startswith("/") or norm.split("/")[0] in ("..", "."):
        return False
    return ".." not in norm.split("/")


async def _lookup_original_name(path: str) -> str | None:
    """2026-08-31（产出物不带编码）：物理路径（uuid8_ 前缀）→ 用户原始文件名。

    覆盖：会话上传文件（chat_files）、产出物（chat_messages.outputs label）。
    """
    from sqlalchemy import text

    from app.core.database import get_global_engine

    engine = get_global_engine()
    async with engine.connect() as conn:
        r = (await conn.execute(text("SELECT file_name FROM chat_files WHERE file_path=:p LIMIT 1"),
                                {"p": path})).first()
        if r:
            return r[0]
        rows = (await conn.execute(text(
            "SELECT outputs FROM chat_messages WHERE role='assistant' AND outputs IS NOT NULL"))).all()
        for row in rows:
            for o in (row[0] or []):
                if isinstance(o, dict) and o.get("file_path") == path and o.get("label"):
                    return str(o["label"])
    return None


async def run_zip_pack(args: dict, ctx: ToolContext) -> dict:
    entries = args.get("entries") or []
    if not isinstance(entries, list) or not entries:
        return {"error": "zip_pack 需要 entries 参数（[{path, name?}]，至少 1 项）"}

    pairs: list[tuple[Path, str]] = []
    seen: set[str] = set()
    for e in entries:
        if not isinstance(e, dict):
            return {"error": "entries 每项须为 {path, name?} 对象"}
        p = str(e.get("path") or "").strip()
        if not p:
            return {"error": "entries 每项需要 path 字段"}
        p = resolve_output_url(p, ctx) or p      # 同 zip_extract：产出 URL 先转磁盘路径再用
        err = validate_readable_path(p, ctx)
        if err:
            return {"error": err}
        src = Path(p)
        if not src.exists():
            return {"error": f"文件不存在: {p[:120]}"}
        arcname = str(e.get("name") or "").strip()
        if not arcname:
            # 2026-08-31（产出物不带编码）：默认名反查用户原始名（物理名带 uuid8_ 前缀，不面向用户）
            arcname = await _lookup_original_name(str(src)) or src.name
        if not _valid_arcname(arcname):
            return {"error": f"归档名非法（不可含绝对路径/../..）: {arcname[:80]}"}
        # 重复归档名拒绝（防同名覆盖）
        norm = posixpath.normpath(arcname)
        if norm in seen:
            return {"error": f"归档名重复: {arcname[:80]}"}
        seen.add(norm)
        if src.is_dir():
            for f in sorted(src.rglob("*")):
                if f.is_file():
                    rel = f.relative_to(src)
                    pairs.append((f, posixpath.join(norm, rel.as_posix())))
        else:
            pairs.append((src, norm))
    if not pairs:
        return {"error": "打包内容为空（目录内无文件）"}
    # 2026-08-31：磁盘流式打包（原 build_zip_bytes 全内存 BytesIO——大文件慢且双倍内存，多会话并发易 OOM；
    # 上限改为累计原始大小实时校验，超限即中止不占内存）
    from app.services.zip_utils import build_zip_file

    out_dir = Path(ctx.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_name = str(args.get("zip_name") or f"{uuid.uuid4().hex[:8]}.zip").strip()
    if not zip_name.lower().endswith(".zip"):
        zip_name += ".zip"
    zip_name = sanitize_filename(zip_name, default="export.zip")
    out_path = out_dir / zip_name
    try:
        await asyncio.to_thread(build_zip_file, pairs, out_path, _settings.session_zip_pack_max_bytes)
    except ValueError as e:
        out_path.unlink(missing_ok=True)
        return {"error": f"打包失败: {str(e)[:200]}"}
    except OSError as e:
        out_path.unlink(missing_ok=True)
        return {"error": f"zip 打包失败: {str(e)[:200]}"}
    # 2026-08-31（产出物不带编码）：label 用 agent 显式 zip_name（用户可读），未指定则通用名——
    # 物理 uuid.zip 文件名不暴露给用户（下载名走 label）
    label = str(args.get("zip_name") or "").strip() or "打包文件.zip"
    if not label.lower().endswith(".zip"):
        label += ".zip"
    return {
        "file_path": output_url(ctx.session_id, ctx.round_id, zip_name),
        "label": label,
        "file_count": len(pairs),
        "note": "zip 已生成，前端可下载",
    }


register_tool(
    ToolSpec(
        name="zip_extract", progress_keys=("unzipped", "files", "target_dir"),
        write=False,
        display_name="解压 zip",
        icon="package",
        summary="解压 zip 压缩包到当前会话目录（自动防路径穿越）",
        group="文件",
        sort_order=41,
        user_description=(
            "把 zip 压缩包解压到会话上传目录的子文件夹（上传时会自动解压一次；"
            "zip 内的 zip 等二次场景用本工具）。解压目录可直接读取，安全校验防路径穿越。"
        ),
        description=(
            "What：解压 zip 压缩包到当前会话目录（源 zip 或 zip 内的 zip）。\n"
            "When：上传的 zip 未自动解压、需要解压 zip 内的 zip、或需要把 zip 展开成目录浏览时调用。\n"
            "How：file_path 必填（zip 服务器路径，可从上传文件清单获取）；target_dir 可选（默认"
            " {zip名}_unzip/，同名已存在自动递增后缀）。\n"
            "注意：上传 zip 时系统已自动解压到 {zip名}_unzip/（文件清单里有说明）——"
            "只有 zip 内的 zip 或需要自定义解压位置时才用本工具。\n"
            "Result：返回解压目录 target_dir 与文件清单；读取其中文件时把完整服务器路径传给 file_parse。\n"
            "**边界：不是 zip/已损坏/加密 → 如实说明，别对同一文件重复解压；解出来的路径原样用，不要改写。**"
        ),
        parameters={
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "zip 文件服务器路径（上传文件清单中有）"},
                "target_dir": {"type": "string", "description": "可选：目标解压目录（默认 {zip名}_unzip/）"},
            },
            "required": ["file_path"],
        },
        queue="default",
        handler=run_zip_extract,
    )
)

register_tool(
    ToolSpec(
        name="zip_pack", progress_keys=("file_path",),
        write=True,
        display_name="打包 zip",
        icon="compress",
        summary="把文件/文件夹打包成 zip 供下载",
        group="产出",
        sort_order=7,
        user_description=(
            "把文件打包成 zip 压缩包供下载：当前会话文件（上传/产出/沙盒中间产物）"
            "以及知识库里的原文件。目录会保留层级结构。"
        ),
        description=(
            "What：把白名单内文件/目录打包成 zip 产出（预览区可直接下载）。\n"
            "When：用户要批量下载多个文件、把沙盒 work 中间产物交付、打包上传素材、"
            "或**打包知识库原文件**交付时调用。\n"
            "How：entries 必填 [{path, name?}]——path 为服务器路径（可含目录，目录递归打包），"
            "name 可选归档内名称；zip_name 可选输出文件名。\n"
            "路径获取：沙盒 work 文件路径形如 /data/sandbox/{session_id}/{round_id}/work/{文件名}——"
            "先用 run_script（ls 工作目录）查看确切文件名；知识库文件路径取 file_search 返回的 file_path；"
            "上传文件路径见会话文件清单。"
            "**禁止按上面的模板编造 session_id/文件名**（猜出来的路径必然报无权访问）——"
            "清单里没有就用工具去定位（run_script ls 工作目录 / file_search 按文件名匹配），"
            "**不要向用户索要服务器路径**。\n"
            "注意：**不要用 run_script 或 zipfile 手动打包**——沙盒内生成的 zip 无法直接给用户下载，"
            "必须用本工具打包（产物自动出现在预览区）。\n"
            "Result：返回可下载 zip 文件。向用户说明打包了哪些内容。\n"
            "**边界：源路径不存在/越界会直接报错——路径取自工具返回或清单，不要编造；失败后按提示改路径，别原样重试。**"
        ),
        parameters={
            "type": "object",
            "properties": {
                "entries": {
                    "type": "array",
                    "description": "打包条目：[{path, name?}]——path 服务器路径（目录会递归打包保留层级），name 可选归档内名",
                    "items": {"type": "object", "properties": {"path": {"type": "string"}, "name": {"type": "string"}}},
                },
                "zip_name": {"type": "string", "description": "可选：输出 zip 文件名（默认自动命名）"},
            },
            "required": ["entries"],
        },
        queue="report",
        handler=run_zip_pack,
    )
)
