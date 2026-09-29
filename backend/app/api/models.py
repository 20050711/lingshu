"""模型选择选项接口（D3，2026-08-14）：QA 界面/技能卡/定制化工具页的模型选择框数据源。

候选来自 model_catalog（唯一维护点，与 admin 配置页同源）；业务侧鉴权（get_business_user）。
2026-08-14 扩展：vision/image/video 层下发（技能卡辅助模型配置）+ 用户级覆盖存取
（GET/PUT /models/aux-preferences，存 users.aux_model_overrides）。
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.middleware import get_business_user
from app.services.model_catalog import get_model_catalog

_settings = get_settings()

router = APIRouter(prefix="/models", tags=["models"])


def _layer_list(catalog: dict, key: str) -> list[dict]:
    return [
        {"platform": p["platform"], "model": m}
        for p in catalog.get(key, []) for m in p.get("models", [])
    ]


@router.get("/options")
async def model_options(user: dict = Depends(get_business_user)):
    """下发模型选择候选（前端不硬编码——同 /admin/model-catalog 约定）。

    返回：
    - llm：主对话模型候选 [{platform, model}]
    - llm_aux：辅助任务模型候选（记忆库等）
    - vision：视觉识别候选（图片识别工具；GLM + agnes 多模态）
    - image：图片生成候选（GLM + agnes）
    - video：视频生成候选（agnes）
    - thinking：思考强度选项（off/low/high/max；关低中高自选不限白名单）
    """
    catalog = get_model_catalog()
    return {
        # 2026-09-08：免费档定义下发（config 维护；前端「免费档」按钮不再硬编码模型名）
        "free": {"platform": _settings.free_llm_platform, "model": _settings.free_llm_model},
        "llm": _layer_list(catalog, "llm"),
        "llm_aux": _layer_list(catalog, "llm_aux"),
        # 2026-08-14：vision 栏直接来自 catalog（agnes 多模态模型含 3.0/2.5，不再单独硬拼——
        # 2026-09-08 去掉重复维护点）
        "vision": [
            {"platform": p["platform"], "model": m}
            for p in catalog.get("vision", []) for m in p.get("models", [])
        ],
        "image": _layer_list(catalog, "image"),
        "video": _layer_list(catalog, "video"),
        # 2026-09-01：三档对齐——运行时自选扩为 off/low/high/max（与 admin 配置页
        # 关/低/中/高 一致；high=中强度、max=高强度，官方 reasoning_effort 透传）
        "thinking": [
            {"key": "off", "label": "关闭思考（快）"},
            {"key": "low", "label": "低强度思考"},
            {"key": "high", "label": "中强度思考"},
            {"key": "max", "label": "高强度思考"},
        ],
    }


class AuxPrefsRequest(BaseModel):
    overrides: dict = {}   # {tool_id: {platform, model}}；tool_id ∈ image_recognition/image_generation/memory/video_generate


@router.get("/aux-preferences")
async def get_aux_preferences(user: dict = Depends(get_business_user)):
    """读取当前用户保存的辅助模型覆盖（AI 技能管理页卡片浮窗配置，system_config KV）。"""
    from app.services.config_service import get_user_aux_overrides

    return {"overrides": await get_user_aux_overrides(int(user["user_id"])) or {}}


@router.put("/aux-preferences")
async def put_aux_preferences(req: AuxPrefsRequest, user: dict = Depends(get_business_user)):
    """保存当前用户的辅助模型覆盖（校验 key 白名单 + platform/model 合法性；空 dict 清空=恢复团队档）。"""
    from app.services.config_service import set_user_aux_overrides
    from app.services.model_catalog import MODEL_CATALOG

    allowed = {"image_recognition", "image_generation", "memory", "video_generate"}
    # 问题 11 横向修复（2026-08-17）：platform/model 必须同时合法且匹配——
    # 原实现只校验 model 非空，任意平台组合可入库（glm 模型配 deepseek 平台等），
    # 下游 client 平台分派后必然 400。候选集 = 目录全段模型 + platform 白名单。
    valid_platforms = {"deepseek", "agnes", "glm"}
    all_models = {m for seg in MODEL_CATALOG.values() for p in seg for m in p["models"]}
    overrides: dict = {}
    for k, v in (req.overrides or {}).items():
        if k not in allowed or not isinstance(v, dict) or not v.get("model"):
            continue
        plat = str(v.get("platform") or "glm" if k.startswith("image") else "deepseek")
        if plat not in valid_platforms or str(v["model"]) not in all_models:
            continue
        ov = {"platform": plat, "model": str(v["model"])}
        # 2026-08-20：thinking 档位纳入保存（SkillsPage 辅助模型卡选了思考档，原白名单过滤
        # 只留 platform/model → 保存即丢失，消费侧 get_aux_model_cfg_for_user 读到的 thinking 恒 None）
        if v.get("thinking") in ("off", "low", "high", "max"):
            ov["thinking"] = v["thinking"]
        overrides[k] = ov
    await set_user_aux_overrides(int(user["user_id"]), overrides)
    return {"ok": True, "overrides": overrides}
