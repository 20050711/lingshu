"""E2E：会议纪要工具 API 全链路（上传录音 → 转写 → 场景总结 → zip 下载）。

- 默认经部署形态 :24426；开发机用 BASE_URL 覆盖：BASE_URL=http://localhost:8001/api/v1 python -u tests/e2e_meeting.py
- 真实语音（FunASR 模型自带 asr_example.wav，含中文口播）→ 本地 FunASR 转写（真实调用）
- 状态机：uploaded → transcribing → ready → summarizing → done
- 权限：他人访问 403；白名单：press 未配置 custom_tools（null=全放行）时 200

用法: cd backend && source scripts/env_aip.sh && python -u tests/e2e_meeting.py
"""
import asyncio
import io
import os
import sys
import zipfile
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.creds import pw  # 密码走环境变量/backend/.env.test（2026-09-10）

# 2026-08-27：BASE 支持 BASE_URL 环境变量覆盖（默认部署形态 24425，开发机指 :8001）
BASE = os.environ.get("BASE_URL", "http://127.0.0.1:24426/api/v1")
ASR_WAV = "/opt/cache/modelscope/models/iic--speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch/snapshots/master/example/asr_example.wav"
TERMINAL = {"done", "failed"}

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  {'✓' if ok else '✗'} {name} {detail}")


async def main() -> int:
    async with httpx.AsyncClient(base_url=BASE, timeout=60) as c:
        # 2026-08-27：登录账号支持 E2E_USER=dept:user:pass 覆盖（部署机用 deploy01——press 压测账号已清理）
        login = (os.environ.get("E2E_USER") or "press:press10:Press#2026$Test").split(":")
        r = await c.post("/auth/login", json={"department_id": login[0], "username": login[1], "password": login[2]})
        r.raise_for_status()
        h = {"Authorization": f"Bearer {r.cookies['access_token']}"}

        # 1. 场景下拉
        r = await c.get("/tools/meetings/scenes", headers=h)
        scenes = r.json().get("scenes") or []
        check("场景下拉 200 含 4 预设", r.status_code == 200 and len(scenes) >= 4,
              f"keys={[s['key'] for s in scenes]}")

        # 2. 上传真实语音
        with open(ASR_WAV, "rb") as f:
            r = await c.post("/tools/meetings", headers=h,
                             files={"file": ("周会录音.wav", f)}, data={"title": "E2E 产品周会"})
        check("上传 200", r.status_code == 200, f"status={r.status_code} {r.text[:120]}")
        meeting_id = r.json()["meeting_id"]
        print(f"meeting: {meeting_id}")

        # 3. 轮询到 ready（FunASR 转写，最长 60s）
        st = "uploaded"
        transcript = ""
        for _ in range(60):  # 2026-09-01：120s 窗口（FunASR 冷启动 60-90s）
            await asyncio.sleep(2)
            data = (await c.get(f"/tools/meetings/{meeting_id}", headers=h)).json()
            st = data["status"]
            transcript = data.get("transcript") or ""
            if st in ("ready", *TERMINAL):
                break
        check("转写完成 ready", st == "ready", f"status={st}")
        check("转写稿非空带时间戳", len(transcript) > 0 and "[00:00]" in transcript,
              f"transcript_len={len(transcript)}")

        # 4. 总结（场景 meeting）
        # 2026-08-26：总结走 GLM 免费真实（aux_model 覆盖 llm_aux 档——DeepSeek 测试一律 mock，GLM 免费可真实）
        r = await c.post(f"/tools/meetings/{meeting_id}/summarize", headers=h,
                         json={"scene": "meeting", "aux_model": {"platform": "glm", "model": "glm-4.7-flash"}})
        check("总结触发 200", r.status_code == 200, f"status={r.status_code}")
        summary = ""
        for _ in range(60):  # 2026-09-01：180s 窗口（GLM 限流重试 429/1305 退避可达 90-120s）
            await asyncio.sleep(3)
            data = (await c.get(f"/tools/meetings/{meeting_id}", headers=h)).json()
            st = data["status"]
            summary = data.get("summary") or ""
            if st in TERMINAL:
                break
        check("总结完成 done", st == "done", f"status={st} err={data.get('error_msg')}")
        # 2026-08-27：去掉 "## " 硬依赖——GLM flash 对超短转写（73 字示例音频）输出可能无 markdown 标题
        check("总结非空", len(summary) > 50, f"summary_len={len(summary)}")

        # 5. zip 下载：完整音频 + 语音转写.md + 总结.md（中文名/内容乱码检查）
        r = await c.get(f"/tools/meetings/{meeting_id}/download", headers=h)
        check("zip 下载 200", r.status_code == 200, f"status={r.status_code}")
        names: list[str] = []
        if r.status_code == 200:
            with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
                names = zf.namelist()
                # 乱码检查：文件名与 md 内容均不得含替换符 �
                for n in names:
                    assert "�" not in n, f"zip 文件名乱码: {n}"
                for n in names:
                    if n.endswith(".md"):
                        assert "�" not in zf.read(n).decode("utf-8"), f"md 内容乱码: {n}"
        check("zip 含 3 类文件", any("完整音频" in n for n in names)
              and any("语音转写" in n for n in names)
              and any("LLM总结" in n for n in names), f"files={names}")

        # 6. 权限：他人访问 403
        r2 = await c.post("/auth/login", json={"department_id": "dept_root", "username": "admin", "password": pw("admin")})
        h2 = {"Authorization": f"Bearer {r2.cookies['access_token']}"}
        r = await c.get(f"/tools/meetings/{meeting_id}", headers=h2)
        check("他人访问 403", r.status_code == 403, f"status={r.status_code}")

        # 7. 历史列表 + 清理
        r = await c.get("/tools/meetings", headers=h)
        check("列表含本条", r.status_code == 200 and any(m["meeting_id"] == meeting_id for m in r.json()["meetings"]))
        r = await c.delete(f"/tools/meetings/{meeting_id}", headers=h)
        check("删除 200", r.status_code == 200, f"status={r.status_code}")

    ok = all(ok for _, ok, _ in results)
    print("=== PASS ===" if ok else "=== FAIL ===")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
