"""会话级 zip 解压/打包 + 成员安全规则（上传自动解压 + zip_extract/zip_pack 工具共用）。

- 安全规则：可执行/载荷黑名单（EXEC_BLOCKLIST/EXEC_LIKE）+ macOS 元数据垃圾判定
- safe_extract_zip：防御解压（zip-slip 三层防御 + 总量/文件数/深度限制）
- safe_extract_zip_filtered：同上 + 跳过可执行/载荷与 macOS 垃圾成员（返回跳过清单）
- build_zip_bytes / build_zip_file：内存 / 磁盘流式打包
- write_skip_manifest：把跳过成员写成《上传跳过清单.md》
"""
from __future__ import annotations

import io
import re
import stat
import zipfile
from pathlib import Path

from app.core.file_utils import sanitize_zip_member

# 可执行/可传播载荷扩展名黑名单（zip 内成员跳过 + 单文件上传直接拒）
# 边界：只收载荷类，不收业务正文/业务脚本——.py/.lua/.html/.pdf 等业务包常见格式一律放行。
EXEC_BLOCKLIST = {
    # Windows 可执行 / 安装 / 驱动载荷
    ".exe", ".msi", ".msp", ".mst", ".msu", ".com", ".scr", ".pif", ".cpl", ".msc",
    ".gadget", ".sys", ".drv", ".ocx", ".dll", ".lnk", ".inf", ".sct", ".shb",
    # 脚本宿主（批处理 / VBScript / JScript / WSH / PowerShell）
    ".bat", ".cmd", ".ps1", ".psm1", ".psd1", ".ps1xml", ".vbs", ".vbe", ".js",
    ".jse", ".wsf", ".wsh", ".hta",
    # 跨平台运行时载荷（Java / Android / Linux / macOS）
    ".jar", ".class", ".war", ".ear", ".jnlp", ".apk", ".dex",
    ".sh", ".run", ".bin", ".so", ".dylib", ".ko", ".pyc", ".pyo",
    ".deb", ".rpm", ".appimage", ".desktop", ".command",
}
# 宏/可执行文档（伪装成办公文件——正文格式 docx/xlsx/pptx 不含宏，故不在此列）
EXEC_LIKE = {".docm", ".dotm", ".xlsm", ".xltm", ".pptm", ".potm", ".xlam", ".xll", ".docb"}


def get_exec_ext(name: str) -> str | None:
    """取最后一段扩展名（双后缀文件取末段：'a.psd.baiduyun.p.downloading' → '.downloading'）。"""
    m = re.search(r"\.([^.\\/]+)$", name.strip())
    return ("." + m.group(1).lower()) if m else None


def is_executable_name(name: str) -> bool:
    ext = get_exec_ext(name)
    return ext in EXEC_BLOCKLIST or ext in EXEC_LIKE


def exec_rule(name: str) -> str | None:
    """命中黑名单时返回人话规则（写进跳过清单）；未命中返回 None。"""
    ext = get_exec_ext(name)
    if ext in EXEC_BLOCKLIST:
        return f"可执行/载荷文件（{ext}）"
    if ext in EXEC_LIKE:
        return f"宏文档（{ext}）"
    return None


def is_macos_junk(name: str) -> bool:
    """macOS 打包 zip 自带的元数据垃圾：__MACOSX/ 目录、.DS_Store、._*（AppleDouble）。

    这些文件与业务内容无关，落库后全部是 unreadable + 日志噪音，上传解压时直接跳过。
    """
    norm = name.replace("\\", "/")
    parts = [p for p in norm.split("/") if p]
    if any(p == "__MACOSX" for p in parts[:-1]) or norm.startswith("__MACOSX/"):
        return True
    base = parts[-1] if parts else norm
    return base == ".DS_Store" or base.startswith("._")


def safe_extract_zip(data: bytes, target_dir: Path, max_total_bytes: int,
                     max_files: int, max_depth: int) -> list[str]:
    """防御性解压 zip 字节流到 target_dir。

    返回解压出的相对路径清单（不含目录成员）。任一校验失败抛 ValueError（整体拒绝，目标目录不落残片）。
    """
    names, _skipped = _extract_zip_inner(data, target_dir, max_total_bytes, max_files, max_depth,
                                         skip_unsafe=False)
    return names


def safe_extract_zip_filtered(data: bytes, target_dir: Path, max_total_bytes: int, max_files: int,
                              max_depth: int) -> tuple[list[str], list[tuple[str, str, str]]]:
    """同 safe_extract_zip，但**跳过**可执行/载荷成员与 macOS 元数据垃圾，并回报跳过清单。

    跳过的成员**不落盘**，"服务器磁盘上不出现可执行文件"的保证不变。
    返回 (解压出的相对路径清单, [(来源, 成员路径, 命中规则), ...])。
    """
    return _extract_zip_inner(data, target_dir, max_total_bytes, max_files, max_depth,
                              skip_unsafe=True)


