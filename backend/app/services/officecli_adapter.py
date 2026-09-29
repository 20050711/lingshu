"""预览转换适配器（4.1）：OfficeCLI 优先，LibreOffice 回退。

- docx → HTML：`officecli view <file> html -o out.html`（失败/不可用回退 libreoffice.to_html）
- pptx/docx → PDF：`officecli view <file> pdf -o out.pdf`（回退 libreoffice.to_pdf）
- 产物命名与 LibreOffice 同命名空间（{stem}_preview_*.html / {stem}_*.pdf），mtime 缓存互通
- subprocess 同步调用一律 to_thread（防阻塞事件循环，HANDOVER 踩坑 16）
"""
from __future__ import annotations

import asyncio
import base64
import shutil
import subprocess
import uuid
from pathlib import Path

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.preview_cache import resolve_hit, stash

logger = get_logger("services.officecli_adapter")
_settings = get_settings()

_officecli_path: str | None | False = None  # None=未探测 False=不可用


def _cli_path() -> str | None:
    """定位 officecli 二进制（PATH 优先，回退配置目录）。"""
    global _officecli_path
    if _officecli_path is not None:
        return _officecli_path or None
    p = shutil.which("officecli")
    if p:
        _officecli_path = p
        return p
    for cand in (Path(_settings.officecli_bin_dir) / "officecli", Path(_settings.officecli_bin_dir) / "officecli.exe"):
        if cand.exists():
            _officecli_path = str(cand)
            return str(cand)
    _officecli_path = False
    logger.warning("OfficeCLI 不可用（%s），预览转换回退 LibreOffice", _settings.officecli_bin_dir)
    return None


def _inline_images(work: Path, html: str) -> str:
    """将 HTML 中引用的相对图片内联为 base64（单文件预览）。"""
    for img in work.iterdir():
        if img.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".svg"):
            mime = "image/png" if img.suffix.lower() in (".png", ".svg") else "image/jpeg"
            b64 = base64.b64encode(img.read_bytes()).decode()
            html = html.replace(f'src="{img.name}"', f'src="data:{mime};base64,{b64}"')
    return html


def _officecli_html(cli: str, src: Path, out: Path) -> str:
    work = out / f"_oc_{uuid.uuid4().hex[:8]}"
    work.mkdir()
    out_html = work / f"{src.stem}.html"
    proc = subprocess.run(
        [cli, "view", str(src), "html", "-o", str(out_html)],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0 or not out_html.exists():
        shutil.rmtree(work, ignore_errors=True)
        raise RuntimeError(f"OfficeCLI 转换失败: {proc.stderr[:200] or proc.stdout[:200]}")
    html = out_html.read_text(encoding="utf-8", errors="ignore")
    final = out / f"{src.stem}_preview_{uuid.uuid4().hex[:8]}.html"
    final.write_text(_inline_images(work, html), encoding="utf-8")
    shutil.rmtree(work, ignore_errors=True)
    return str(final)


def _officecli_pdf(cli: str, src: Path, out: Path) -> str:
    work = out / f"_oc_{uuid.uuid4().hex[:8]}"
    work.mkdir()
    out_pdf = work / f"{src.stem}.pdf"
    proc = subprocess.run(
        [cli, "view", str(src), "pdf", "-o", str(out_pdf)],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0 or not out_pdf.exists():
        shutil.rmtree(work, ignore_errors=True)
        raise RuntimeError(f"OfficeCLI 转换失败: {proc.stderr[:200] or proc.stdout[:200]}")
    final = out / f"{src.stem}_{uuid.uuid4().hex[:8]}.pdf"
    shutil.move(str(out_pdf), str(final))
    shutil.rmtree(work, ignore_errors=True)
    return str(final)


async def to_html(path: str, out_dir: str, cache_dir: str | None = None) -> str:
    """docx → 单文件 HTML（OfficeCLI 优先，失败回退 LibreOffice）。

    2026-09-17：cache_dir=会话级预览缓存（跨 ask 复用，见 services/preview_cache.py）。
    """
    src = Path(path)
    if not src.exists():
        raise RuntimeError("源文档不存在")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    cached = resolve_hit(out, src, "preview", cache_dir)
    if cached:
        return cached

    cli = _cli_path()
    if cli:
        try:
            final = await asyncio.to_thread(_officecli_html, cli, src, out)
            stash(final, cache_dir)
            logger.info("OfficeCLI docx→HTML 预览生成: %s", Path(final).name)
            return final
        except Exception as e:
            logger.warning("OfficeCLI 转换失败，回退 LibreOffice: %s", str(e)[:120])

    from app.services.libreoffice import to_html as lo_to_html

    return await lo_to_html(path, out_dir, cache_dir)


async def to_pdf(path: str, out_dir: str, cache_dir: str | None = None) -> str:
    """docx/pptx → PDF（OfficeCLI 优先，失败回退 LibreOffice）。同 to_html：可传会话级缓存目录。"""
    src = Path(path)
    if not src.exists():
        raise RuntimeError("源文档不存在")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    cached = resolve_hit(out, src, "pdf", cache_dir)
    if cached:
        return cached

    cli = _cli_path()
    if cli:
        try:
            final = await asyncio.to_thread(_officecli_pdf, cli, src, out)
            stash(final, cache_dir)
            logger.info("OfficeCLI pptx→PDF 预览生成: %s", Path(final).name)
            return final
        except Exception as e:
            logger.warning("OfficeCLI 转换失败，回退 LibreOffice: %s", str(e)[:120])

    from app.services.libreoffice import to_pdf as lo_to_pdf

    return await lo_to_pdf(path, out_dir, cache_dir)
