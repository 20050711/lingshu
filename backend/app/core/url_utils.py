"""产出物/子接口 URL 构造（M4：统一走 settings.api_prefix，杜绝散落硬编码）。

消费方：agent/tools/*（doc_tools/html_report/media_tools/skills）、api/chat.py、api/tools_video.py。
放 core/ 的理由：所有消费方已依赖 app.core.config，不新增层间耦合。
"""
from __future__ import annotations

from app.core.config import get_settings


def _prefix() -> str:
    # get_settings() 有 lru_cache；调用时取值（而非模块导入时），API_PREFIX 变更即时生效
    return get_settings().api_prefix.rstrip("/")


def output_url(session_id: str, round_id: int | str, filename: str) -> str:
    """产出物下载 URL（/api/v1/outputs/{session_id}/{round_id}/{filename}）。

    2026-09-15（走查同类排查）：文件名**必须百分号编码**——含 `#`/`?`/`%` 的产出名
    （上传原名可带，如 "报告#2.xlsx" 派生出的产出）会让 URL 被浏览器按"片段"截断 → 404
    （实测：`报告%232.md` → 200，裸 `报告#2.md` → 404）。解码端：
    agent/tools/__init__.resolve_output_url 与 api/chat.py doc_preview（均 unquote）。
    """
    from urllib.parse import quote

    return f"{_prefix()}/outputs/{session_id}/{round_id}/{quote(str(filename), safe='/')}"


def api_url(path: str) -> str:
    """以 API 前缀拼接子接口 URL（path 以 / 开头）。"""
    p = path if path.startswith("/") else f"/{path}"
    return f"{_prefix()}{p}"
