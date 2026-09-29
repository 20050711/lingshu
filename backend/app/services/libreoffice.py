"""LibreOffice 文档转换服务（二期 M8）。

- docx → HTML（图片内联 base64，单文件可 iframe 预览）
- pptx/docx → PDF（pptx 的 HTML 导出质量差，改走 PDF 浏览器原生渲染）

实现：subprocess 直调 soffice --headless；单实例信号量 + 每次独立
-env:UserInstallation profile 防并发锁冲突。soffice 不可用 → 抛 RuntimeError
（上层转 E011 友好错误 + 告警日志）。
"""
from __future__ import annotations

import asyncio
import base64
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger("services.libreoffice")
_settings = get_settings()

_sem = asyncio.Semaphore(1)  # soffice 单实例（并发转换会锁冲突）

_available: bool | None = None


def is_available() -> bool:
    """探测 soffice 可用性（启动时与调用时使用）。"""
    global _available
    if _available is None:
        _available = bool(shutil.which(_settings.libreoffice_path)) or Path(_settings.libreoffice_path).exists()
        if not _available:
            logger.warning("LibreOffice 不可用（%s），文档预览/PDF 导出将返回 E011", _settings.libreoffice_path)
    return _available


def _find_cached(out: Path, src: Path, kind: str) -> str | None:
    """查找转换缓存（4.1 抽取到 services/preview_cache.py，与 OfficeCLI 适配器共用）。"""
    from app.services.preview_cache import find_cached

    return find_cached(out, src, kind)


async def run_convert(args: list[str], timeout: int = 120) -> None:
    """通用转换入口（2026-09-10）：单实例信号量 + 独立 profile + 可配置 soffice 路径。

    供 services/doc_index 的「老 Office 家族 → 文本」索引用（原 doc_index 自建 subprocess 并
    硬编码 "soffice"，既重复实现又违反「不硬编码路径」约定）。
    """
    async with _sem:
        await asyncio.to_thread(_run_soffice, args, timeout)


def _run_soffice(args: list[str], timeout: int = 120) -> None:
    """执行 soffice 转换（单实例 + 独立 profile）。"""
    if not is_available():
        raise RuntimeError("LibreOffice 转换服务不可用，请检查 soffice 安装")
    profile = tempfile.mkdtemp(prefix="lo_profile_")
    cmd = [
        _settings.libreoffice_path, "--headless",
        "-env:UserInstallation=file://" + profile,
        *args,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    shutil.rmtree(profile, ignore_errors=True)
    if proc.returncode != 0:
        raise RuntimeError(f"LibreOffice 转换失败: {proc.stderr[:200] or proc.stdout[:200]}")


async def to_html(docx_path: str, out_dir: str, cache_dir: str | None = None) -> str:
    """docx → 单文件 HTML（图片内联 base64）。返回 HTML 路径。

    缓存：同一源文件已转换过（产物 mtime >= 源 mtime）→ 直接复用，免重复 soffice。
    2026-09-17：缓存目录可按会话传（cache_dir）——跨 ask 再看同一份文档不再重转。
    """
    src = Path(docx_path)
    if not src.exists():
        raise RuntimeError("源文档不存在")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    from app.services.preview_cache import resolve_hit, stash

    cached = resolve_hit(out, src, "preview", cache_dir)
    if cached:
        return cached

    work = out / f"_lo_{uuid.uuid4().hex[:8]}"
    work.mkdir()
    async with _sem:
        await asyncio.to_thread(
            _run_soffice, ["--convert-to", "html:HTML", "--outdir", str(work), str(src)]
        )
    html_files = list(work.glob("*.html"))
    if not html_files:
        raise RuntimeError("LibreOffice 未生成 HTML")
    html_path = html_files[0]

    # 图片内联：<img src="xxx.png"> → data URI，随后清理外部图片
    html = html_path.read_text(encoding="utf-8", errors="ignore")
    for img in work.glob("*.*"):
        if img.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".svg"):
            mime = "image/png" if img.suffix.lower() == ".png" else "image/jpeg"
            b64 = base64.b64encode(img.read_bytes()).decode()
            html = html.replace(f'src="{img.name}"', f'src="data:{mime};base64,{b64}"')

    final = out / f"{src.stem}_preview_{uuid.uuid4().hex[:8]}.html"
    final.write_text(html, encoding="utf-8")
    shutil.rmtree(work, ignore_errors=True)
    stash(str(final), cache_dir)
    logger.info("docx→HTML 预览生成: %s", final.name)
    return str(final)


async def to_pdf(path: str, out_dir: str, cache_dir: str | None = None) -> str:
    """docx/pptx → PDF（浏览器原生渲染质量优于 Impress HTML 导出）。返回 PDF 路径。

    缓存：同一源文件已转换过 → 直接复用（2026-09-17：可选会话级目录，跨 ask 也命中）。
    """
    src = Path(path)
    if not src.exists():
        raise RuntimeError("源文档不存在")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    from app.services.preview_cache import resolve_hit as _resolve_hit
    from app.services.preview_cache import stash as _stash

    cached = _resolve_hit(out, src, "pdf", cache_dir)
    if cached:
        return cached

    work = out / f"_lo_{uuid.uuid4().hex[:8]}"
    work.mkdir()
    async with _sem:
        await asyncio.to_thread(
            _run_soffice, ["--convert-to", "pdf", "--outdir", str(work), str(src)]
        )
    pdf_files = list(work.glob("*.pdf"))
    if not pdf_files:
        raise RuntimeError("LibreOffice 未生成 PDF")
    final = out / f"{src.stem}_{uuid.uuid4().hex[:8]}.pdf"
    shutil.move(str(pdf_files[0]), str(final))
    shutil.rmtree(work, ignore_errors=True)
    _stash(str(final), cache_dir)
    return str(final)
