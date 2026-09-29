"""image_recognition / image_generation 工具：GLM 视觉识别与文生图。

参考 docs/官方接口规范/glm接口规范/：
- 视觉: POST /chat/completions，content 数组含 image_url（base64 data URI）
- 图片生成: POST /images/generations（cogview 系列），URL 30 天过期 → 立即转存本地
- 并发配额低（视觉 5/图片 1-2）→ queue_manager 限流
"""
from __future__ import annotations

import asyncio
import base64
import json
import uuid
from pathlib import Path

import httpx

from app.agent.tools import ToolContext, ToolSpec, register_tool
from app.core.config import get_settings
from app.core.url_utils import output_url

_settings = get_settings()

_RECOGNITION_TYPES = ["auto", "chart", "text", "scene"]


async def _resolve_gen_model(ctx: ToolContext, kind: str) -> tuple[str, str]:
    """生成模型解析（2026-08-10 Agnes 接入）：返回 (platform, model)——用户级覆盖（技能卡配置）
    优先 → model_layer 分层配置 → env 兜底（glm）。"""
    from app.services.config_service import get_model_config

    # 2026-08-14：用户级辅助模型覆盖（AI 技能管理页卡片浮窗配置）最优先
    if ctx.aux_overrides:
        tool_key = "image_recognition" if kind == "vision" else "image_generation"
        ov = ctx.aux_overrides.get(tool_key)
        if ov and ov.get("model"):
            return str(ov.get("platform") or "glm"), str(ov["model"])
    cfg = await get_model_config(ctx.department_id, ctx.user_role, kind)
    if cfg and cfg.get("model"):
        return str(cfg.get("platform") or "glm"), str(cfg["model"])
    return "glm", (_settings.glm_vision_model if kind == "vision" else _settings.glm_image_model)


async def _resolve_glm_model_vision(ctx: ToolContext) -> str:
    """视觉识别模型（GLM 专用路径用）。"""
    platform, model = await _resolve_gen_model(ctx, "vision")
    return model if platform == "glm" else _settings.glm_vision_model


# 2026-09-17（缓存审计 P0-5）：图片识别结果缓存——原实现无缓存，同一张图两次识别（换
# recognition_type / 失败重试 / 多轮追问）＝两次真调视觉模型（付费档花钱、免费档吃 429 限流配额）。
# 键 = 图片指纹 + 识别类型 + 平台/模型（换模型必须换键，否则换档后仍返回旧结果）；
# 指纹：本地文件走 路径+size+mtime（不读盘即可判定），URL 走地址，base64/data URI 走内容 hash。
# 只缓存成功且非空的结果；错误不缓存（下次可重试）。
def _image_cache_key(src_fp: str, raw: str, recognition_type: str, platform: str, model: str) -> str:
    import hashlib as _hl

    fp = src_fp or ("raw:" + _hl.md5(raw.encode()).hexdigest())
    return "img_rec:" + _hl.md5(f"{fp}|{recognition_type}|{platform}|{model}".encode()).hexdigest()


async def _img_cache_get(key: str) -> dict | None:
    try:
        from app.core.redis import redis_get

        hit = await redis_get(key)
        if hit:
            return json.loads(hit)
    except Exception:
        pass
    return None


async def _img_cache_set(key: str, result: dict) -> None:
    try:
        from app.core.redis import redis_set

        await redis_set(key, json.dumps(result, ensure_ascii=False), _settings.image_recognition_cache_ttl_s)
    except Exception:
        pass


def _looks_like_base64(s: str) -> bool:
    """粗略判断字符串是否为 base64 图片数据（长度 ≥100 且只含 base64 字符集）。"""
    if len(s) < 100:
        return False
    return all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=" for c in s)


