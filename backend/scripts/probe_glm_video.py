"""D1 前置探测（2026-08-14，只读不落库）：验证 GLM 原生视频输入可行性。

官方对话接口仅支持 video_url（URL 形式）——本地上传视频不是 URL。本脚本依次尝试：
1. GLM 文件上传通道（POST {base}/files，purpose=file-extract）→ 拿 file_id
2. 用 file_id 构造 video_url 消息调 glm-4.6v（或 --model 指定）问"视频讲了什么"

结论三选一：
- native：文件上传通道接受 mp4 且 video_url 调通 → D1 全切可行
- partial：上传通道拒绝 mp4（仅图片）→ 不可行，回落抽帧
- api-error：接口报错（查 .env key/网络）

用法：
    cd backend && source scripts/env_aip.sh && python -u scripts/probe_glm_video.py [视频路径] [--model glm-4.6v]
    默认视频 /tmp/test_clip.mp4（E2 夹具；prep_test_data.sh 已恢复）
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from app.core.config import get_settings

_settings = get_settings()


async def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--model")]
    video = Path(args[0] if args else "/tmp/test_clip.mp4")
    model = "glm-4.6v"
    for a in sys.argv[1:]:
        if a.startswith("--model"):
            model = a.split("=", 1)[1]
    if not video.exists():
        print(f"[ERROR] 视频不存在: {video}（先跑 deploy/prep_test_data.sh 恢复夹具）")
        return 1

    base = _settings.zhipu_base_url.rstrip("/")
    key = _settings.zhipu_api_key
    headers = {"Authorization": f"Bearer {key}"}
    print(f"== GLM 视频输入探测 ==\n视频: {video}（{video.stat().st_size} 字节）\nbase: {base}\n")

    async with httpx.AsyncClient(timeout=120) as c:
        # 1. 文件上传通道
        print("[1] 尝试文件上传通道 POST /files ...")
        try:
            with open(video, "rb") as f:
                r = await c.post(f"{base}/files",
                                 headers=headers,
                                 files={"file": (video.name, f, "video/mp4")},
                                 data={"purpose": "file-extract"})
            print(f"    status={r.status_code}")
            if r.status_code >= 400:
                print(f"    body={r.text[:300]}")
                if r.status_code == 404:
                    print("\n[结论] partial：GLM 无 /files 通道（或路径不同）→ 回落抽帧")
                    return 2
                print("\n[结论] partial：文件上传通道拒绝 mp4 → 回落抽帧")
                return 2
            fid = (r.json() or {}).get("id")
            if not fid:
                print(f"    body={r.text[:300]}")
                print("\n[结论] partial：响应无 file id → 回落抽帧")
                return 2
            print(f"    file_id={fid}")
        except httpx.HTTPError as e:
            print(f"    [ERROR] 网络异常: {str(e)[:200]}")
            print("\n[结论] api-error（检查 .env GLM key 与网络）")
            return 3

        # 2. video_url 消息调模型
        print(f"[2] 用 file_id 构造 video_url 调 {model} ...")
        try:
            r2 = await c.post(
                f"{base}/chat/completions",
                headers=headers,
                json={
                    "model": model,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "video_url", "video_url": {"url": fid}},
                            {"type": "text", "text": "视频讲了什么？一句话回答。"},
                        ],
                    }],
                },
            )
            print(f"    status={r2.status_code}")
            if r2.status_code >= 400:
                print(f"    body={r2.text[:300]}")
                print("\n[结论] partial：上传成功但 video_url 调用失败 → 需人工分析；暂回落抽帧")
                return 2
            content = (r2.json() or {}).get("choices", [{}])[0].get("message", {}).get("content")
            print(f"    模型回复: {str(content)[:200]}")
            if not content:
                print("\n[结论] partial：回复为空（模型未识别视频）→ 暂回落抽帧")
                return 2
            print("\n[结论] native：GLM 原生视频输入可行（上传通道+video_url 全通）→ D1 全切可实施")
            return 0
        except httpx.HTTPError as e:
            print(f"    [ERROR] 网络异常: {str(e)[:200]}")
            print("\n[结论] api-error（检查 .env GLM key 与网络）")
            return 3


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
