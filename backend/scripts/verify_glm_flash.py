"""验证 GLM-4-Flash-250414 为文本 LLM（OpenAI 兼容端点，非 thinking）。

用法：cd backend && source scripts/env_aip.sh && python scripts/verify_glm_flash.py
预期：输出 model=glm-4-flash-250414 与一段正常回复；thinking 参数被拒绝（该模型无思考模式）。
key 读 backend/.env 的 ZHIPU_API_KEY（config.zhipu_api_key），不新增配置。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openai import AsyncOpenAI


async def main() -> None:
    from app.core.config import get_settings

    s = get_settings()
    if not s.zhipu_api_key:
        raise SystemExit("ZHIPU_API_KEY 未配置（backend/.env）")
    client = AsyncOpenAI(api_key=s.zhipu_api_key, base_url=s.zhipu_base_url, timeout=30, max_retries=0)

    # 1) 常规对话（不传 thinking）——确认是文本 LLM
    resp = await client.chat.completions.create(
        model="glm-4-flash-250414",
        messages=[{"role": "user", "content": "只回复两个字：正常"}],
    )
    print("model:", resp.model)
    print("content:", resp.choices[0].message.content)
    print("usage:", resp.usage)

    # 2) 反向确认：若传 thinking.enabled 会 400（证明不支持思考，防止未来误配）
    try:
        await client.chat.completions.create(
            model="glm-4-flash-250414",
            messages=[{"role": "user", "content": "hi"}],
            extra_body={"thinking": {"type": "enabled"}},
        )
        print("WARN: 该模型接受了 thinking 参数（与预期不符，需复核）")
    except Exception as e:  # noqa: BLE001
        print("thinking 参数被拒绝（符合预期，不开思考）:", str(e)[:120])


if __name__ == "__main__":
    asyncio.run(main())