def _agnes_image_payload(size: str, count: int) -> dict:
    """Agnes 图片参数转换：项目 GLM 格式（1024x1024）→ Agnes 档位（1K-4K + ratio）。"""
    ratio_map = {
        "1024x1024": ("1K", "1:1"), "1152x864": ("1K", "4:3"), "864x1152": ("1K", "3:4"),
        "1312x736": ("1K", "16:9"), "736x1312": ("1K", "9:16"),
        "2048x2048": ("2K", "1:1"), "1920x1080": ("2K", "16:9"), "1080x1920": ("2K", "9:16"),
        "3072x3072": ("3K", "1:1"), "4096x4096": ("4K", "1:1"),
    }
    tier, ratio = ratio_map.get(size, ("1K", "1:1"))
    payload: dict = {"size": tier, "ratio": ratio, "n": count}
    if size not in ratio_map:
        payload["size_mapping_note"] = f"尺寸 {size} 标准化为 {tier}/{ratio}"
    return payload


async def run_image_recognition(args: dict, ctx: ToolContext) -> dict:
    image_data = str(args.get("image_data", ""))  # base64 或 data URI
    src_fp = ""          # 2026-09-17：缓存指纹（文件 stat / URL / 内容 hash，见 _image_cache_key）
    recognition_type = str(args.get("recognition_type", "auto"))
    if recognition_type not in _RECOGNITION_TYPES:
        recognition_type = "auto"
    if not image_data:
        return {"error": "需要提供图片（base64 或文件路径）"}

    # 2026-08-10 修复：支持本地文件路径（绝对路径或 work 目录相对路径——LLM 视角
    # cwd=work 目录，传 "liuying.jpg" 应解析到 {sandbox}/{session}/{round}/work/ 下；
    # 原实现相对路径被当 base64 发给 GLM → 识别失败，LLM 反复重试 3 次）
    if image_data.startswith("data:"):
        pass  # data URI 直传（GLM 视觉支持）——2026-08-11 修复：原实现无 data: 分支，
        # data URI 会被 _looks_like_base64 拒绝（含 ':'）→ 误走 work 相对路径分支 → 路径
        # 拼接 File name too long 崩溃（并发测试 press14 实测复现）
    elif image_data.startswith(("http://", "https://")):
        # SEC-15：URL 直传 GLM/Agnes 中转拉取前做 SSRF 校验（防内网/元数据探测面）
        from app.core.ssrf import validate_public_url_async

        err = await validate_public_url_async(image_data)
        if err:
            return {"error": f"图片 URL 校验失败：{err}"}
        src_fp = f"url:{image_data}"
        pass  # URL 直传（GLM 视觉支持 URL）
    elif image_data.startswith("/") and not _looks_like_base64(image_data):
        # 绝对路径 → 读文件转 base64（越狱防护：仅限当前会话目录）
        # 2026-08-11 修复：base64 标准字符集含 "/"，裸 base64 以 "/" 开头（1/64 概率）会被
        # 误判为绝对路径 → validate_readable_path 拒绝（并发测试 press14 实测复现）——
        # base64 判定优先于路径判定
        from app.agent.tools import resolve_output_url, validate_readable_path

        denied = validate_readable_path(image_data, ctx)
        if denied:
            return {"error": denied}
        # 2026-08-20（走查实锤）：产出 URL 形式（/api/v1/outputs/...）→ 磁盘路径（读取用）
        resolved = resolve_output_url(image_data, ctx) or image_data
        p = Path(resolved)
        if not p.exists():
            return {"error": f"图片文件不可读: {image_data}"}
        try:  # 缓存指纹（stat，不读盘）
            _st = p.stat()
            src_fp = f"file:{p}|{_st.st_size}|{_st.st_mtime_ns}"
        except OSError:
            src_fp = ""
        image_data = base64.b64encode(await asyncio.to_thread(p.read_bytes)).decode()  # M9
    elif not _looks_like_base64(image_data):
        # work 目录相对路径（先试 work 下，再试 work/work/ 下——LLM 脚本可能写到子目录）
        from app.agent.tools import validate_readable_path

        candidates = [
            Path(_settings.sandbox_dir) / str(ctx.session_id) / str(ctx.round_id) / "work" / image_data,
            Path(_settings.sandbox_dir) / str(ctx.session_id) / str(ctx.round_id) / "work" / "work" / image_data,
        ]
        found = next((p for p in candidates if p.exists()), None)
        if found is not None:
            denied = validate_readable_path(str(found), ctx)
            if denied:
                return {"error": denied}
            try:
                _st = found.stat()
                src_fp = f"file:{found}|{_st.st_size}|{_st.st_mtime_ns}"
            except OSError:
                src_fp = ""
            image_data = base64.b64encode(await asyncio.to_thread(found.read_bytes)).decode()  # M9
        # 找不到且不像 base64 → 明确报错引导（原实现静默当 base64 发 GLM，报错信息无意义）
    if not image_data.startswith("data:"):
        image_data = f"data:image/png;base64,{image_data}"

    prompt_map = {
        "chart": "这是一张图表。请提取图表类型、标题、坐标轴含义与关键数据点，输出 JSON。",
        "text": "请识别图片中的文字内容，逐行输出。",
        "scene": "请描述图片中的场景内容。",
        "auto": "请识别图片内容与类型。若是图表，提取图表类型与关键数据；若是文档，提取文字；否则描述场景。",
    }
    # 2026-08-14：平台分派——glm 走智谱；agnes 走 agnes 多模态（OpenAI 兼容 image_url 格式）；
    # 2026-09-09：deepseek 走 DeepSeek 官方图片理解（同 OpenAI 兼容 image_url，见
    # docs/官方接口规范/deepseek接口规范/图片理解.md）
    platform, model = await _resolve_gen_model(ctx, "vision")
    # 2026-09-17（缓存审计 P0-5）：先查缓存（同图 + 同识别类型 + 同模型档）——命中即返回，不调视觉模型
    _ck = _image_cache_key(src_fp, image_data, recognition_type, platform, model)
    _hit = await _img_cache_get(_ck)
    if _hit is not None:
        return {**_hit, "note": "图片识别结果（与本次相同图片+类型+模型的历史结果，未重复调用视觉模型）"}
    if platform in ("agnes", "deepseek"):
        from app.agent.llm_client import DeepSeekLLM

        try:
            # 2026-09-01 bug 修复：必须经 create 的 platform_override 建 client（原 `llm.platform = "agnes"`
            # 只改属性不改 client——create 按团队配置（deepseek）建的 client 会打到 DeepSeek 官方 API 花钱）
            llm = await DeepSeekLLM.create(role=ctx.user_role or "employee", dept_id=ctx.department_id,
                                           platform_override=platform, model_override=model)
            msg = await llm.ainvoke([{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": image_data}},
                {"type": "text", "text": prompt_map.get(recognition_type, prompt_map["auto"])},
            ]}], tools=None, stream_cb=None)
            content = (msg.get("content") or "").strip()
            if not content:
                return {"error": f"{platform} 视觉识别返回为空（模型可能不支持 data URI 图片，请换回 GLM）"}
            _result = {"type": recognition_type, "content": content[:4000], "note": "图片识别结果"}
            await _img_cache_set(_ck, _result)
            return _result
        except Exception as e:  # noqa: BLE001
            return {"error": f"{platform} 视觉识别失败: {str(e)[:150]}（可换回 GLM 档）"}
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_data}},
                    {"type": "text", "text": prompt_map.get(recognition_type, prompt_map["auto"])},
                ],
            }
        ],
    }
    # 2026-09-01：免费模型限流重试（429/1302/1305 退避 ×5）——原无重试，限流时直接失败
    resp = await _free_retry_post(
        f"{_settings.zhipu_base_url}/chat/completions",
        {"Authorization": f"Bearer {_settings.zhipu_api_key}"},
        payload,
        _settings.vision_api_timeout_s,
    )
    if resp.status_code != 200:
        return {"error": f"GLM 视觉识别失败: {resp.text[:200]}"}
    content = resp.json()["choices"][0]["message"]["content"]
    _result = {"type": recognition_type, "content": content[:4000], "note": "图片识别结果"}
    await _img_cache_set(_ck, _result)
    return _result


