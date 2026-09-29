"""站点经验：按域名攒的**笔记**（机器读的配置在 `profiles/`，这里是给 agent 看的经验）。

做法借鉴 web-access 的 site-patterns（2026-09-24 评审后采纳）：
- 任务中发现的、**验证过的**事实（URL 结构、该点哪个入口、哪些按钮会弹窗、额度提示原文…）记一笔；
- 下次再到这个站点，`browse_open` 会自动把它顶在快照开头——省得每次从零摸索；
- 每条带日期，当好用的**提示**看，不当作保证；与实测不符就以实测为准，并把它改掉。

存哪儿：`<home>/site-notes/<域名>.md`（home 默认 `%LOCALAPPDATA%\\agent-browser`）。
用户/agent 也可以直接用文件工具读写这些 md——工具只在"到达站点时读一次"和"记一笔"上帮忙。
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from urllib.parse import urlparse

from . import config

MAX_CHARS = 1500        # 顶在快照开头时的上限，别让笔记挤掉正文


def notes_dir() -> Path:
    d = config.home_dir() / "site-notes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def domain_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def path_for(url: str) -> Path | None:
    """域名 → 笔记文件。只接受干净的域名（防路径穿越）。"""
    d = domain_of(url)
    if not d or re.search(r"[^a-z0-9.\-]", d):
        return None
    return notes_dir() / f"{d}.md"


def read(url: str) -> str:
    """这个站点已有的经验（没有就是空串）。"""
    p = path_for(url)
    if not p or not p.is_file():
        return ""
    try:
        return p.read_text(encoding="utf-8").strip()[:MAX_CHARS]
    except OSError:
        return ""


def append(url: str, fact: str) -> str:
    """记一笔（追加一行，带日期）。返回文件路径。"""
    p = path_for(url)
    if p is None:
        raise ValueError("当前页面不是 http(s) 站点，记不了经验")
    fact = " ".join(str(fact).split())[:400]
    if not fact:
        raise ValueError("要记的内容是空的")
    fresh = not p.exists()
    with open(p, "a", encoding="utf-8") as f:
        if fresh:
            f.write(f"# 站点经验：{domain_of(url)}\n\n"
                    f"> 任务中攒下的、**验证过的**事实。带日期，当提示看；"
                    f"与实测不符时以实测为准，并把这里改掉。\n\n")
        f.write(f"- [{time.strftime('%Y-%m-%d')}] {fact}\n")
    return str(p)
