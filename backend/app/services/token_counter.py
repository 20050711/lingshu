"""DeepSeek token 计费计数（需求 3，2026-08-17）。

- 计费主链路：LLM API 返回的 usage（prompt_tokens/completion_tokens，准确）→ 累计落库 sessions.cost_tokens
- 本模块：官方 tokenizer 本地估算兜底（API usage 缺失时，如异常截断/非标准客户端）——
  官方工具 deepseek_v3_tokenizer.zip（/data/tools/deepseek_tokenizer/deepseek_v3_tokenizer/tokenizer.json），
  用轻量 tokenizers 库加载（官方样例为 transformers，本项目避免重依赖）。
- 非 deepseek 模型（agnes 文本/GLM 多模态）：计费框显示"未知"，不计数（用户决策）。
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from app.core.config import get_settings

_settings = get_settings()

_TOKENIZER_PATH = Path(_settings.tools_data_dir) / "deepseek_tokenizer/deepseek_v3_tokenizer/tokenizer.json"


@lru_cache(maxsize=1)
def _tokenizer():
    """加载官方 tokenizer（惰性；缺失返回 None = 估算退化为启发式）。"""
    if not _TOKENIZER_PATH.exists():
        return None
    try:
        from tokenizers import Tokenizer

        return Tokenizer.from_file(str(_TOKENIZER_PATH))
    except Exception:
        return None


# 启发式兜底（tokenizer 不可用/非 deepseek 文本）：1 中文字符 ≈ 0.6 token，1 英文/数字字符 ≈ 0.3 token
_HEUR_CN = re.compile(r"[一-鿿　-〿＀-￯]")


def count_tokens(text: str) -> int:
    """估算文本 token 数（deepseek 语义）。tokenizer 可用时用官方工具；否则启发式。"""
    if not text:
        return 0
    tok = _tokenizer()
    if tok is not None:
        try:
            return len(tok.encode(text).ids)
        except Exception:
            pass
    cn = len(_HEUR_CN.findall(text))
    other = max(0, len(text) - cn)
    return int(cn * 0.6 + other * 0.3) or 1


def estimate_messages_tokens(messages: list[dict]) -> int:
    """估算消息列表 token 总数（usage 缺失兜底：content + 工具参数文本）。"""
    total = 0
    for m in messages:
        content = m.get("content") or ""
        total += count_tokens(str(content))
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            total += count_tokens(str(fn.get("name", "")) + str(fn.get("arguments", "")))
    return total