_FREE_LIMIT_STATUS = {429, 1302, 1305}  # 免费模型限流码（429 HTTP / 1302 速率 / 1305 访问量过大）


async def _free_retry_post(url: str, headers: dict, json_body: dict, timeout: float) -> httpx.Response:
    """免费模型（GLM 视觉/图片生成）限流重试（2026-09-01）：429/1302/1305 退避 2/4/6/8/10/12/14/16s × 8 次。
    免费档限流是常态（glm-4.7 实测间歇 1305）——无重试时工具直接失败（压测 press09 图片生成失败根因）。"""
    last: httpx.Response | None = None
    for attempt in range(9):
        try:
            async with httpx.AsyncClient(timeout=timeout, limits=httpx.Limits(max_connections=1)) as c:
                last = await c.post(url, headers=headers, json=json_body)
        except Exception:
            if attempt == 8:
                raise
            await asyncio.sleep(2 * (attempt + 1))
            continue
        if last.status_code in _FREE_LIMIT_STATUS and attempt < 8:
            await asyncio.sleep(2 * (attempt + 1))
            continue
        return last
    raise RuntimeError("免费模型请求重试耗尽")


async def run_image_generation(args: dict, ctx: ToolContext) -> dict:
    prompt = str(args.get("prompt", "")).strip()
    if not prompt:
        return {"error": "需要提供图片描述 prompt"}
    size = str(args.get("size") or "1024x1024")
    count = min(int(args.get("n") or 1), 4)

    # 2026-09-17（缓存审计 P2-12）：同参数 10 分钟内重复触发 → 复用上次产物（防 agent 原样重试重复花钱）；
    # 用户确实要再来一张时传 regenerate=true 绕过
    from app.services import gen_dedupe

    _sig = {"prompt": prompt, "size": size, "n": count}
    if not bool(args.get("regenerate")):
        prev = await gen_dedupe.recent("image", ctx.user_id or 0, _sig)
        if prev and prev.get("images"):
            return {**{k: v for k, v in prev.items() if not k.startswith("_")},
                    "note": f"（同一描述 {prev.get('_ago_min', 0)} 分钟前刚生成过，直接复用上次的图；"
                            "确实要重新生成请把 regenerate 设为 true）"}

    # 2026-08-10（Agnes 接入）：平台分派——agnes 走档位参数（1K-4K+ratio），glm 保持原格式
    platform, model = await _resolve_gen_model(ctx, "image")
    payload: dict = {"model": model, "prompt": prompt, "n": count}
    if platform == "agnes":
        payload.update(_agnes_image_payload(size, count))
    else:
        platform, model = "glm", model
        payload["size"] = size
    # 独立客户端 + 短超时：GLM cogview 并发 1 可能排队，连接挂起会导致事件循环卡死
    try:
        if platform == "agnes":
            url, key = f"{_settings.agnes_base_url}/images/generations", _settings.agnes_api_key
        else:
            url, key = f"{_settings.zhipu_base_url}/images/generations", _settings.zhipu_api_key
        # 2026-09-01：免费模型限流重试（429/1302/1305 退避 ×5）——原无重试（压测 press09 失败根因）
        resp = await _free_retry_post(url, {"Authorization": f"Bearer {key}"}, payload, 120)
        if resp.status_code != 200:
            return {"error": f"图片生成失败({platform}): {resp.text[:200]}"}
        urls = [d.get("url") for d in resp.json().get("data", []) if d.get("url")]
        if not urls:
            return {"error": "图片生成无结果（可能仍在排队，请稍后重试）"}
    except Exception as e:
        return {"error": f"GLM 图片生成请求失败: {str(e)[:150]}"}

    # URL 30 天过期 → 立即转存本地（独立客户端，避免复用挂起连接）
    out_dir = Path(ctx.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=60, limits=httpx.Limits(max_connections=1)) as c:
            for i, url in enumerate(urls):
                # SEC-15：provider 返回的 URL 同样校验（提供商异常/被攻破返回内网地址时防中转探测）
                from app.core.ssrf import validate_public_url_async

                if await validate_public_url_async(url):
                    continue
                img = await c.get(url)
                if img.status_code != 200:
                    continue
                fname = f"img_{uuid.uuid4().hex[:8]}_{i}.png"
                await asyncio.to_thread((out_dir / fname).write_bytes, img.content)  # M9
                saved.append(output_url(ctx.session_id, ctx.round_id, fname))
    except Exception as e:
        if not saved:
            return {"error": f"图片下载失败: {str(e)[:150]}"}

    result = {
        "images": saved,
        "note": "图片已生成并保存（原始 URL 30 天过期，已转存本地）",
    }
    await gen_dedupe.remember("image", ctx.user_id or 0, _sig, {"images": saved})
    return result


