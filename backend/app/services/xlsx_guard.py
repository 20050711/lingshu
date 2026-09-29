"""xlsx 解压炸弹预检（openpyxl 加载前的通用守卫）。

2026-09-17：从 `import_pipeline` 抽出为独立模块——数据导入/查询线下线后，
file_parse（`agent/tools/search_tools.run_xlsx_parse`）仍需要这道防线；
若随管线一起删掉，zip 炸弹预检会**静默消失**（唯一回归在 `tests/attack_repro_xlsx.py`）。

F6（红队二次，2026-08-18）：zip 炸弹防御阈值——
红队链 7：500MB 展开的 sharedStrings 压缩至 488KB 过 20MB 上传上限 + magic 校验，
load_workbook(read_only=True) 仍整体物化 sharedStrings → uvicorn RSS 5.5GB 打崩面。
阈值按平台用途（团队数据表，入口 20MB，正常文件压缩比远低于 100:1）校准。
"""
from __future__ import annotations

import zipfile
from xml.etree import ElementTree

_XLSX_MAX_COMPRESSION_RATIO = 100     # zip 条目 file_size/compress_size > 100 拒（炸弹典型 1000:1）
_XLSX_MAX_SHARED_STRINGS_BYTES = 100 * 1024 * 1024  # sharedStrings 展开后上限 100MB


def precheck_xlsx(file_path: str) -> str | None:
    """openpyxl 加载前预检——zip 条目压缩比 + sharedStrings 展开上限。

    在 load_workbook 之前执行（拒绝发生在 openpyxl 启动前，不占解析内存）；
    .xls/.csv 非 zip 直接跳过（openpyxl 现状本就解析失败，行为不变）。
    返回 None=放行；否则返回拒绝原因（调用方以异常/错误暴露）。
    """
    try:
        zf = zipfile.ZipFile(file_path)
    except (zipfile.BadZipFile, OSError):
        return None  # 非 zip（.xls/.csv）——openpyxl 后续自行失败，行为与现状一致
    with zf:
        for info in zf.infolist():
            if info.compress_size and info.file_size and info.file_size / info.compress_size > _XLSX_MAX_COMPRESSION_RATIO:
                return (f"文件压缩比异常（{info.filename}: 展开 {info.file_size} 字节 / 压缩 "
                        f"{info.compress_size} 字节，超阈值 {_XLSX_MAX_COMPRESSION_RATIO}:1）——疑似解压炸弹，已拒绝解析")
        # sharedStrings 流式预扫（iterparse + clear 防驻留；openpyxl read_only 会整体物化此表）
        if "xl/sharedStrings.xml" in zf.namelist():
            total = 0
            try:
                with zf.open("xl/sharedStrings.xml") as f:
                    for _event, elem in ElementTree.iterparse(f, events=("end",)):
                        if elem.tag.endswith("}t") and elem.text:
                            total += len(elem.text)
                        elem.clear()
                        if total > _XLSX_MAX_SHARED_STRINGS_BYTES:
                            return (f"共享字符串表展开后超过 {_XLSX_MAX_SHARED_STRINGS_BYTES // 1024 // 1024}MB "
                                    f"上限（已累计 {total // 1024 // 1024}MB）——疑似解压炸弹，已拒绝解析")
            except (OSError, ElementTree.ParseError, ValueError):
                return None  # 预扫异常不阻断（openpyxl 会再报真实错误）
    return None
