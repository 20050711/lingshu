"""LLM 当眼睛：用 GLM 视觉模型分析 Playwright 失败截图（调试辅助）。

用法:
    source scripts/env_aip.sh && python scripts/llm_eye.py <截图路径> [问题描述]

- 模型：GLM-4.6V-Flash（便宜视觉档）；key 读 backend/.env 的 ZHIPU_API_KEY（不打印、不外泄）
- 输出：模型对截图的描述/诊断文本
"""
import base64
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent


def load_env() -> None:
    """轻量读取 backend/.env（只取本脚本用到的键，不依赖 python-dotenv）"""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python scripts/llm_eye.py <截图路径> [问题描述]")
        return 2
    img_path = Path(sys.argv[1])
    if not img_path.exists():
        print(f"[错误] 截图不存在: {img_path}")
        return 2
    question = sys.argv[2] if len(sys.argv) > 2 else "描述这张截图，指出关键 UI 元素与异常状态"

    load_env()
    api_key = os.environ.get("ZHIPU_API_KEY", "")
    base_url = os.environ.get("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
    if not api_key:
        print("[错误] ZHIPU_API_KEY 未配置（backend/.env）")
        return 2

    b64 = base64.b64encode(img_path.read_bytes()).decode()
    for model in ["GLM-4.6V-Flash", "glm-4.6v-flash", "glm-4v-flash"]:
        try:
            r = httpx.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                                {"type": "text", "text": question},
                            ],
                        }
                    ],
                },
                timeout=60,
            )
            if r.status_code == 200:
                print(f"--- GLM 视觉分析（model={model}）---")
                print(r.json()["choices"][0]["message"]["content"])
                return 0
            print(f"[模型 {model} 失败 {r.status_code}] {r.text[:200]}")
        except Exception as e:
            print(f"[模型 {model} 异常] {e}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