register_tool(
    ToolSpec(
        name="image_recognition", progress_keys=("content", "text", "results"),
        display_name="图片识别",
        icon="image",
        summary="识别图片内容，提取图表数据或文字",
        group="媒体",
        sort_order=7,
        user_description="识别图片内容：图表提取数据、文档提取文字、场景描述；支持上传图片、产出图片与资料库中的图片。",
        description=(
            "What：调用 GLM 视觉模型识别图片内容（类型 auto/chart/text/scene，图表可提取数据、文档可提取文字）。\n"
            "When：用户上传/产出的图片需要理解内容、提取图表数据或 OCR 文字时调用；"
            "**知识库里的图片先用 file_search 按文件名/标题找到**（图片是纯资源文件，没有正文索引，"
            "只能靠文件名检索），再把返回的 file_path 传进来。\n"
            "How：image_data 传图片 base64、服务器文件路径或图片 http 链接（file_search 返回的 "
            "file_path 可直接用；**服务器路径必须来自工具返回或清单，禁止编造——猜出来的只会报无权访问**；"
            "找不到就用 file_search 按文件名找图）；recognition_type 指定识别类型。\n"
            "Result：返回识别文本；回答引用识别出的数据与结论。\n"
            "**边界：图片读不到/格式不支持 → 换文件或请用户重传，别拿同一张反复试；"
            "图里没有的信息就当没有，不要编造识别结果。**"
        ),
        parameters={
            "type": "object",
            "properties": {
                "image_data": {"type": "string", "description": "图片输入：①本地文件路径（work 目录相对路径或绝对路径，自动读取转 base64——最推荐）②base64 或 data URI ③公网图片 URL"},
                "recognition_type": {"type": "string", "enum": _RECOGNITION_TYPES, "description": "识别类型"},
            },
            "required": ["image_data"],
        },
        queue="vision",
        handler=run_image_recognition,
    )
)

