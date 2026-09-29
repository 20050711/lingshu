"""通用文档索引层冒烟（2026-09-10）：格式嗅探 / 聊天切块 / 通用切块 / char_start / 解析分发。

纯单测（不打 DB、不打网络、不调 LLM）——刻意覆盖两类容易踩的坑：
1. **格式嗅探不写死**：md 与 txt 两种对话导出格式都要识别；普通文档不能误判
2. **char_start 落在原文坐标系**：LF 与 CRLF 文档都要准（CRLF 每处 \\r\\n 会让规范化偏移漂 1 字符）

用法：cd backend && source scripts/env_aip.sh && python tests/doc_index_smoke.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.doc_index import (  # noqa: E402
    chunk_chat,
    chunk_document,
    chunk_text,
    detect_chat,
    sniff_format,
)
from app.services.kb_chunker import chunk_text as kb_chunk_text  # noqa: E402

_RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    _RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")


# ---------- 夹具（两种真实导出格式） ----------

MD_CHAT = """## 2026-07-31
**系统**: "项目组负责人"邀请你和"张三"加入了群聊
**项目组负责人**: 周报参考
**托塔李天王**: 【文件】静和周报629-705.xlsx (media/file/静和周报629-705(1).xlsx)
**塔迪斯**: 原始数据有吗
**你听得见**: 在整理
**托塔李天王**: 我稍后把数据拆分发给他
**塔迪斯**: 那就是还有未知的数据源了
**项目组负责人**: ai可以识别、处理吧
## 2026-08-03
**塔迪斯**: 【文件】周报.xlsx (media/file/周报.xlsx)
**你听得见**: kos总账号数据聚光后台拉不出来的
**塔迪斯**: ok
"""

TXT_CHAT = """********* 2026-07-12 *********
系统: 50149608460@chatroom:
Joshua_: 肇庆
y_ryan: 哦哦
Jacob: 好嘟
Joshua_: 前面两个人都知道，但都不是我想要合作的
Jacob: 好滴 收到反馈
********* 2026-07-14 *********
Joshua_: 9:32 提醒一下今天下午的会
Jacob: 收到
y_ryan: 我来安排
"""

PLAIN_DOC = "# 员工手册\n\n公司实行标准工时制，每日工作八小时。\n\n## 报销\n\n报销需附发票，七个工作日到账。\n"
# 弱模式（无装饰的「名字: 内容」）误判压力测试：只有 3 行，低于阈值 15
WEAK_FALSE_POSITIVE = "会议纪要\n\n2026-01-01\n\n议题: 讨论预算\n时间: 下午三点\n地点: 会议室\n"


def test_sniff() -> None:
    record("嗅探：md 版聊天记录 → chat", sniff_format(MD_CHAT) == "chat")
    record("嗅探：txt 版聊天记录 → chat", sniff_format(TXT_CHAT) == "chat")
    record("嗅探：普通文档 → plain", sniff_format(PLAIN_DOC) == "plain")
    record("嗅探：短冒号文档不误判 → plain", sniff_format(WEAK_FALSE_POSITIVE) == "plain")
    record("嗅探：空文本不报错", sniff_format("") == "plain")
    record("嗅探：日期装饰多样化（=== 包裹）",
           sniff_format("=== 2026-01-01 ===\n" + "**A**: 内容行\n" * 6) == "chat")
    record("detect_chat 返回消息正则（非布尔）", hasattr(detect_chat(MD_CHAT), "findall"))


def _chat_case(text: str, label: str) -> None:
    blocks = chunk_document(text)
    record(f"[{label}] 切出块", len(blocks) >= 1, f"blocks={len(blocks)}")
    # 块标题带日期（检索时命中权重更高）
    has_day = any("2026-" in (b["block_title"] or "") for b in blocks)
    record(f"[{label}] 块标题带日期", has_day, blocks[0]["block_title"][:40] if blocks else "")
    # 块标题带发言人
    has_spk = any("/" in (b["block_title"] or "") for b in blocks)
    record(f"[{label}] 块标题带发言人", has_spk)
    # char_start 落在该块首条消息的起点（原文坐标系）
    ok_cs = True
    for b in blocks:
        at = text[b["char_start"]: b["char_start"] + 30]
        # 块正文 = [标题行] + 消息；正文里应能找到 char_start 处的开头片段
        frag = at.strip().split("\n")[0][:12]
        if frag and frag not in b["content"]:
            ok_cs = False
    record(f"[{label}] char_start 指向块首消息", ok_cs)
    # 消息不被拦腰砍断：每条消息行都以 **说话人** 或 说话人: 开头
    broken = 0
    for b in blocks:
        for line in b["content"].split("\n")[1:]:
            line = line.strip()
            if not line or line.startswith(("[", "###", "<!--", "|", "-", "\t", "「")):
                continue
            if not (line.startswith("**") or (":" in line[:20])):
                broken += 1
    record(f"[{label}] 消息行完整（未从中间截断）", broken <= 2, f"异常行={broken}")


def test_chat_chunk() -> None:
    _chat_case(MD_CHAT, "md导出")
    _chat_case(TXT_CHAT, "txt导出")

    # 重叠：回带上块最后一条完整消息（不是字符切一半）
    long_chat = "## 2026-07-31\n" + "".join(f"**发言人{i % 3}**: 这是第{i}条消息内容。\n" for i in range(200))
    blocks = chunk_document(long_chat)
    record("长聊天记录多块", len(blocks) >= 3, f"blocks={len(blocks)}")
    if len(blocks) >= 2:
        tail_msg = blocks[0]["content"].rstrip().split("\n")[-1]
        record("重叠回带完整消息（上块末条出现在下块开头）",
               tail_msg[:20] in blocks[1]["content"][:400], tail_msg[:30])

    # 超长单条消息：按标点硬切且每片补发言人前缀（需 ≥5 条消息才会被判为聊天记录）
    huge = ("## 2026-07-31\n" + "".join(f"**路人{i}**: 正常消息。\n" for i in range(6))
            + "**话痨**: " + "这是一句很长的话。" * 400)
    hb = chunk_document(huge)
    record("超长单条消息硬切", len(hb) >= 3, f"blocks={len(hb)}")
    huge_blocks = [b for b in hb if "话痨" in b["content"]]
    record("硬切片保留发言人前缀", len(huge_blocks) >= 3
           and all("**话痨**" in b["content"] for b in huge_blocks), f"话痨块={len(huge_blocks)}")


def test_generic_chunk() -> None:
    doc = "一、总则\n" + "制度说明内容。" * 60 + "\n\n二、细则\n" + "细则条款细节。" * 60
    a = chunk_text(doc)
    b = chunk_document(doc)
    record("通用策略与 kb_chunker 行为一致",
           [(x["content"], x["para_loc"]) for x in a] == [(x["content"], x["para_loc"]) for x in b])
    record("通用策略块含 char_start", all("char_start" in x for x in b))

    # char_start 在 LF / CRLF 两种原文下都落在块首
    for label, text in (("LF", doc), ("CRLF", doc.replace("\n", "\r\n"))):
        blocks = chunk_text(text)
        ok = all(
            text[x["char_start"]: x["char_start"] + 16].replace("\r\n", "\n")[:12] in x["content"]
            for x in blocks
        )
        record(f"char_start 原文坐标系（{label}）", ok,
               f"cs={[x['char_start'] for x in blocks]}")

    # 长段硬切：片偏移也在原文坐标系
    # 判据去掉空白差异（块正文是段落 join 的规范化渲染，空行被折叠为单个 \n）
    mixed = "标题\r\n\r\n" + "超长段落内容。" * 300
    blocks = chunk_text(mixed)
    def _flat(s: str) -> str:
        return s.replace("\r\n", "").replace("\n", "")

    ok = all(
        # 窗口不得超过该块自身长度（否则会越过短块、读到下一块的内容）
        _flat(mixed[x["char_start"]: x["char_start"] + min(24, len(x["content"]))])
        in _flat(x["content"])
        for x in blocks
    )
    record("长段硬切片内偏移正确", ok, f"blocks={len(blocks)}")

    # 超长「# 开头整段」不再被塞成巨块（header 分支原实现跳过硬切）
    overlong = "# 标题\n" + "正文内容。" * 600
    ob = chunk_text(overlong)
    record("# 开头超长整段按硬切分块", len(ob) >= 2 and all(len(x["content"]) <= 1100 for x in ob),
           f"blocks={len(ob)} lens={[len(x['content']) for x in ob]}")

    record("空文本不报错", chunk_document("") == [] and chunk_document("   ") == [])
    record("kb_chunk_text 兼容（旧引用）", len(kb_chunk_text(doc)) == len(a))


def test_chat_helper_direct() -> None:
    """chunk_chat 需显式传入消息正则（由 detect_chat 得出）。"""
    rex = detect_chat(MD_CHAT)
    blocks = chunk_chat(MD_CHAT, rex)
    record("chunk_chat 直调", len(blocks) >= 1 and blocks[0]["seq"] == 1)


def main() -> int:
    print("== 格式嗅探 ==")
    test_sniff()
    print("== 聊天记录切块 ==")
    test_chat_chunk()
    print("== 通用切块与 char_start ==")
    test_generic_chunk()
    print("== chunk_chat 直调 ==")
    test_chat_helper_direct()
    failed = [r for r in _RESULTS if not r[1]]
    print(f"\n== {len(_RESULTS) - len(failed)}/{len(_RESULTS)} PASS ==")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
