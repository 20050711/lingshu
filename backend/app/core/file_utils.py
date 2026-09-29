"""上传文件名净化与读取限流（S2/M3 修复：统一各上传点）。

用法：`name = sanitize_filename(f.filename)`——返回值不含路径分隔符；
`content = await read_limited(f, max_bytes)`——分块读取并限制大小。
"""
import re
from pathlib import Path

# SEC-16：扩展名 → 文件头魔数白名单（手写头字节表，不引新依赖）。
# PK\x03\x04 覆盖 xlsx/docx/pptx（zip 容器）；\xd0\xcf\x11\xe0 覆盖 xls/ppt（OLE2 容器）；
# csv/md/txt/html 为文本类不校验（防误伤，文本解析器畸形输入风险低）。
_MAGIC: dict[str, tuple[bytes, ...]] = {
    ".xlsx": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
    ".zip": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
    ".docx": (b"PK\x03\x04",),
    ".pptx": (b"PK\x03\x04",),
    ".xls": (b"\xd0\xcf\x11\xe0",),
    ".ppt": (b"\xd0\xcf\x11\xe0",),
    ".pdf": (b"%PDF",),
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    # 2026-09-15（音视频上传）：常见媒体魔数（ISO BMFF 家族 mp4/mov/m4a 的 ftyp 在第 4 字节，见下方特判）
    ".webm": (b"\x1a\x45\xdf\xa3",), ".mkv": (b"\x1a\x45\xdf\xa3",),
    ".wav": (b"RIFF",), ".avi": (b"RIFF",),
    ".mp3": (b"ID3",),
    ".flac": (b"fLaC",), ".ogg": (b"OggS",), ".opus": (b"OggS",),
}


def check_magic_bytes(content: bytes, ext: str) -> str | None:
    """内容嗅探：返回 None=通过；否则返回拒绝原因（伪造扩展名/损坏文件）。

    SEC-16（2026-08-17）：扩展名白名单 + 头字节双重校验（改名 exe/任意文本冒充
    Office/PDF/图片被拒）。html 去 BOM/空白后须以 '<' 开头；文本类（csv/md/txt）不校验。
    """
    if not content:
        return "文件内容为空"
    ext = ext.lower()
    if ext in (".html", ".htm"):
        head = content.lstrip(b"\xef\xbb\xbf \t\r\n")
        if not head.startswith(b"<"):
            return "HTML 文件内容需以 < 开头"
        return None
    # 2026-09-15（音视频上传）：ISO BMFF 家族（mp4/mov/m4a/3gp）——ftyp box 在第 4-8 字节
    # （前 4 字节是 box 长度，可变），前缀匹配不适用
    if ext in (".mp4", ".mov", ".m4v", ".m4a", ".3gp"):
        if len(content) >= 12 and content[4:8] == b"ftyp":
            return None
        return f"文件内容与 {ext} 格式不符（疑似伪造扩展名或损坏文件）"
    expected = _MAGIC.get(ext)
    if expected is None:  # 文本类（csv/md/txt）不校验
        return None
    if not content.startswith(expected):
        return f"文件内容与 {ext} 格式不符（疑似伪造扩展名或损坏文件）"
    return None


# SEC-19：团队标识白名单（数据库 URL/文件路径直拼前校验；字母数字下划线 ≤32）
VALID_DEPT_RE = re.compile(r"^[a-z0-9_]{1,32}$")


def is_valid_dept_id(dept_id: str | None) -> bool:
    return bool(dept_id) and bool(VALID_DEPT_RE.match(dept_id))


def sanitize_filename(filename: str | None, default: str = "file") -> str:
    """净化上传文件名：取 basename（去 ../ 与 / ）、去控制字符；空值回落 default。"""
    raw = (filename or default).replace("\\", "/")  # Windows 反斜杠同样处理
    name = Path(raw).name.strip()
    if not name or name in (".", ".."):
        name = default
    name = "".join(ch for ch in name if ch.isprintable())
    return name or default


# Windows 文件/目录名非法字符（含控制字符）——zip 里带这些字符时，Windows 资源管理器
# **直接打不开、显示为空**（7-Zip 能列出但解压报错）
_WIN_ILLEGAL_RE = re.compile(r'[<>:"|?*\x00-\x1f]')


def sanitize_zip_member(name: str, default: str = "file") -> str:
    """把 zip 归档内路径净化成 **Windows 可解压** 的形态（逐段替换非法字符，保留 `/` 分隔）。

    2026-09-10 走查问题：外部标题原样进归档目录名，含半角 `|` 的标题（如
    「拼豆日记📔郁金香小羊🌷| 附图纸」）打出来的 zip 在 Windows 上**打开是空的**——
    用户以为"下载空了"，其实文件都在、是归档名非法（第一轮标题只含全角「？」故正常）。
    顺带防 `..`/绝对路径段逃逸（归档名来自用户内容/第三方产物的地方都要过这里）。
    """
    out: list[str] = []
    for seg in str(name or "").replace("\\", "/").split("/"):
        seg = seg.strip()
        if not seg or seg in (".", ".."):
            continue
        seg = _WIN_ILLEGAL_RE.sub("_", seg).rstrip(" .")   # Windows 不允许结尾空格/点
        if seg:
            out.append(seg)
    return "/".join(out) or default


# R6（红队三修复）：错误回显脱敏——替换服务端绝对路径（/data /tmp /home /var 开头），
# 防解析器/渲染器异常文本向客户端泄露服务器目录结构（kb_service/admin import/chat preview 复用）
_PATH_SANITIZE_RE = re.compile(r"(?:/data|/tmp|/home|/var)[^\s'\"]*")


def sanitize_err_text(text: str, limit: int = 200) -> str:
    return _PATH_SANITIZE_RE.sub("<path>", str(text))[:limit]


async def read_limited(f, max_bytes: int) -> bytes:
    """分块读取上传文件并限制大小（M3：原全量 read() 进内存再判，GB 级请求可打爆进程内存）。

    超限抛 ValueError（调用方转业务错误码）。
    """
    chunks: list[bytes] = []
    total = 0
    while chunk := await f.read(1024 * 1024):
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(f"文件超过 {max_bytes // (1024 * 1024)}MB 限制")
        chunks.append(chunk)
    return b"".join(chunks)


async def save_limited(f, dest: Path, max_bytes: int) -> tuple[int, bytes]:
    """分块**流式落盘**（2026-09-15：音视频 200MB 级不整读进内存）并限大小。

    返回 (字节数, 头 32 字节)——头部供 check_magic_bytes 魔数嗅探（ftyp 判定需前 12 字节）。
    超限抛 ValueError 并清理半成品文件（调用方转业务错误码）。
    """
    import asyncio

    total = 0
    head = b""
    out = await asyncio.to_thread(open, dest, "wb")
    try:
        while chunk := await f.read(1024 * 1024):
            total += len(chunk)
            if total > max_bytes:
                await asyncio.to_thread(out.close)
                dest.unlink(missing_ok=True)
                raise ValueError(f"文件超过 {max_bytes // (1024 * 1024)}MB 限制")
            if len(head) < 32:
                head = (head + chunk)[:32]
            await asyncio.to_thread(out.write, chunk)  # M9：同步写盘包线程，防并发上传阻塞事件循环
    finally:
        try:
            await asyncio.to_thread(out.close)
        except Exception:
            pass
    return total, head
