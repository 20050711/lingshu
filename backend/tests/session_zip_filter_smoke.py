"""会话 zip 直传「过滤 + 跳过名单」回归（2026-09-17 用户要求）。

口径与平台 zip 上传链路一致：zip 内命中可执行/载荷黑名单的成员与 macOS 元数据垃圾
**跳过不落盘**，其余正常解压；跳过清单回给前端（小浮窗）并在解压目录写一份《上传跳过清单.md》。

断言：
1) 上传响应 extra.skipped 覆盖 .exe / .DS_Store / __MACOSX 三类
2) 正常成员解压成功（unzipped_count ≥ 1）
3) 磁盘上**不出现** .exe（不落盘的硬保证）
4) 解压目录内有《上传跳过清单.md》
5) 自建自删（删会话清文件）

用法：cd backend && source scripts/env_aip.sh && python -u tests/session_zip_filter_smoke.py
"""
from __future__ import annotations

import asyncio
import io
import sys
import zipfile
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.creds import pw  # 密码走环境变量/backend/.env.test（2026-09-10）

BASE = "http://localhost:8001/api/v1"
RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"{'✓' if ok else '✗'} {name}" + (f"  {detail}" if detail else ""))


def build_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("资料/说明.txt", "hello")
        zf.writestr("资料/工具/绿色版助手.exe", b"MZ fake")
        zf.writestr("资料/宏文档.xlsm", b"fake macro")
        zf.writestr("__MACOSX/._说明.txt", b"junk")
        zf.writestr("资料/.DS_Store", b"junk")
    return buf.getvalue()


async def main() -> int:
    from app.core.config import get_settings

    async with httpx.AsyncClient(base_url=BASE, timeout=120) as c:
        r = await c.post("/auth/login", json={"department_id": "demo", "username": "demo",
                                              "password": pw("demo")})
        h = {"Authorization": f"Bearer {r.cookies['access_token']}", "X-Client-ID": "zip-filter"}
        sid = (await c.post("/chat/sessions", headers=h, json={"client_id": "zip-filter"})).json()["session_id"]
        try:
            up = await c.post("/chat/files", headers=h, data={"session_id": sid},
                              files={"files": ("测试包.zip", build_zip())})
            record("上传 200", up.status_code == 200, f"status={up.status_code} {up.text[:120]}")
            data = up.json() if up.status_code == 200 else {}
            extra = data.get("extra") or {}
            skipped = extra.get("skipped") or []
            names = " | ".join(x.get("name", "") for x in skipped)
            record("跳过清单含 .exe", any(n.endswith(".exe") for n in names.split(" | ")), names)
            record("跳过清单含宏文档 .xlsm", ".xlsm" in names, names)
            record("跳过清单含 macOS 垃圾（.DS_Store/__MACOSX）",
                   ".DS_Store" in names and "__MACOSX" in names, names)
            record("正常成员已解压", int(extra.get("unzipped_count") or 0) >= 1,
                   f"unzipped_count={extra.get('unzipped_count')}")

            # 磁盘核对：解压目录里不得出现 .exe；须有跳过清单
            s = get_settings()
            base = Path(s.upload_dir) / "users"
            hits = [p for p in base.rglob("测试包_unzip/**/*") if p.is_file()] if base.exists() else []
            files = [p for p in base.rglob("*") if p.is_file() and "测试包_unzip" in str(p)]
            record("磁盘上无 .exe（不落盘保证）", not any(p.suffix.lower() == ".exe" for p in files),
                   f"解压出 {len(files)} 个文件 {[p.name for p in files][:6]}")
            record("解压目录有《上传跳过清单.md》",
                   any(p.name == "上传跳过清单.md" for p in files), f"{[p.name for p in files][:6]}")
            record("正常文件已落盘", any(p.name == "说明.txt" for p in files), "")
            _ = hits
        finally:
            d = await c.delete(f"/chat/sessions/{sid}", headers=h)
            print(f"清理会话: {d.status_code}")

    fails = [r for r in RESULTS if not r[1]]
    print(f"\n=== {'PASS' if not fails else 'FAIL'}: {len(RESULTS) - len(fails)}/{len(RESULTS)} ===")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
