"""通用分片上传冒烟（2026-09-10 通用化验收）。

覆盖：
1. 通用端点 /uploads/chunk|complete：齐备合并返回暂存凭据 / 缺片 400 / MD5 不符 400
2. 归属校验：他人 upload_id 不能 complete（403）——upload_id 由客户端生成，防猜中会话劫持
3. parse_staged 参数校验（纯单测：非 JSON / 非数组 / 缺 upload_id / 超量）
4. 视频批次取件消费：staged_files → 文件按原名落到 video 批次目录且字节一致（批建后清理）
5. 会议录音取件消费：staged_file → 落到 meeting 目录（录音任务随后失败属预期，测试即删）
6. 目录树内单文件取件 / 工具下载取件（staged）/ **工具下载「编辑换包」PUT+staged**（2026-09-10）

用法（后端运行中，LLM_MOCK=1 零费用）：
    cd backend && source scripts/env_aip.sh && python -u tests/upload_chunk_smoke.py
原则：测试自建自删（分片会话 / 视频批次 / 会议行全清）。
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.creds import pw  # 密码走环境变量/backend/.env.test（2026-09-10）

import httpx

BASE = "http://localhost:8001/api/v1"
CHUNK = 50 * 1024 * 1024
VIDEO_DIR = Path("/data/tools/video")
MEETING_DIR = Path("/data/tools/meeting")

_RESULTS: list[tuple[str, bool, str]] = []
_SESSIONS: list[str] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    _RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")


def payload(size_mb: int = 3) -> tuple[bytes, str]:
    data = bytes(range(256)) * (size_mb * 1024 * 4)   # 非平凡字节（MD5 可辨）
    return data, hashlib.md5(data).hexdigest()


def chunks_of(data: bytes) -> list[bytes]:
    return [data[i * CHUNK: (i + 1) * CHUNK] for i in range((len(data) + CHUNK - 1) // CHUNK)]


async def login(c: httpx.AsyncClient, dept: str, username: str, password: str) -> dict:
    r = await c.post(f"{BASE}/auth/login",
                     json={"department_id": dept, "username": username, "password": password})
    if r.status_code != 200:
        print("  login failed:", r.status_code, r.text[:150])
        raise SystemExit(1)
    return {"Cookie": f"access_token={r.cookies.get('access_token')}"}


async def put_chunks(c: httpx.AsyncClient, h: dict, upload_id: str, data: bytes) -> None:
    for i, piece in enumerate(chunks_of(data)):
        r = await c.post(f"{BASE}/uploads/chunk",
                         data={"upload_id": upload_id, "chunk_index": str(i)},
                         files={"chunk": (f"chunk-{i}", io.BytesIO(piece), "application/octet-stream")},
                         headers=h, timeout=120)
        if r.status_code != 200:
            raise AssertionError(f"片 {i} 上传失败: {r.status_code} {r.text[:150]}")


def complete_form(upload_id: str, data: bytes, md5: str, file_name: str = "示例视频.mp4") -> dict:
    total = (len(data) + CHUNK - 1) // CHUNK
    return {"upload_id": upload_id, "total_chunks": str(total), "total_size": str(len(data)),
            "md5": md5, "file_name": file_name}


async def main() -> int:
    c = httpx.AsyncClient(base_url=BASE, timeout=180)
    admin = await login(c, "demo", "demo_admin", pw("demo_admin"))
    emp = await login(c, "demo", "demo", pw("demo"))
    batch_id = None
    meeting_id = None
    # 2026-09-10：video 工具 2026-09-02 起归入「团队定制化工具」白名单制（custom_allow，默认全关）——
    # 本用例要打 /tools/video/batches 取件口，须先种白名单（与 e2e_video_* 同法），跑完还原
    seeded_allow = False
    try:
        from app.core.database import get_global_engine
        from sqlalchemy import text as _text

        async with get_global_engine().connect() as conn:
            row = (await conn.execute(_text(
                "SELECT value FROM system_config WHERE key='custom_allow.demo'"))).first()
        if not row or "video" not in (row[0] or ""):
            import json as _json

            async with get_global_engine().begin() as conn:
                await conn.execute(_text(
                    "INSERT INTO system_config (key, value) VALUES ('custom_allow.demo', :v) "
                    "ON CONFLICT (key) DO UPDATE SET value=:v"),
                    {"v": _json.dumps(["video"])})
            seeded_allow = True
            print("  （已临时种 custom_allow.demo=['video']）")
    except Exception as e:
        print("  custom_allow 种子失败（视频用例可能 403）:", str(e)[:80])

    try:
        # ===== 1. parse_staged 参数校验（纯单测）=====
        from app.services import upload_chunks as uc

        for bad, why in (("not-json", "非 JSON"), ('{"a":1}', "非数组"), ("[{}]", "缺 upload_id"),
                         ('[{"upload_id":"x","file_name":"a"}]', "超过 limit")):
            try:
                uc.parse_staged(bad, limit=0 if why == "超过 limit" else 20)
                record(f"parse_staged 拒绝（{why}）", False, "未抛错")
            except Exception as e:   # app_error → AppError（.code/.message）
                record(f"parse_staged 拒绝（{why}）", getattr(e, "code", "") == "E003", str(e)[:60])
        ok_list = uc.parse_staged('[{"upload_id":"abc123","file_name":"a.mp4"}]')
        record("parse_staged 正常解析", len(ok_list) == 1 and ok_list[0]["file_name"] == "a.mp4", str(ok_list))

        # ===== 2. 缺片 complete → 400 =====
        data_a, md5_a = payload()
        uid_a = f"upchunk-a-{int(time.time())}"
        _SESSIONS.append(uid_a)
        await put_chunks(c, admin, uid_a, data_a)
        form = complete_form(uid_a, data_a, md5_a)
        form["total_chunks"] = str(int(form["total_chunks"]) + 1)   # 多声明一片 = 缺片
        r = await c.post(f"{BASE}/uploads/complete", data=form, headers=admin)
        record("缺片 complete 拒绝", r.status_code == 400 and "分片不完整" in r.text, f"{r.status_code} {r.text[:60]}")

        # ===== 3. MD5 不符 → 400 =====
        r = await c.post(f"{BASE}/uploads/complete",
                         data=complete_form(uid_a, data_a, "0" * 32), headers=admin)
        record("MD5 不符拒绝", r.status_code == 400 and "MD5" in r.text, f"{r.status_code} {r.text[:60]}")

        # ===== 4. 他人会话 complete → 403（归属校验）=====
        r = await c.post(f"{BASE}/uploads/complete", data=complete_form(uid_a, data_a, md5_a), headers=emp)
        record("他人 upload_id 拒绝（403）", r.status_code == 403, f"{r.status_code} {r.text[:60]}")

        # ===== 5. 齐备 complete → 暂存凭据 =====
        r = await c.post(f"{BASE}/uploads/complete",
                         data=complete_form(uid_a, data_a, md5_a, file_name="示例视频.mp4"), headers=admin)
        ok = r.status_code == 200 and r.json().get("upload_id") == uid_a
        record("齐备 complete 返回暂存凭据", ok,
               f"{r.status_code} {r.json() if r.status_code == 200 else r.text[:60]}")
        record("凭据保留原始包名与大小",
               r.status_code == 200 and r.json().get("file_name") == "示例视频.mp4"
               and r.json().get("size") == len(data_a), str(r.json())[:80])

        # ===== 6. 视频批次取件消费：staged_files → 文件落到批次目录且字节一致 =====
        staged = [{"upload_id": uid_a, "file_name": "示例视频.mp4"}]
        r = await c.post(f"{BASE}/tools/video/batches",
                         data={"staged_files": json.dumps(staged, ensure_ascii=False),
                               "task_desc": "分片取件冒烟"},
                         headers=admin)
        batch_id = r.json().get("batch_id") if r.status_code == 200 else None
        record("视频批次接收 staged_files", r.status_code == 200 and batch_id, f"{r.status_code} {r.text[:80]}")
        if batch_id:
            dst = VIDEO_DIR / str(batch_id) / "示例视频.mp4"
            same = dst.is_file() and dst.read_bytes() == data_a
            record("取件落盘字节一致", same, f"{dst} size={dst.stat().st_size if dst.is_file() else '-'}")
            record("取件后暂存会话已清", not (Path("/data/upload_chunks") / uid_a).exists(), uid_a)

        # ===== 7. 会议录音取件消费：staged_file → 落到 meeting 目录 =====
        data_b, md5_b = payload(1)
        uid_b = f"upchunk-b-{int(time.time())}"
        _SESSIONS.append(uid_b)
        await put_chunks(c, admin, uid_b, data_b)
        r = await c.post(f"{BASE}/uploads/complete",
                         data=complete_form(uid_b, data_b, md5_b, file_name="分片录音.wav"), headers=admin)
        r = await c.post(f"{BASE}/tools/meetings",
                         data={"staged_file": json.dumps(
                             [{"upload_id": uid_b, "file_name": "分片录音.wav"}], ensure_ascii=False),
                             "title": "分片取件冒烟"},
                         headers=admin)
        meeting_id = r.json().get("meeting_id") if r.status_code == 200 else None
        record("会议录音接收 staged_file", r.status_code == 200 and meeting_id, f"{r.status_code} {r.text[:80]}")
        if meeting_id:
            hits = list((MEETING_DIR / str(meeting_id)).glob("录音_*.wav"))
            same = bool(hits) and hits[0].read_bytes() == data_b
            record("会议取件落盘字节一致", same, str(hits[:1]))

        # ===== 8. 目录树内单文件取件（staged_file）——大文件单传不再走单请求 body =====
        import io as _io
        import zipfile

        buf = _io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
            zf.writestr("seed.txt", "seed")
        cid = None
        folder_id = None
        try:
            r = await c.post(f"{BASE}/customer/upload",
                             data={"scope": "personal", "customer_name": f"分片单文件冒烟{int(time.time())}"},
                             files={"zip_file": ("seed.zip", _io.BytesIO(buf.getvalue()),
                                                 "application/zip")}, headers=admin)
            cid = r.json().get("customer_id") if r.status_code == 200 else None
            if not cid:
                record("树内单文件取件（建客户）", False, f"{r.status_code} {r.text[:80]}")
            else:
                det = (await c.get(f"{BASE}/customer/customers/{cid}", headers=admin)).json()
                folder_id = (det.get("root_folders") or [{}])[0].get("id")
            if cid and folder_id:
                data_c, md5_c = payload(1)
                uid_c = f"upchunk-c-{int(time.time())}"
                _SESSIONS.append(uid_c)
                await put_chunks(c, admin, uid_c, data_c)
                await c.post(f"{BASE}/uploads/complete",
                             data=complete_form(uid_c, data_c, md5_c, file_name="树内文件.txt"), headers=admin)
                r = await c.post(f"{BASE}/customer/folders/{folder_id}/files",
                                 data={"staged_file": json.dumps(
                                     [{"upload_id": uid_c, "file_name": "树内文件.txt"}], ensure_ascii=False)},
                                 headers=admin)
                children = (await c.get(f"{BASE}/customer/folders/{folder_id}/children", headers=admin)).json()
                names = [f["file_name"] for f in children.get("files", [])]
                record("树内单文件取件（staged）", r.status_code == 200 and "树内文件.txt" in names,
                       f"{r.status_code} {names[-2:]}")
                record("取件后暂存会话已清（单文件）",
                       not (Path("/data/upload_chunks") / uid_c).exists(), uid_c)
        finally:
            if cid:
                await c.delete(f"{BASE}/customer/customers/{cid}", headers=admin)

        # ===== 9. 工具下载取件（staged_file，2026-09-10 运维工具包分片上传）=====
        root = await login(c, "dept_root", "admin", pw("admin"))
        buf2 = _io.BytesIO()
        with zipfile.ZipFile(buf2, "w") as z:
            z.writestr("tool.txt", "tool-package")
        blob = buf2.getvalue()
        uid_d = f"upchunk-d-{int(time.time())}"
        _SESSIONS.append(uid_d)
        td_id = None
        try:
            await put_chunks(c, root, uid_d, blob)
            r = await c.post(f"{BASE}/uploads/complete",
                             data={"upload_id": uid_d, "total_chunks": "1", "total_size": str(len(blob)),
                                   "md5": hashlib.md5(blob).hexdigest(), "file_name": "工具包冒烟.zip"},
                             headers=root)
            r = await c.post(f"{BASE}/admin/tool-downloads",
                             data={"title": "切片上传冒烟工具包", "desc": "smoke",
                                   "staged_file": json.dumps(
                                       [{"upload_id": uid_d, "file_name": "工具包冒烟.zip"}], ensure_ascii=False)},
                             headers=root)
            rows = (await c.get(f"{BASE}/admin/tool-downloads", headers=root)).json().get("files", [])
            hit = next((x for x in rows if x["title"] == "切片上传冒烟工具包"), None)
            td_id = hit["id"] if hit else None
            on_disk = bool(hit) and (Path("/data/tools_downloads") / hit["stored_path"]).is_file()
            record("工具下载取件（staged）", r.status_code == 200 and on_disk,
                   f"{r.status_code} {hit['filename'] if hit else r.text[:60]}")

            # ===== 10. 工具下载「编辑换文件」= PUT + 分片取件（2026-09-10 走查问题回归）=====
            # 现象：运维点「编辑」换包 → 前端提示「请求失败，请稍后重试」。根因：前端把新建/编辑统一
            # 走 uploadFileDirect（固定 POST），编辑路径打到 POST /admin/tool-downloads/{id}
            # （后端只注册 PUT）→ 405，body 非业务错误信封 → 前端只能给通用文案。
            # 本用例从**接口契约**侧钉住：PUT + staged_file 必须 200，且换包后旧文件被删、指向新文件。
            if td_id and hit:
                old_stored = hit["stored_path"]
                buf3 = _io.BytesIO()
                with zipfile.ZipFile(buf3, "w") as z:
                    z.writestr("tool2.txt", "tool-package-v2")
                blob2 = buf3.getvalue()
                uid_e = f"upchunk-e-{int(time.time())}"
                _SESSIONS.append(uid_e)
                await put_chunks(c, root, uid_e, blob2)
                await c.post(f"{BASE}/uploads/complete",
                             data={"upload_id": uid_e, "total_chunks": "1", "total_size": str(len(blob2)),
                                   "md5": hashlib.md5(blob2).hexdigest(), "file_name": "工具包冒烟v2.zip"},
                             headers=root)
                r = await c.put(f"{BASE}/admin/tool-downloads/{td_id}",
                                data={"title": "切片上传冒烟工具包", "desc": "smoke-edit",
                                      "enabled": "true", "sort_order": "0",
                                      "staged_file": json.dumps(
                                          [{"upload_id": uid_e, "file_name": "工具包冒烟v2.zip"}],
                                          ensure_ascii=False)},
                                headers=root)
                rows2 = (await c.get(f"{BASE}/admin/tool-downloads", headers=root)).json().get("files", [])
                hit2 = next((x for x in rows2 if x["id"] == td_id), None)
                new_ok = bool(hit2) and hit2["stored_path"] != old_stored \
                    and (Path("/data/tools_downloads") / hit2["stored_path"]).read_bytes() == blob2
                record("工具下载编辑换包（PUT + staged）", r.status_code == 200 and new_ok,
                       f"{r.status_code} {r.text[:60]} stored={hit2['stored_path'] if hit2 else '-'}")
                record("换包后旧文件已删",
                       not (Path("/data/tools_downloads") / old_stored).exists(), old_stored)
        finally:
            if td_id:
                await c.delete(f"{BASE}/admin/tool-downloads/{td_id}", headers=root)
    finally:
        if seeded_allow:
            try:
                from app.core.database import get_global_engine
                from sqlalchemy import text as _text

                async with get_global_engine().begin() as conn:
                    await conn.execute(_text("DELETE FROM system_config WHERE key='custom_allow.demo'"))
                print("  （已还原 custom_allow.demo）")
            except Exception:
                pass
        for uid in _SESSIONS:
            import shutil

            shutil.rmtree(f"/data/upload_chunks/{uid}", ignore_errors=True)
        if batch_id:
            try:
                await c.delete(f"{BASE}/tools/video/batches/{batch_id}", headers=admin)
            except Exception:
                pass
        if meeting_id:
            try:
                await c.delete(f"{BASE}/tools/meetings/{meeting_id}", headers=admin)
            except Exception:
                pass
        await c.aclose()
    failed = [r for r in _RESULTS if not r[1]]
    print(f"\n== {len(_RESULTS) - len(failed)}/{len(_RESULTS)} PASS ==")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
