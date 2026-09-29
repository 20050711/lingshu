"""预览转换缓存（4.1 抽取）：LibreOffice 与 OfficeCLI 适配器共用 mtime 缓存。

命中规则：{stem}_preview_*.html 或 {stem}_*.pdf 且产物 mtime >= 源文件 mtime → 复用。
两引擎产物共用同一命名空间，切换引擎也能命中缓存。

2026-09-17（缓存审计 P1）：**加会话级缓存目录**——原缓存只落在"本轮产出目录"
（`{output_dir}/{session}/{round}`），换个 ask 再看同一份 pptx 就是新目录、缓存必然落空，
LibreOffice 又要跑 30 秒级。现在：
- 查缓存时**本轮目录 + 会话级缓存目录都查**；
- 命中会话级缓存 → 拷回本轮目录再用（**产出 URL/鉴权链路不变**，只是省掉转换）；
- 新转换出的产物顺手存一份进会话级缓存，供后续轮次复用。
会话删除时整目录一起回收（输出目录本就按会话清理）。
"""
from __future__ import annotations

import shutil
from pathlib import Path

PREVIEW_CACHE_SUBDIR = "_preview_cache"


def cache_dir_for(session_out_root: Path | str) -> Path:
    """会话级预览缓存目录：{output_dir}/{session}/_preview_cache。"""
    return Path(session_out_root) / PREVIEW_CACHE_SUBDIR


def find_cached(out: Path, src: Path, kind: str) -> str | None:
    """查找转换缓存：kind=preview 匹配 {stem}_preview_*.html；kind=pdf 匹配 {stem}_*.pdf。"""
    if kind == "preview":
        candidates = sorted(out.glob(f"{src.stem}_preview_*.html"), key=lambda p: p.stat().st_mtime, reverse=True)
    else:
        candidates = sorted(out.glob(f"{src.stem}_*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in candidates:
        try:
            if p.stat().st_mtime >= src.stat().st_mtime:
                return str(p)
        except OSError:
            continue
    return None


def resolve_hit(out: Path, src: Path, kind: str, cache_dir: Path | str | None) -> str | None:
    """查缓存（本轮目录优先，其次会话级）→ 返回**本轮目录内的**可用路径。

    命中会话级缓存时拷一份回本轮目录：产物 URL 仍是 `{session}/{round}/{name}`，
    前端与 outputs 鉴权链路零改动。
    """
    hit = find_cached(out, src, kind)
    if hit:
        return hit
    if not cache_dir:
        return None
    hit = find_cached(Path(cache_dir), src, kind)
    if not hit:
        return None
    try:
        target = out / Path(hit).name
        if not target.exists():
            out.mkdir(parents=True, exist_ok=True)
            shutil.copy2(hit, target)
        return str(target)
    except OSError:
        return hit  # 拷贝失败也把命中路径返回（转换才是贵的）


def stash(final: str, cache_dir: Path | str | None) -> None:
    """新产物存一份进会话级缓存（失败静默——缓存不该影响主流程）。"""
    if not cache_dir:
        return
    try:
        cdir = Path(cache_dir)
        cdir.mkdir(parents=True, exist_ok=True)
        dst = cdir / Path(final).name
        if not dst.exists():
            shutil.copy2(final, dst)
    except OSError:
        pass
