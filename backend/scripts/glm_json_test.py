"""GLM 结构化输出（response_format=json_object）实测脚本（2026-09-11 走查问题10）。

目的：验证 glm-4.7-flash 在 json_object 模式下**长输出**的格式质量——
纯 JSON / markdown 包装 / 截断 / 解析成功率，给「是否给 GLM 调用点加 response_format」提供依据。

用法：cd backend && source scripts/env_aip.sh && python scripts/glm_json_test.py [--no-format]
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.llm_client import create_text_client  # noqa: E402
from app.core.text_utils import parse_llm_json  # noqa: E402

# 长输出任务：60 条记录 + 每条 4 字段（贴近真实「评论/记录落表」场景）
SYSTEM = (
    "你是数据分析助手。只输出一个 JSON 对象，不要任何解释、不要 markdown 代码块。"
    "结构：{\"note_id\": string, \"title\": string, \"comments\": [{\"rank\": number, "
    "\"nickname\": string, \"content\": string, \"likes\": number, \"ip\": string}], "
    "\"summary\": string}。comments 必须正好 60 条，content 为 15~40 字的虚构中文评论。"
)


async def run(use_format: bool, model_name: str) -> None:
    cfg = {"platform": "glm", "model": model_name}
    client, model, tkw = create_text_client(cfg)
    kwargs = {"model": model, "messages": [{"role": "system", "content": SYSTEM},
                                           {"role": "user", "content": "为一条拼豆图纸笔记生成 60 条评论分析。"}],
              "max_tokens": 8000, **tkw}
    if use_format:
        kwargs["response_format"] = {"type": "json_object"}
    t0 = time.time()
    resp = await client.chat.completions.create(**kwargs)
    dt = time.time() - t0
    content = resp.choices[0].message.content or ""
    stripped = content.strip()
    print(f"--- model={model_name} response_format={'json_object' if use_format else '未设置'} ---")
    print(f"耗时 {dt:.1f}s | 字符数 {len(content)} | usage {getattr(resp, 'usage', None)}")
    print(f"首 60 字符: {stripped[:60]!r}")
    print(f"末 60 字符: {stripped[-60:]!r}")
    print(f"以 ``` 包装: {stripped.startswith('```')}")
    try:
        data = json.loads(stripped)
        n = len(data.get("comments") or [])
        print(f"json.loads: OK | comments 条数 = {n}")
    except Exception as e:
        print(f"json.loads: FAIL {e}")
    try:
        data2 = parse_llm_json(content)
        print(f"parse_llm_json: OK type={type(data2).__name__} "
              f"comments={len(data2.get('comments') or []) if isinstance(data2, dict) else '-'}")
    except Exception as e:
        print(f"parse_llm_json: FAIL {type(e).__name__} {e}")
    print()


async def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    model_name = args[0] if args else "glm-4.7-flash"
    use_format = "--no-format" not in sys.argv
    await run(use_format, model_name)
    if "--both" in sys.argv:
        await run(not use_format, model_name)


if __name__ == "__main__":
    asyncio.run(main())
