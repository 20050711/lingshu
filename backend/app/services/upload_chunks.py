"""大文件分片上传公共层（2026-09-10 通用化）。

背景（2026-09-09 排障结论）：单个请求的大 body 有三层门槛——本机 nginx
client_max_body_size 600m、部署机 Windows portproxy 对 >~650MB 长 body 转发缺陷（400 空体）、
后端业务上限。统一方案：前端 50MB 切片，每片一个请求，任何一层都绕开。

协议：
- upload_id 客户端生成（6-64 位字母数字_-）→ 逐片 POST（幂等：同 index 覆盖重传）
- complete 前逐片齐备校验（缺失列明）→ 按序流式合并（不占内存）→ 位级校验（总大小 + MD5）

消费两条路：
1. 就地消费：complete 直接跑业务管线（业务侧自带处理函数）
2. 暂存消费（会议纪要 / 工具包）：/uploads/complete 合并后保留 merged.bin 返回凭据，
   业务接口以 staged_files（JSON）接收，取件用 take_staged（移动消费，取走即清理）

归属：会话目录首个分片落 .owner（user_id），complete/取件校验同一用户——upload_id 由客户端
生成，防猜中他人会话劫持。
清理：每日 03:45 cleanup_expired（TTL 见 config upload_chunk_ttl_hours）；前端取消=停止发送，
残留分片由 TTL 兜底（不另设取消接口）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import time
from pathlib import Path

from fastapi import UploadFile

from app.core.config import get_settings
from app.core.exceptions import app_error

_settings = get_settings()

_ID_RE = re.compile(r"[A-Za-z0-9_-]{6,64}")
STAGED_NAME = "merged.bin"      # 合并产物名（暂存消费：待业务接口取件）
_OWNER_FILE = ".owner"


def _root() -> Path:
    return Path(_settings.upload_chunk_dir)


def session_dir(upload_id: str) -> Path:
    """分片会话目录（upload_id 由客户端生成，须防路径穿越）。"""
    if not _ID_RE.fullmatch(upload_id or ""):
        raise app_error("E003", "upload_id 非法（仅允许字母/数字/_/-，6-64 位）", status_code=400)
    return _root() / upload_id


def _claim_owner(d: Path, user_id: int) -> None:
    """首片落 .owner；已有 owner 时校验同一用户（防上传到他人会话）。"""
    f = d / _OWNER_FILE
    if f.is_file():
        try:
            owner = int(f.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            owner = 0
        if owner and owner != user_id:
            raise app_error("E006", "无权访问该上传会话", status_code=403)
        return
    try:
        f.write_text(str(user_id), encoding="utf-8")
    except OSError:
        pass


def assert_owner(d: Path, user_id: int) -> None:
    """complete/取件前的归属校验（历史会话无 .owner 时视为可认领）。"""
    f = d / _OWNER_FILE
    if not f.is_file():
        return
    try:
        owner = int(f.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return
    if owner and owner != user_id:
        raise app_error("E006", "无权访问该上传会话", status_code=403)


async def save_chunk(user: dict, upload_id: str, chunk_index: int, chunk: UploadFile) -> dict:
    """保存单个分片（幂等：同名覆盖）。并发安全：不同 index 互不竞争，同 index 原子替换。"""
    if chunk_index < 0 or chunk_index > 100000:
        raise app_error("E003", "chunk_index 非法", status_code=400)
    limit = _settings.upload_chunk_max_mb * 1024 * 1024
    d = session_dir(upload_id)
    await asyncio.to_thread(d.mkdir, parents=True, exist_ok=True)
    _claim_owner(d, user["user_id"])
    size = 0
    final = d / f"{chunk_index}.part"
    tmp = d / f"{chunk_index}.part.tmp"
    with tmp.open("wb") as f:
        while True:
            data = await chunk.read(1024 * 1024)
            if not data:
                break
            size += len(data)
            if size > limit:
                f.close()
                tmp.unlink(missing_ok=True)
                raise app_error("E003", f"单片超过 {_settings.upload_chunk_max_mb}MB 限制",
                                status_code=400)
            f.write(data)
    tmp.replace(final)   # 原子替换 = 幂等（重复上传同一片覆盖）
    return {"ok": True, "index": chunk_index, "size": size}


def list_uploaded_chunks(user: dict, upload_id: str) -> dict:
    """已落盘的分片索引（2026-09-15 断点续传：客户端只补缺失片，不必整包重传）。

    会话不存在/已过期（TTL 清理）→ exists=False，客户端从头开始；
    非本人会话 → 403（upload_id 由客户端生成，防猜中他人会话）。
    """
    d = session_dir(upload_id)
    if not d.is_dir():
        return {"exists": False, "uploaded": []}
    assert_owner(d, user["user_id"])
    idx: list[int] = []
    for p in d.glob("*.part"):
        try:
            idx.append(int(p.stem))
        except ValueError:
            continue
    return {"exists": True, "uploaded": sorted(idx)}


# ---------- 服务端阶段进度（2026-09-16：前端在 complete 挂起期间轮询） ----------
#
# 背景（走查）：>50MB 的分片传完后，complete 这一个请求要同步做「合并 + 位级校验 + 解压 +
# 归档」，大包要一两分钟——前端此前只能把进度条钉在 90% 不动，用户以为卡死。
# 这里把服务端阶段的真实进度写 Redis（TTL 1h 自清），前端 1s 轮询刷 90→99%。

def _prog_ttl() -> int:
    """进度 key 的 TTL：**与会话目录对齐**（2026-09-16 exe 侧反馈）。

    原固定 3600s：10GB 级包分片上传 + 合包可能超过 1 小时，进度 key 会先于会话过期，
    exe 轮询时误判成"会话没了"。取会话 TTL 与 2h 的较大者。
    """
    return max(7200, _settings.upload_chunk_ttl_hours * 3600)


_MERGED_TTL_S = 3600     # 「已合并」标记：会话目录清掉后，客户端仍能区分"已合并"与"已过期"
_MERGING: set[str] = set()   # 合包互斥（单 worker 下进程内即原子；见 try_begin_merge）


def _prog_key(upload_id: str) -> str:
    return f"upload_prog:{upload_id}"


def _merged_key(upload_id: str) -> str:
    return f"upload_merged:{upload_id}"


async def set_progress(upload_id: str, percent: float, text: str, phase: str = "") -> None:
    """上报进度（尽力而为：任何异常都吞掉，绝不影响上传本身）。

    phase（2026-09-16，exe 侧需求）：merging / extracting / importing / done / failed，
    空字符串=不改变已记录的阶段（老调用点无需改）。
    """
    if not _ID_RE.fullmatch(upload_id or ""):
        return
    try:
        payload: dict = {"percent": max(0, min(100, int(percent))), "text": str(text)[:80]}
        if phase:
            payload["phase"] = str(phase)[:16]
        raw = json.dumps(payload, ensure_ascii=False)
    except Exception:
        return
    try:
        from app.core.redis import redis_get, redis_set

        if not phase:                       # 未指定阶段：沿用已记录的那个，避免把 phase 冲掉
            old = await redis_get(_prog_key(upload_id))
            if old:
                p = json.loads(old).get("phase")
                if p:
                    payload["phase"] = p
                    raw = json.dumps(payload, ensure_ascii=False)
        await redis_set(_prog_key(upload_id), raw, _prog_ttl())
    except Exception:
        pass


async def get_progress(upload_id: str) -> dict:
    """读进度（无记录返回空态；前端据此只显示文案不推进度）。"""
    if not _ID_RE.fullmatch(upload_id or ""):
        raise app_error("E003", "upload_id 非法", status_code=400)
    try:
        from app.core.redis import redis_get

        raw = await redis_get(_prog_key(upload_id))
        if raw:
            data = json.loads(raw)
            return {"percent": int(data.get("percent", 0)), "text": str(data.get("text", "")),
                    "phase": str(data.get("phase", ""))}
    except Exception:
        pass
    return {"percent": 0, "text": "", "phase": ""}


async def mark_merged(upload_id: str) -> None:
    """记「已合并」标记（尽力而为）。

    2026-09-16（exe 侧需求）：complete 成功后立刻 drop_session，客户端超时后重查只会拿到
    「会话不存在」——分不清"已合并（后台在导入）"与"会话过期（得重传）"。留一个短 TTL 标记，
    让客户端重查能拿到确定结论。
    """
    if not _ID_RE.fullmatch(upload_id or ""):
        return
    try:
        from app.core.redis import redis_set

        await redis_set(_merged_key(upload_id), "1", _MERGED_TTL_S)
    except Exception:
        pass


async def is_merged(upload_id: str) -> bool:
    try:
        from app.core.redis import redis_get

        return bool(await redis_get(_merged_key(upload_id)))
    except Exception:
        return False


def try_begin_merge(upload_id: str) -> bool:
    """合包互斥（2026-09-16 exe 侧需求）：在途时第二次 complete 应被挡住而不是撞车。

    两个并发 complete 会同时通过齐备校验、往同一个 `merged.bin` 写（互相覆盖），
    失败时一边 unlink 另一边还在读。这里用**进程内**守卫：判重与占位之间没有 await，
    单 worker（项目硬约束）下等价于原子操作；进程重启即自然释放（那时合包也确实断了）。
    """
    if upload_id in _MERGING:
        return False
    _MERGING.add(upload_id)
    return True


def end_merge(upload_id: str) -> None:
    _MERGING.discard(upload_id)


async def merge_chunks(user: dict, upload_id: str, total_chunks: int, total_size: int,
                       md5: str, out_name: str = STAGED_NAME) -> Path:
    """齐备校验 → 按序流式合并 → 位级校验（总大小 + MD5）；返回合并产物路径。

    校验失败一律 400 且不留半成品（合并产物删除，分片保留供补传）；调用方决定会话目录去留。
    """
    d = session_dir(upload_id)
    if not d.is_dir():
        raise app_error("E003", "分片会话不存在（已合并完成或已过期，请重新上传）", status_code=400)
    assert_owner(d, user["user_id"])
    if total_chunks < 1 or total_chunks > 100000:
        raise app_error("E003", "total_chunks 非法", status_code=400)
    missing = [i for i in range(total_chunks) if not (d / f"{i}.part").is_file()]
    if missing:
        raise app_error("E003", f"分片不完整，缺失 {len(missing)} 片（{missing[:10]}…），请补齐后重试",
                        status_code=400)
    h = hashlib.md5()
    merged = 0
    merge_path = d / out_name
    # 合并占服务端总进度的 0~45%（解压/归档占 45~100%，见业务侧进度回调）
    await set_progress(upload_id, 1, f"正在合并分片 0/{total_chunks}", phase="merging")
    with merge_path.open("wb") as out:
        for i in range(total_chunks):
            if i % 5 == 0 or i == total_chunks - 1:      # 每 5 片报一次（15GB≈300 片，够细且不刷爆 Redis）
                await set_progress(upload_id, 45 * (i + 1) / total_chunks,
                                   f"正在合并分片 {i + 1}/{total_chunks}", phase="merging")
            with (d / f"{i}.part").open("rb") as src:
                while True:
                    buf = src.read(1024 * 1024)
                    if not buf:
                        break
                    h.update(buf)
                    merged += len(buf)
                    out.write(buf)
    if total_size and merged != total_size:
        merge_path.unlink(missing_ok=True)
        raise app_error("E003", f"合并大小不符（实际 {merged}，声明 {total_size}）——分片可能被篡改或遗漏",
                        status_code=400)
    if md5 and h.hexdigest() != md5.lower():
        merge_path.unlink(missing_ok=True)
        raise app_error("E003", "分片总校验（MD5）不符——上传数据不完整，请整包重传", status_code=400)
    return merge_path


def drop_session(upload_id: str) -> None:
    """删除会话目录（成功消费/前端确认后清理；失败路径保留供补传）。"""
    if not _ID_RE.fullmatch(upload_id or ""):
        return
    shutil.rmtree(_root() / upload_id, ignore_errors=True)


# ---------- 暂存消费（会议纪要/工具包等业务接口取件） ----------

def parse_staged(raw: str | None, limit: int = 20) -> list[dict]:
    """解析 staged_files（JSON 数组：[{upload_id, file_name}]）；空=无。"""
    if not raw or not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise app_error("E003", "staged_files 须为 JSON 数组", status_code=400)
    if not isinstance(data, list):
        raise app_error("E003", "staged_files 须为 JSON 数组", status_code=400)
    if len(data) > limit:
        raise app_error("E003", f"staged_files 最多 {limit} 条", status_code=400)
    out: list[dict] = []
    for item in data:
        if not isinstance(item, dict) or not item.get("upload_id"):
            raise app_error("E003", "staged_files 条目须含 upload_id", status_code=400)
        out.append({"upload_id": str(item["upload_id"]),
                    "file_name": str(item.get("file_name") or "upload.bin")})
    return out


def take_staged(user: dict, descriptor: dict) -> tuple[str, Path]:
    """取件：返回 (原始文件名, 合并产物路径)；调用方负责移走（取走后 drop_session）。

    产物是 merged.bin（无扩展名）——扩展名以 descriptor.file_name 为准（调用方自行校验白名单）。
    """
    d = session_dir(str(descriptor.get("upload_id") or ""))
    path = d / STAGED_NAME
    if not d.is_dir() or not path.is_file():
        raise app_error("E003", "分片会话不存在（已消费或已过期，请重新上传）", status_code=400)
    assert_owner(d, user["user_id"])
    return str(descriptor.get("file_name") or "upload.bin"), path


def cleanup_expired() -> int:
    """分片临时目录清理（每日任务挂载）：超过 TTL 的 upload_id 目录整体删除。"""
    root = _root()
    if not root.is_dir():
        return 0
    ttl_s = _settings.upload_chunk_ttl_hours * 3600
    now = time.time()
    n = 0
    for d in root.iterdir():
        if d.is_dir():
            try:
                if now - d.stat().st_mtime > ttl_s:
                    shutil.rmtree(d, ignore_errors=True)
                    n += 1
            except OSError:
                continue
    return n
