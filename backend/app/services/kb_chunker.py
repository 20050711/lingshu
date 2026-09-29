"""知识库分段器（KB-REDESIGN：块级存储与检索）。

- chunk_text：纯文本按标题结构/段落切块，500-1000 字/块，块间重叠 50-100 字防切断语义
- 纯同步函数；调用方以 asyncio.to_thread 包裹（M9 防阻塞约定）
"""
from __future__ import annotations

import bisect
import re

MIN_SIZE = 500
MAX_SIZE = 1000
OVERLAP = 50

# 标题识别：Markdown 标题 / 第N章(节/部分/篇) / 一、 / （一） / 1.
_HEADER_RE = re.compile(
    r"^(?:#{1,6}\s+"
    r"|第[一二三四五六七八九十百零〇\d]+[章节部分篇]"
    r"|[一二三四五六七八九十]+、"
    r"|[（(][一二三四五六七八九十]+[）)]"
    r"|\d+\.\s)"
)


def _normalize_pos(text: str) -> list[tuple[str, int, int]]:
    """按空行拆段落，返回 [(段落文本(行尾已规范为 \\n), 段首原文偏移, 段首规范化偏移)]。

    2026-09-10：偏移必须落在**原文**坐标系——agent 侧 file_parse 读到的是原始文本
    （txt/md/csv 原样带 \\r\\n），若按规范化文本给偏移，CRLF 文档每遇一个 \\r\\n 就漂 1 字符
    （聊天记录类 txt 全是 CRLF，一份 20 万字的记录能漂出几千字符）。
    折叠 \\r\\n→\\n 每处少 1 字符，故 原文偏移 = 规范化偏移 + 该位置之前的 \\r\\n 个数。
    切分口径与旧 _normalize 完全一致（按 \\n\\s*\\n 拆、strip、丢空段）。
    """
    crlf = [m.start() - i for i, m in enumerate(re.finditer(r"\r\n", text))]

    def orig(norm_pos: int) -> int:
        return norm_pos + bisect.bisect_left(crlf, norm_pos)

    norm = text.replace("\r\n", "\n").replace("\r", "\n")
    out: list[tuple[str, int, int]] = []
    cursor = 0
    for m in re.finditer(r"\n\s*\n", norm):
        raw = norm[cursor:m.start()]
        seg = raw.strip()
        if seg:
            nstart = cursor + raw.index(seg[0])   # 前缀必为空白 → 首个非空白位置
            out.append((seg, orig(nstart), nstart))
        cursor = m.end()
    raw = norm[cursor:]
    seg = raw.strip()
    if seg:
        nstart = cursor + raw.index(seg[0])
        out.append((seg, orig(nstart), nstart))
    return out


def _is_header(para: str) -> bool:
    return bool(_HEADER_RE.match(para))


def _split_long_para(para: str, max_size: int, overlap: int) -> list[tuple[str, int]]:
    """单段超长硬切：按句子边界切（避免句中切断语义），每片 ≤ max_size，片间重叠 overlap 字符。

    返回 [(文本片段, 片段在段内的起始偏移)]（2026-09-10）。
    """
    if len(para) <= max_size:
        return [(para, 0)]
    pieces: list[tuple[str, int]] = []
    rest = para
    base = 0
    while len(rest) > max_size:
        cut = rest[:max_size]
        idx = max(cut.rfind("。"), cut.rfind("！"), cut.rfind("？"), cut.rfind("；"), cut.rfind("；"))
        if idx < max_size * 0.6:
            idx = max_size  # 边界太靠前 → 硬切点
        else:
            idx += 1
        pieces.append((cut[:idx], base))
        shift = max(0, idx - overlap)
        base += shift
        rest = rest[shift:]
    if rest:
        pieces.append((rest, base))
    return pieces


def chunk_text(
    text: str,
    min_size: int = MIN_SIZE,
    max_size: int = MAX_SIZE,
    overlap: int = OVERLAP,
) -> list[dict]:
    """全文分块：按标题结构/段落贪心组装，500-1000 字/块，块间重叠 50-100 字。

    返回 [{seq, block_title, content, para_loc, char_start}]：
    - block_title：块所属的最近标题（无标题则为 ""）
    - para_loc：块内段落序号区间（源文从 1 计，如 "p12-p18"）；char_start 为块首字符偏移
    - char_start：块首段落在规范化后全文中的字符偏移（**不含拼接的 overlap 前缀**），
      供 agent 用 file_parse(path, offset=char_start, length=...) 精确续读（2026-09-10）
    """
    paras = _normalize_pos(text)
    if not paras:
        return []
    # 规范化偏移 → 原文偏移（\\r\\n 折叠每处少 1 字符；长段硬切片内偏移也要换算）
    _crlf = [m.start() - i for i, m in enumerate(re.finditer(r"\r\n", text))]

    def _orig(norm_pos: int) -> int:
        return norm_pos + bisect.bisect_left(_crlf, norm_pos)

    blocks: list[dict] = []
    cur_title = ""
    cur_parts: list[str] = []
    cur_len = 0
    loc_start = loc_end = 0
    cur_start = 0
    next_overlap = ""

    def push_block(content: str, loc_s: int, loc_e: int, char_start: int) -> None:
        """写入一个块；next_overlap 拼在块开头（上一块末尾 overlap 字符防切断语义）。"""
        nonlocal next_overlap
        if not content.strip():
            return
        if next_overlap:
            content = next_overlap + "\n" + content
        blocks.append(
            {
                "seq": len(blocks) + 1,
                "block_title": cur_title,
                "content": content,
                "para_loc": f"p{loc_s}-p{loc_e}" if loc_s != loc_e else f"p{loc_s}",
                "char_start": char_start,
            }
        )
        next_overlap = content[-overlap:] if overlap else ""

    def flush() -> None:
        nonlocal cur_parts, cur_len, loc_start
        if not cur_parts:
            return
        push_block("\n".join(cur_parts), loc_start, loc_end, cur_start)
        cur_parts, cur_len = [], 0

    para_no = 0
    for para, off, nstart in paras:
        para_no += 1
        if _is_header(para):
            # 标题：先收当前块，再以新标题开块（标题行并入内容，检索时块标题+正文一体）
            # 注意：段落可能含标题行后的正文（单换行未拆段），block_title 只取首行（标题行），防超长
            flush()
            cur_title = para.split("\n")[0][:200]
            if len(para) > max_size:
                # 2026-09-10（doc_index_smoke 实锤）：`#` 开头且整段超长时原实现直接塞成巨块
                # （长段硬切分支被 continue 跳过）——按硬切分支处理，每片沿用本标题
                for piece, poff in _split_long_para(para, max_size, overlap):
                    push_block(piece, para_no, para_no, _orig(nstart + poff))
                cur_parts, cur_len = [], 0
                continue
            cur_parts.append(para)
            cur_len = len(para)
            loc_start = loc_end = para_no
            cur_start = off
            continue
        if len(para) > max_size:
            # 单段超长：硬切成多片，每片独立成块（沿用最近标题，段落定位相同）
            flush()
            for piece, poff in _split_long_para(para, max_size, overlap):
                push_block(piece, para_no, para_no, _orig(nstart + poff))
            cur_parts, cur_len = [], 0
            continue
        if not cur_parts:
            loc_start = para_no
            cur_start = off
        if cur_len + len(para) > max_size and cur_len >= min_size:
            flush()
            loc_start = para_no
            cur_start = off
        cur_parts.append(para)
        cur_len += len(para)
        loc_end = para_no
    flush()
    return blocks
