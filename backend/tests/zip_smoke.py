"""会话 zip 冒烟测试（2026-08-21）：防御解压矩阵 / 上传链路 / zip_extract / zip_pack。

用法: cd backend && conda run -n aip python -u tests/zip_smoke.py
覆盖：
1. safe_extract_zip 防御矩阵（zip-slip/绝对路径/反斜杠/符号链接/CRC/超限/深度 → 整体拒绝且零残留）
2. build_zip_bytes 打包
3. zip_extract / zip_pack handler 直调（越权拒绝 / 正常打包 output_url 可解析）
4. 工具注册冒烟（zip_extract/zip_pack 非 None，_INTERNAL_TOOLS 隐藏于元数据）
5. 归档名净化（Windows 非法字符/逃逸/撞名去重——2026-09-10 走查：含 | 的 zip 在 Windows 打开是空的）
6. 解压跳过 macOS 垃圾（.DS_Store/._*/__MACOSX——2026-09-11 走查：4GB 包 134 个垃圾入库）
7. zip 内黑名单成员跳过 + 《上传跳过清单.md》（2026-09-16；结构类异常仍整体拒绝）
"""
from __future__ import annotations

import asyncio
import io
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.tools import ToolContext, get_all_tool_meta, get_tool
from app.core.config import get_settings
from app.services.zip_utils import build_zip_bytes, safe_extract_zip

_results: list[tuple[str, bool, str]] = []
_settings = get_settings()


def record(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, ok, detail))
    print(f"{'✓' if ok else '✗'} {name}" + (f" — {detail[:120]}" if detail else ""))