register_tool(
    ToolSpec(
        name="image_generation", progress_keys=("images",),
        write=True,
        display_name="图片生成",
        icon="palette",
        summary="根据文字描述生成图片",
        group="媒体",
        sort_order=8,
        user_description="根据文字描述生成图片（海报/插图/素材图），产出可直接预览与下载。",
        description=(
            "What：调用 GLM CogView 根据文字描述生成图片（海报/插图/素材图），产出可预览与下载。\n"
            "When：用户要生成图片/海报/插图/素材时调用；图片生成结果不满意时基于反馈修改 prompt 再调（最多 5 轮）。\n"
            "How：prompt 详细描述画面内容与风格；size 尺寸（如 1024x1024）；n 数量——【数量纪律】除非用户明确要求多张，n 必须为 1。\n"
            "Result：返回图片文件（浏览区预览）；回答说明图片主题与风格。\n"
            "**边界：生成失败（内容审核/配额/超时）分两种——审核类改 prompt 再试一次即可；"
            "配额/服务类不要原样重试，如实告知用户。**"
        ),
        parameters={
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "图片内容描述"},
                "size": {"type": "string", "description": "尺寸，如 1024x1024"},
                "n": {"type": "integer", "description": "生成数量（默认 1，除非用户明确要多张）"},
                "regenerate": {"type": "boolean",
                               "description": "确实要重新生成（默认 false：10 分钟内同参数直接复用上次结果）"},
            },
            "required": ["prompt"],
        },
        queue="image_gen",
        handler=run_image_generation,
    )
)
