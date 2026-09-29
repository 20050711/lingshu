"""智能助手媒体工具冒烟（2026-09-15：video_understand / audio_transcribe）。

覆盖：
1. 注册与闸门：select_mode=multi（前端会静默过滤 single）、队列已在 QUEUE_CONFIG 注册
2. 路径越权拒绝（/etc/passwd）与不存在路径提示
3. 非媒体扩展名拒绝
4. 转写缓存：命中 / mtime 变化失效（mock 转写，零成本）
5. **真实链路**：本地 FunASR 转写 3 秒测试音频（免费、本地）+ GLM 视频理解 3 秒测试片（免费档）
   —— 视频理解断言：analysis 非空、产物 md 落盘

用法：cd backend && source scripts/env_aip.sh && python -u tests/av_tools_smoke.py
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.tools import ToolContext, av_tools, get_tool  # noqa: E402
from app.agent.queue.queue_manager import QUEUE_CONFIG, QUEUE_TIMEOUT  # noqa: E402
from app.core.config import get_settings  # noqa: E402

_settings = get_settings()
_RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    _RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")


def _ctx(session_id: str, out_dir: str) -> ToolContext:
    return ToolContext(session_id=session_id, round_id=0, department_id="demo",
                       user_role="employee", client_id="test", output_dir=out_dir, user_id=0)


def _ffmpeg(args: list[str]) -> bool:
    try:
        r = subprocess.run(["ffmpeg", "-y", "-v", "error", *args], capture_output=True, text=True, timeout=120)
        return r.returncode == 0
    except Exception:
        return False


async def main() -> int:
    session_id = f"avtest-{uuid.uuid4().hex[:8]}"
    out_dir = Path(_settings.output_dir) / session_id / "0"
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = Path(_settings.tools_data_dir) / "av_cache"
    cache_before = {f.name for f in cache_dir.iterdir()} if cache_dir.exists() else set()
    ctx = _ctx(session_id, str(out_dir))

    try:
        # ---------- 1. 注册与闸门 ----------
        for n in ("video_understand", "audio_transcribe"):
            t = get_tool(n)
            record(f"{n} 已注册", t is not None)
            if t is None:
                continue
            record(f"{n} select_mode=multi（前端会静默过滤 single）", t.select_mode == "multi", f"={t.select_mode}")
            record(f"{n} 队列已注册", t.queue in QUEUE_CONFIG and t.queue in QUEUE_TIMEOUT, f"queue={t.queue}")
        record("transcribe 队列 = 1/900", QUEUE_CONFIG.get("transcribe") == 1 and QUEUE_TIMEOUT.get("transcribe") == 900)

        # ---------- 2. 路径校验 ----------
        r = await av_tools.run_audio_transcribe({"media_path": "/etc/passwd"}, ctx)
        record("越权路径被拒（/etc/passwd）", bool(r.get("error")) and "无权访问" in r["error"], str(r.get("error"))[:60])
        r = await av_tools.run_audio_transcribe({"media_path": "/data/nope.mp3"}, ctx)
        record("不存在路径被拒", bool(r.get("error")) and "文件不存在" in r["error"])

        # ---------- 3. 非媒体扩展名 ----------
        txt = out_dir / "note.txt"
        txt.write_text("not media", encoding="utf-8")
        r = await av_tools.run_audio_transcribe({"media_path": str(txt)}, ctx)
        record("非媒体扩展名被拒", bool(r.get("error")) and "不是音频/视频文件" in r["error"], str(r.get("error"))[:50])

        # ---------- 4. 转写缓存（mock 转写，零成本）----------
        fake_calls = 0

        async def _fake_transcribe(path: str) -> dict:
            nonlocal fake_calls
            fake_calls += 1
            return {"text": "你好世界。测试语音。", "segments": [{"start": "00:00", "text": "你好世界。"},
                                                                {"start": "00:02", "text": "测试语音。"}],
                    "backend": "local_funasr"}

        orig_transcribe = av_tools.transcribe
        fake_media = out_dir / "fake_media.mp3"
        fake_media.write_bytes(b"\x00" * 64)
        try:
            av_tools.transcribe = _fake_transcribe  # type: ignore[assignment]
            tr1, hit1 = await av_tools._cached_transcribe(str(fake_media))
            tr2, hit2 = await av_tools._cached_transcribe(str(fake_media))
            record("首次转写 miss + 二次命中缓存", (not hit1) and hit2 and fake_calls == 1,
                   f"calls={fake_calls} hit={hit1}->{hit2}")
            record("缓存结果含分段文本", bool(tr2.get("segments")) and "你好世界" in tr2.get("text", ""))
            # mtime 变化 → 指纹变化 → 不误命中
            stat = fake_media.stat()
            import os
            os.utime(fake_media, (stat.st_atime + 5, stat.st_mtime + 5))
            _tr3, hit3 = await av_tools._cached_transcribe(str(fake_media))
            record("mtime 变化后不误命中", (not hit3) and fake_calls == 2, f"calls={fake_calls}")
        finally:
            av_tools.transcribe = orig_transcribe  # type: ignore[assignment]

        # ---------- 5a. 真实本地 FunASR 转写（免费）----------
        tone = out_dir / "tone.wav"
        ok_gen = _ffmpeg(["-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-ar", "16000", "-ac", "1", str(tone)])
        if not ok_gen:
            record("测试音频生成", False, "ffmpeg 生成失败")
        else:
            t0 = time.time()
            tr, _hit = await av_tools._cached_transcribe(str(tone))
            record("真实 FunASR 转写返回结构完整", isinstance(tr, dict) and "text" in tr and "segments" in tr,
                   f"backend={tr.get('backend')} 耗时={time.time()-t0:.1f}s")

        # ---------- 5b. 真实视频理解（GLM 免费档 + 本地转写）----------
        video = out_dir / "test_clip.mp4"
        ok_v = _ffmpeg(["-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=3",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-shortest",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(video)])
        if not ok_v:
            record("测试视频生成", False, "ffmpeg libx264 不可用？")
        else:
            t0 = time.time()
            r = await av_tools.run_video_understand(
                {"media_path": str(video), "instruction": "描述画面内容", "transcribe": True}, ctx)
            ok = bool(r.get("analysis")) and not r.get("error")
            record("真实视频理解（GLM）返回 analysis", ok, f"耗时={time.time()-t0:.1f}s err={str(r.get('error'))[:80]}")
            record("视频理解产物落盘", bool(r.get("file_path")) and (out_dir / f"视频理解_{video.stem}.md").exists(),
                   str(r.get("file_path", ""))[-40:])
            if ok:
                print(f"      analysis 前 120 字：{str(r['analysis'])[:120]}")
            # 视频只调转写（不调画面理解）：audio_transcribe 接受视频文件
            r_t = await av_tools.run_audio_transcribe({"media_path": str(video)}, ctx)
            err = str(r_t.get("error") or "")
            record("视频可只调转写（audio_transcribe 接受视频路径）",
                   (not err) or ("转写结果为空" in err),
                   err[:60] or f"转写成功 {len(str(r_t.get('transcript') or ''))} 字")
    finally:
        # 清理：测试会话目录 + 本次新增的缓存文件
        import shutil
        shutil.rmtree(Path(_settings.output_dir) / session_id, ignore_errors=True)
        if cache_dir.exists():
            for f in cache_dir.iterdir():
                if f.name not in cache_before:
                    f.unlink(missing_ok=True)

    failed = [x for x in _RESULTS if not x[1]]
    print(f"\n== {len(_RESULTS) - len(failed)}/{len(_RESULTS)} PASS ==")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