def make_zip(members: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members:
            zf.writestr(name, data)
    return buf.getvalue()


def make_zip_with_link() -> bytes:
    """构造含符号链接成员的 zip（external_attr 置 symlink 位）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        info = zipfile.ZipInfo("evil_link")
        info.create_system = 3  # Unix
        info.external_attr = (0o120777 << 16)  # symlink 模式
        zf.writestr(info, "/etc/passwd")
    return buf.getvalue()


def run_macos_junk_filter() -> None:
    """解压跳过 macOS 打包垃圾（2026-09-11 走查：4GB 包 134 个 .DS_Store 入库）。
    只验 zip_utils.safe_extract_zip_disk（知识库上传走的那条磁盘流式链路）。"""
    from app.services.customer_zip import is_macos_junk, safe_extract_zip_disk

    record("垃圾判定：__MACOSX/._*/.DS_Store 为真，普通文件（含 .hidden）为假",
           is_macos_junk("__MACOSX/._a.pdf") and is_macos_junk("a/.DS_Store")
           and is_macos_junk("sub/._报告.pdf") and is_macos_junk("._.DS_Store")
           and not is_macos_junk("报告.pdf") and not is_macos_junk(".hidden.txt")
           and not is_macos_junk("a/b/c.txt"))
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        # zip 与 zipfile.writestr 都自动补目录项；这里显式给全
        raw = make_zip([
            ("__MACOSX/._real.txt", b"junk"),
            ("__MACOSX/sub/._real2.txt", b"junk"),
            ("folder/.DS_Store", b"junk"),
            ("folder/._real.txt", b"junk"),
            ("folder/real.txt", b"hello"),
            ("folder/真实文件.docx", b"world"),
        ])
        zp = td_path / "x.zip"
        zp.write_bytes(raw)
        names, _total, _skipped = safe_extract_zip_disk(zp, td_path / "out", 10**7, 100, 10)
        record("解压后仅真实文件落盘（垃圾 4 个被跳过）",
               sorted(names) == ["folder/real.txt", "folder/真实文件.docx"], str(names))
        record("磁盘上无垃圾残留（.DS_Store/._*/__MACOSX）",
               not (td_path / "out" / "__MACOSX").exists()
               and not (td_path / "out" / "folder" / ".DS_Store").exists()
               and not (td_path / "out" / "folder" / "._real.txt").exists())


def run_exec_skip_manifest() -> None:
    """zip 内黑名单成员：跳过 + 生成《上传跳过清单.md》（2026-09-16，替代原"整包拒绝"）。

    背景：客户聊天记录包常带企业小工具（.exe），整包拒绝 = 一份资料都拿不到。
    新语义：黑名单成员**跳过不落盘** + 清单落当前层；结构类异常（路径穿越等）仍整体拒绝。
    """
    from app.services.customer_zip import is_executable_name, safe_extract_zip_disk, write_skip_manifest

    record("黑名单扩充生效（载荷类拦住）",
           all(is_executable_name(n) for n in (
               "a.exe", "b.msu", "c.psm1", "d.wsf", "e.deb", "f.appimage", "g.dll",
               "h.xll", "i.dotm", "j.dex", "k.lnk", "l.command"))
           and not any(is_executable_name(n) for n in (
               "报告.docx", "数据.xlsx", "工具.py", "页面.html", "说明.txt", "a.psd")),
           "含 .py/.html 等业务格式应放行")

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        zp = td_path / "包.zip"
        zp.write_bytes(make_zip([
            ("资料/报告.docx", b"legal"),
            ("工具/客户端.exe", b"MZ fake"),
            ("数据.bin", b"binary"),
            ("run.sh", b"#!/bin/sh"),
        ]))
        out = td_path / "out"
        names, _total, skipped = safe_extract_zip_disk(zp, out, 10**7, 100, 10, source="包.zip")
        record("黑名单成员跳过、其余正常解压",
               names == ["资料/报告.docx"] and len(skipped) == 3,
               f"落盘={names} 跳过={len(skipped)}")
        record("磁盘上无被跳过的可执行文件（安全属性不变）",
               (out / "资料" / "报告.docx").is_file()
               and not (out / "工具").exists() and not (out / "数据.bin").exists()
               and not (out / "run.sh").exists())
        mf = write_skip_manifest(out, skipped)
        txt = mf.read_text(encoding="utf-8") if mf else ""
        record("清单落盘并列出被跳过文件 + 原因 + 来源",
               bool(mf) and mf.name == "上传跳过清单.md"
               and all(k in txt for k in ("客户端.exe", "数据.bin", "run.sh", "可执行/载荷文件", "包.zip")),
               txt.splitlines()[2][:80] if txt else "（未生成）")

        # markdown 注入/串行：文件名含 | 与换行必须仍是一行三列
        mf2 = write_skip_manifest(td_path, [("a|b.zip", "x\n|y.exe", "可执行/载荷文件（.exe）")])
        rows = [l for l in mf2.read_text(encoding="utf-8").splitlines() if l.startswith("| `")]
        record("清单转义：文件名含 | 与换行不破表格",
               bool(rows) and all(r.count("|") == 4 for r in rows), str(rows[:1]))

        # 结构类异常仍整体拒绝（跳过语义只覆盖黑名单，不放行恶意包）
        (td_path / "evil.zip").write_bytes(make_zip([("ok.txt", b"x"), ("../evil.txt", b"x")]))
        try:
            safe_extract_zip_disk(td_path / "evil.zip", td_path / "evil_out", 10**7, 100, 10)
            record("路径穿越仍整体拒绝（未被跳过语义放行）", False, "未拒绝！")
        except ValueError:
            record("路径穿越仍整体拒绝（未被跳过语义放行）", True)


def run_extract_matrix() -> None:
    """防御矩阵：非法 zip 整体拒绝且目标目录无残留。"""
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)

        # 正常解压
        ok_zip = make_zip([("a.txt", b"hello"), ("sub/b.txt", b"world")])
        out: list[str] = []
        err = ""
        try:
            out = safe_extract_zip(ok_zip, td_path / "ok", 10**6, 100, 10)
            good = sorted(out) == ["a.txt", "sub/b.txt"] and (td_path / "ok" / "sub" / "b.txt").read_bytes() == b"world"
        except Exception as e:      # 2026-09-10 同类排查：原在 except 外引用 e → 失败路径反被 NameError 盖掉
            good = False
            err = repr(e)
        record("正常解压", good, str(out) if good else err)

        # zip-slip：../ 与绝对路径与反斜杠
        for bad_name in ("../evil.txt", "/abs.txt", "..\\win_evil.txt"):
            try:
                safe_extract_zip(make_zip([(bad_name, b"x")]), td_path / "slip", 10**6, 100, 10)
                record(f"zip-slip 拒绝（{bad_name}）", False, "未拒绝！")
            except ValueError:
                record(f"zip-slip 拒绝（{bad_name}）", True)
        record("zip-slip 零残留", not (td_path / "slip").exists())

        # 符号链接成员
        try:
            safe_extract_zip(make_zip_with_link(), td_path / "lnk", 10**6, 100, 10)
            record("符号链接成员拒绝", False, "未拒绝！")
        except ValueError:
            record("符号链接成员拒绝", True)

        # CRC 损坏（改一个字节）
        bad_zip = bytearray(ok_zip)
        bad_zip[30] ^= 0xFF  # 破坏压缩数据区
        try:
            safe_extract_zip(bytes(bad_zip), td_path / "crc", 10**6, 100, 10)
            record("CRC 损坏拒绝", False, "未拒绝！")
        except ValueError:
            record("CRC 损坏拒绝", True)

        # 文件数超限（101 个 > 100）
        try:
            safe_extract_zip(make_zip([(f"f{i}.txt", b"x") for i in range(101)]), td_path / "cnt", 10**6, 100, 10)
            record("文件数超限拒绝", False, "未拒绝！")
        except ValueError:
            record("文件数超限拒绝", True)

        # 总量超限（3 字节 > 2 字节上限）
        try:
            safe_extract_zip(make_zip([("a.txt", b"123")]), td_path / "tot", 2, 100, 10)
            record("总量超限拒绝", False, "未拒绝！")
        except ValueError:
            record("总量超限拒绝", True)

        # 深度超限（a/b/c/d.txt 深度 3 > 2）
        try:
            safe_extract_zip(make_zip([("a/b/c/d.txt", b"x")]), td_path / "dep", 10**6, 100, 2)
            record("深度超限拒绝", False, "未拒绝！")
        except ValueError:
            record("深度超限拒绝", True)

        # 非 zip 字节
        try:
            safe_extract_zip(b"not a zip at all", td_path / "bad", 10**6, 100, 10)
            record("非 zip 字节拒绝", False, "未拒绝！")
        except ValueError:
            record("非 zip 字节拒绝", True)


async def run_tool_handlers() -> None:
    """zip_extract / zip_pack handler 直调（产出目录内源文件，白名单可读）。"""
    from app.agent.tools.zip_tools import run_zip_extract, run_zip_pack

    with tempfile.TemporaryDirectory() as td:
        out_root = Path(td)
        ctx = ToolContext("smoke-zip", 1, "demo", "employee", "smoke", str(out_root), user_id=1)

        # 越权路径（系统文件）→ 拒绝
        r = await run_zip_extract({"file_path": "/etc/passwd"}, ctx)
        record("zip_extract 越权路径拒绝", bool(r.get("error")), r.get("error", "")[:80])

        # 正常解压：源 zip 放产出目录（白名单内）
        src_zip = out_root / "data.zip"
        src_zip.write_bytes(make_zip([("x.txt", b"hi"), ("d/y.txt", b"yo")]))
        r = await run_zip_extract({"file_path": str(src_zip)}, ctx)
        good = not r.get("error") and r.get("count") == 2 and Path(r["target_dir"]).is_dir()
        record("zip_extract 正常解压", good, str(r.get("error") or r.get("target_dir"))[:100])
        unzip_root = Path(r["target_dir"]) if not r.get("error") else out_root

        # 二次解压：2026-09-08 起默认目录已存在 → **不重复解压**，直接返回既有清单
        # （原行为递增成 _unzip2 再解一遍，浪费且 agent 易读到旧副本；此处断言现行契约）
        r2 = await run_zip_extract({"file_path": str(src_zip)}, ctx)
        good2 = (not r2.get("error")
                 and r2.get("target_dir") == str(unzip_root)
                 and r2.get("count", 0) >= 1
                 and "未重复解压" in (r2.get("note") or ""))
        record("zip_extract 二次解压复用既有目录（不重解）", good2,
               f"dir={r2.get('target_dir')} count={r2.get('count')}")
        # 显式 target_dir 仍可解到新目录（覆盖"确需重新解压"的出口）
        alt = src_zip.parent / "data_alt_unzip"
        r3 = await run_zip_extract({"file_path": str(src_zip), "target_dir": str(alt)}, ctx)
        record("zip_extract 显式 target_dir 解到新目录",
               (not r3.get("error")) and r3.get("target_dir") == str(alt) and alt.is_dir(),
               str(r3.get("target_dir"))[:80])

        # 越权打包（/etc/passwd）→ 拒绝
        r = await run_zip_pack({"entries": [{"path": "/etc/passwd"}]}, ctx)
        record("zip_pack 越权路径拒绝", bool(r.get("error")), r.get("error", "")[:80])

        # 正常打包：产出目录内两个文件
        (out_root / "a.txt").write_text("A")
        (out_root / "b.txt").write_text("B")
        r = await run_zip_pack({"entries": [{"path": str(out_root / "a.txt")}, {"path": str(out_root / "b.txt")}]}, ctx)
        good = not r.get("error") and r.get("file_path", "").startswith("/api/v1/outputs/")
        if good:
            import re
            from app.core.url_utils import output_url
            m = re.search(r"outputs/([^/]+)/([^/]+)/([^/]+)$", r["file_path"])
            good = m is not None and (out_root / m.group(3)).exists()
        record("zip_pack 正常打包 → output_url 文件存在", good, r.get("file_path", "")[:80])

        # 目录递归打包（用解压出的 data_unzip/d 目录）
        r = await run_zip_pack({"entries": [{"path": str(unzip_root / "d")}], "zip_name": "dir.zip"}, ctx)
        good = not r.get("error") and r.get("file_count", 0) >= 1
        record("zip_pack 目录递归", good, str(r.get("error") or r.get("file_count")))

        # 归档名注入（../ 拒绝）
        r = await run_zip_pack({"entries": [{"path": str(out_root / "a.txt"), "name": "../evil.txt"}]}, ctx)
        record("zip_pack 归档名注入拒绝", bool(r.get("error")), r.get("error", "")[:60])

        # build_zip_bytes 冒烟
        data = build_zip_bytes([(out_root / "a.txt", "a.txt")])
        record("build_zip_bytes 打包", len(data) > 0 and data[:2] == b"PK")


def run_archive_name_sanitize() -> None:
    """归档名净化（2026-09-10 走查 bug 回归）。

    现象：示例站点记录标题含半角 `|`（如「拼豆日记📔郁金香小羊🌷| 附图纸」）→ 归档目录名带
    Windows 非法字符 → 用户下载的 zip 在 Windows 资源管理器里**打不开、显示为空**
    （第一轮标题只含全角「？」故正常）。修复：打包层统一净化归档名。
    """
    from app.core.file_utils import sanitize_zip_member
    from app.services.customer_zip import build_zip_file

    # 单元：非法字符/尾点尾空格/.. 逃逸/空值
    cases = [
        ("a|b:c?d*e\"f<g>h.txt", "a_b_c_d_e_f_g_h.txt"),
        ("拼豆日记📔郁金香小羊🌷| 附图纸/图1.jpeg", "拼豆日记📔郁金香小羊🌷_ 附图纸/图1.jpeg"),
        ("dir /sub/../evil.txt", "dir/sub/evil.txt"),
        ("trail. / x. ", "trail/x"),        # 每段去首尾空格 + 去 Windows 禁止的尾点
        ("", "file"),
    ]
    ok = all(sanitize_zip_member(a) == b for a, b in cases)
    record("sanitize_zip_member 规则（非法字符/逃逸/尾点/空值）", ok,
           str([(a, sanitize_zip_member(a)) for a, b in cases if sanitize_zip_member(a) != b]))

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "src").mkdir()
        (root / "src" / "f1.txt").write_text("1")
        (root / "src" / "f2.txt").write_text("2")
        bad = "xhs/6a7d_作者_标题|带竖线/图1.jpeg"
        # 1) build_zip_file（磁盘流式，xhs/客户库/zip_pack 共用）
        out = root / "a.zip"
        build_zip_file([(root / "src" / "f1.txt", bad)], out, 10 * 1024 * 1024)
        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
        record("build_zip_file 归档名已净化（Windows 可解压）",
               names and all(c not in names[0] for c in '|:?*"<>'), str(names))
        # 2) 净化后撞名 → 去重后缀（否则解压互相覆盖）
        out2 = root / "b.zip"
        build_zip_file([(root / "src" / "f1.txt", "a|b.txt"), (root / "src" / "f2.txt", "a?b.txt")],
                       out2, 10 * 1024 * 1024)
        with zipfile.ZipFile(out2) as zf:
            n2 = zf.namelist()
        record("净化后撞名去重（_2 后缀）", len(n2) == 2 and len(set(n2)) == 2, str(n2))
        # 3) build_zip_bytes（内存版）同净化
        data = build_zip_bytes([(root / "src" / "f1.txt", bad)])
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            n3 = zf.namelist()
        record("build_zip_bytes 归档名已净化", n3 and "|" not in n3[0], str(n3))


def run_zip_name_decode() -> None:
    """zip 条目名解码（2026-09-10 示例部乱码事故回归）。

    事故：同一批上传里一半名字成「Θà╖Θ¢¬…」（cp437 形态）、一半成「閰烽洩…」（GBK 误解）——
    原实现"无 UTF-8 flag 一律按 GBK 解"对 UTF-8 名字必错（两种乱码就是它的两条分支）。
    本用例钉住：无 flag 时按 UTF-8 → GBK 试解，两种真实编码都能还原。
    """
    from app.services.customer_zip import decode_zip_name

    want = "酷雪头图10次卡.png"
    utf8_no_flag = want.encode("utf-8").decode("cp437")   # 字节是 UTF-8 但没声明 flag（zipfile 按 cp437 解）
    gbk_no_flag = want.encode("gbk").decode("cp437")      # 字节是 GBK 但没声明 flag（中文 WinRAR 常见）
    cases = [
        ("UTF-8 无 flag", utf8_no_flag, 0, want),
        ("GBK 无 flag", gbk_no_flag, 0, want),
        ("UTF-8 flag 置位", want, 0x800, want),
        ("纯 ASCII", "report_v2.pdf", 0, "report_v2.pdf"),
    ]
    bad = [(n, decode_zip_name(r, f), w) for n, r, f, w in cases if decode_zip_name(r, f) != w]
    record("zip 名解码：UTF-8/GBK 无 flag 均可还原（修前 UTF-8 必乱码）", not bad, str(bad[:2]))
    # 事故里真实出现过的两种乱码形态，修后不应再产生
    got_utf8 = decode_zip_name(utf8_no_flag, 0)
    record("不再产出事故乱码形态（Θà╖ / 閰烽洩）",
           not any(c in got_utf8 for c in "Θà╖閰烽洩") and got_utf8 == want, got_utf8)


def run_registry() -> None:
    for name in ("zip_extract", "zip_pack"):
        record(f"工具注册 {name}", get_tool(name) is not None)
    meta_ids = {m["id"] for m in get_all_tool_meta()}
    hidden = "zip_extract" not in meta_ids and "zip_pack" not in meta_ids
    record("zip 工具隐藏于前端技能列表（_INTERNAL_TOOLS）", hidden)


async def main() -> int:
    run_extract_matrix()
    run_macos_junk_filter()
    run_exec_skip_manifest()
    await run_tool_handlers()
    run_archive_name_sanitize()
    run_zip_name_decode()
    run_registry()
    failed = [r for r in _results if not r[1]]
    print(f"\n{'=' * 40}\n总计 {len(_results)} 项，失败 {len(failed)} 项")
    for name, _, detail in failed:
        print(f"  ✗ {name} — {detail[:120]}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
