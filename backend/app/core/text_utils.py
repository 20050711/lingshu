"""文本工具：emoji 过滤（对话回复禁 emoji，UI 界面除外）+ 工具调用 XML 剥离。"""
from __future__ import annotations

import json
import re

# emoji 主要 Unicode 范围（含变体选择符/旗帜/符号）
EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"      # 杂项符号与象形文字等
    "\U00002600-\U000027BF"      # 杂项符号/装饰符号
    "\U0001F1E6-\U0001F1FF"      # 区域指示符（旗帜）
    "\U00002B00-\U00002BFF"      # 杂项符号箭头
    "\U0000FE0F"                 # 变体选择符
    "\U00002764\U00002763"       # 心形等
    "\U0001F900-\U0001F9FF"      # 补充符号
    "]+"
)


def parse_llm_json(text: str):
    """解析 LLM 输出为 JSON（2026-09-01 统一容错——免费档 agnes/glm 思考关会输出 ```json markdown 包装）。

    依次尝试：直接解析 → 剥 markdown 代码块（```json/```）→ 提取首个 { 到最后一个 }。
    成功返回 dict/list；全部失败抛 ValueError（调用方降级/报错）。
    """
    if not text or not text.strip():
        raise ValueError("LLM 输出为空，无法解析 JSON")
    t = text.strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", t, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    s, e = t.find("{"), t.rfind("}")
    if s != -1 and e > s:
        try:
            return json.loads(t[s:e + 1])
        except Exception:
            pass
    raise ValueError(f"无法解析 LLM JSON 输出: {t[:100]}")


def strip_emoji(text: str) -> str:
    """剔除文本中的 emoji（保留中英文与标点）。"""
    return EMOJI_RE.sub("", text or "")


# 工具调用 XML 幻觉块（2026-08-07：DeepSeek 偶发把 tool_calls 以 XML 文本写进 content，
# 必须从所有面向用户的输出中剥离——chat/verify/计划反问轮 + 前端渲染双保险）
# 2026-08-17（问题 3 横向扩展）：+ <function=...> 形态的工具幻觉（实测 <function=todo_step>...）、
# + 含工具标签的 markdown 代码围栏（LLM 收尾轮把想调的工具写进代码块）
_TOOL_XML_BLOCKS = [
    (re.compile(r"<tool_calls>.*?</tool_calls>", re.S), ""),
    (re.compile(r"<invoke name=.*?</invoke>", re.S), ""),
    (re.compile(r"<parameter name=.*?</parameter>", re.S), ""),
    (re.compile(r"</?(?:invoke|parameter|tool_calls|tool_call|search_query)[^>]*>", re.I), ""),
    (re.compile(r"<function=[^>]*>.*?</function>", re.S), ""),
    (re.compile(r"</?function[^>]*>", re.I), ""),
    # 代码围栏内含工具调用格式（收尾轮幻觉的另一种载体；正常教学代码块不受影响）
    (re.compile(r"```[a-z]*\s*<tool_calls>.*?```", re.S), ""),
    (re.compile(r"```[a-z]*\s*<function=.*?```", re.S), ""),
]


# 工具调用标记子串（不依赖闭合标签——幻觉常为未闭合截断，正则无法剥离）
_TOOL_MARKUP_FRAGMENTS = ("<tool_calls", "</tool_calls", "<invoke", "<function=", "<parameter name", "<parameter ")


def sanitize_db_text(s: str) -> str:
    """入库文本清洗（2026-08-27 提取公共版，原 kb_service 私有实现）：NUL（PG UTF8 拒绝 0x00
    字节，昨天 kb 上传 500 实锤）+ 非法 surrogate（asyncpg 编码拒绝）统一剔除。
    所有用户可控/外部来源文本入库路径必须过此函数（kb 上传 / xlsx 导入 / 转写文本）。"""
    return s.replace("\x00", "").encode("utf-8", errors="ignore").decode("utf-8")


def read_text_any_encoding(raw: bytes) -> str:
    """文本解码（2026-08-27 乱码全面修复）：UTF-8 strict → GB18030（覆盖 GBK/GB2312）→ UTF-16 → 兜底忽略。
    国内用户 txt/csv 常见 GBK 编码（Excel 导出/记事本 ANSI），原硬编码 UTF-8 解码产生乱码
    （kb 上传 + 会话文件解析两条链路实锤）。"""
    for enc in ("utf-8", "gb18030", "utf-16"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="ignore")


def detect_binary_text(raw: bytes) -> bool:
    """文本内容二进制特征检测（2026-08-27：zip 改名 .md 上传实锤——按文本解码产生乱码入库）。
    zip/gzip 魔数直接命中；NUL 比例 >2%（排除 UTF-16 文本——其 NUL 是正常结构）。"""
    if len(raw) < 4:
        return False
    if raw[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08") or raw[:3] == b"\x1f\x8b\x08":
        return True
    is_utf16 = raw[:2] in (b"\xff\xfe", b"\xfe\xff")
    if is_utf16:
        return False
    return raw.count(b"\x00") / len(raw) > 0.02


def _normalize_tool_markup(text: str) -> str:
    """2026-08-20（走查实锤）：LLM 幻觉变体伪装标签绕过全部 ASCII 检测/剥离正则——
    ① 全角竖线（U+FF5C）插在标签内：`<｜tool_calls｜>`；② DeepSeek DSML 前缀：
    `<｜DSML｜｜tool_calls>`（开/闭标签均可能带）。归一化：移除标签内（< 或 </ 之后）
    的全角竖线与 DSML 前缀，使既有 contains/strip 生效。"""
    t = re.sub(r"(?<=<)[｜]*(?:DSML)?[｜]*", "", text)
    t = re.sub(r"(?<=</)[｜]*(?:DSML)?[｜]*", "", t)
    return t


def contains_tool_markup(text: str) -> bool:
    """子串级检测是否含工具调用标记（2026-08-17 绝对方案：不依赖正则闭合匹配——
    幻觉输出常为未闭合截断；命中即整段作废，而非剥离残余）。"""
    if not text:
        return False
    return any(frag in _normalize_tool_markup(text) for frag in _TOOL_MARKUP_FRAGMENTS)


def strip_tool_xml(content: str) -> str:
    """剥离 LLM 幻觉输出的工具调用 XML/函数标记（标签块 + 残留标签 + 工具代码块），防"工具链吐给用户"。"""
    text = _normalize_tool_markup(content or "")
    for pattern, _ in _TOOL_XML_BLOCKS:
        text = pattern.sub("", text)
    return text.strip()


def truncate_head_tail(text: str, budget: int) -> str:
    """长文本注入 LLM 上下文的预算截断：头部 2/3 + 尾部 1/3 保留，中间省略标注。

    2026-08-25（媒体理解/会议纪要共用）：用于转写稿、GLM 理解输出等注入前的预算控制——
    只截断"注入 LLM 的副本"，落盘产物（语音转写.md / subtask_*.md / result.md）始终用原文。
    尾部保留用于保住结尾的综合总结/结论；短于预算原样返回。
    """
    if not text or len(text) <= budget:
        return text
    head = int(budget * 2 / 3)
    tail = budget - head
    return f"{text[:head]}\n……（中间 {len(text) - budget} 字符已省略，详见落盘原文）……\n{text[-tail:]}"