def _extract_zip_inner(data: bytes, target_dir: Path, max_total_bytes: int, max_files: int,
                       max_depth: int, skip_unsafe: bool) -> tuple[list[str], list[tuple[str, str, str]]]:
    """解压主流程（skip_unsafe=False 时行为与旧 safe_extract_zip 完全一致）。"""
    skipped: list[tuple[str, str, str]] = []
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("不是有效的 zip 文件")
    try:
        if zf.testzip():
            raise ValueError("zip 文件损坏（CRC 校验失败）")
        infos = zf.infolist()
        if len(infos) > max_files:
            raise ValueError(f"zip 文件数超过 {max_files} 限制")
        # 总量预检只算"真要落盘的成员"——被跳过的成员不写盘、不该顶掉包预算
        real_infos = []
        for i in infos:
            if skip_unsafe and not i.is_dir():
                base = i.filename.replace("\\", "/").split("/")[-1]
                rule = exec_rule(base) or ("macOS 元数据" if is_macos_junk(i.filename) else None)
                if rule:
                    skipped.append(("zip", i.filename, rule))
                    continue
            real_infos.append(i)
        if sum(i.file_size for i in real_infos) > max_total_bytes:
            raise ValueError(f"zip 解压总大小超过 {max_total_bytes // 1048576}MB 限制")
        target = target_dir.resolve()
        # 两阶段：先全量校验 + 内存读（任一成员非法整体拒绝，目标目录零残留），后统一落盘
        files: dict[str, bytes] = {}
        for info in real_infos:
            name = info.filename.replace("\\", "/")
            # 成员路径三层校验：绝对路径/首段 .. 与 . / 中间 .. 段
            if not name or name.startswith("/") or name.split("/")[0] in ("..", "."):
                raise ValueError(f"非法成员路径: {info.filename[:80]}")
            if ".." in name.split("/"):
                raise ValueError(f"非法成员路径: {info.filename[:80]}")
            if stat.S_ISLNK(info.external_attr >> 16):  # zip 符号链接成员 → 拒绝
                raise ValueError(f"不支持符号链接成员: {info.filename[:80]}")
            if name.count("/") > max_depth:
                raise ValueError(f"成员路径过深: {info.filename[:80]}")
            if info.is_dir():
                continue
            if info.file_size > max_total_bytes:  # 防谎报
                raise ValueError(f"zip 解压总大小超过 {max_total_bytes // 1048576}MB 限制")
            content = zf.read(info)
            if len(content) > max_total_bytes:
                raise ValueError(f"zip 解压总大小超过 {max_total_bytes // 1048576}MB 限制")
            files[name] = content
    finally:
        zf.close()
    # 落盘（二次 containment 兜底）
    target.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        dest = (target / name).resolve()
        if not dest.is_relative_to(target):
            raise ValueError(f"非法成员路径: {name[:80]}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
    return list(files.keys()), skipped


def build_zip_bytes(entries: list[tuple[Path, str]]) -> bytes:
    """内存打包 [(源绝对路径, 归档内相对名)] → zip 字节流（ZIP_DEFLATED）。

    归档名过 `sanitize_zip_member` 净化——含 Windows 非法字符（如半角 `|`）的包在
    Windows 资源管理器里**打不开、显示为空**（与 build_zip_file 同源问题）。
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for src, arcname in entries:
            zf.write(src, sanitize_zip_member(arcname))
    return buf.getvalue()


def build_zip_file(entries: list[tuple[Path, str]], out_path: Path, max_bytes: int) -> int:
    """磁盘流式打包 [(源绝对路径, 归档内相对名)] → out_path（ZIP_DEFLATED）。

    大文件逐成员流式写（zipfile 内部分块读，不整读内存）；累计原始大小超限抛 ValueError。
    返回打包内容总字节数。归档名净化后同名去重（`_2`/`_3` 后缀），防解压时互相覆盖。
    """
    total = 0
    used: set[str] = set()
    with zipfile.ZipFile(str(out_path), "w", zipfile.ZIP_DEFLATED) as zf:
        for src, arcname in entries:
            size = src.stat().st_size if src.is_file() else 0
            total += size
            if total > max_bytes:
                raise ValueError(f"打包内容超过 {max_bytes // (1024*1024)}MB 限制")
            name = sanitize_zip_member(arcname)
            if name in used:                      # 净化后撞名 → 加序号（否则解压互相覆盖）
                stem, dot, ext = name.rpartition(".")
                i = 2
                while True:
                    cand = f"{stem}_{i}{dot}{ext}" if dot else f"{name}_{i}"
                    if cand not in used:
                        break
                    i += 1
                name = cand
            used.add(name)
            zf.write(str(src), name)
    return total


SKIP_MANIFEST_NAME = "上传跳过清单.md"


def _md_cell(s: str) -> str:
    """表格单元里的用户可控文本：去换行/竖线、去反引号，再反引号包裹（防 markdown 注入/串行）。"""
    t = re.sub(r"[\r\n|]+", " ", str(s)).replace("`", "'").strip()
    return f"`{t[:160]}`"


def write_skip_manifest(target_dir: Path, rows: list[tuple[str, str, str]]) -> Path | None:
    """把被跳过的成员写成《上传跳过清单.md》，落在 target_dir（=本次上传解压后的当前层）。

    清单会被 agent 检索、也会在页面上渲染 → 文件名一律 _md_cell 转义。
    """
    if not rows:
        return None
    out = [
        "# 上传跳过清单",
        "",
        f"本次上传有 **{len(rows)}** 个文件按安全规则跳过（未入库、未落盘），其余文件已正常归档。",
        "平台不接收可执行文件/脚本载荷；如确需这些文件，请单独确认来源后人工处理。",
        "",
        "| 来源 | 文件 | 跳过原因 |",
        "| --- | --- | --- |",
    ]
    for src, name, rule in rows[:500]:
        out.append(f"| {_md_cell(src)} | {_md_cell(name)} | {_md_cell(rule)} |")
    if len(rows) > 500:
        out.append(f"| `（截断）` | `其余 {len(rows) - 500} 条未列出` | `` |")
    path = target_dir / SKIP_MANIFEST_NAME
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return path
