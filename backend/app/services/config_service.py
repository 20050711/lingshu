"""团队级配置服务（三期 M13：system_config 的代码消费侧；B4 2026-08-07：三栏模型分层扩展）。

- get_model_config(dept_id, role, kind)：模型分层（key=model_layer.{dept_id} → model_layer.default → env 兜底）
  kind ∈ llm（LLM 对话）/ vision（视觉识别）/ image（多模态生成）；旧结构（{"employee":{"model","effort"}}）兼容为 llm
- get_model_layer(dept_id, role)：兼容旧调用方的 llm 薄封装
- get_dept_tools(dept_id)：团队工具黑名单（key=dept_block.{dept_id}，2026-09-01 白名单→黑名单：勾选=禁用，缺省=None 无禁用）

约定：system_config.value 是 TEXT 列（非 JSONB），读写自行 json.dumps/loads（HANDOVER 踩坑 3/12 边界）；
60s 进程内缓存：配置修改后 ≤60s 生效，无需重启。
"""
from __future__ import annotations

import json
import time

from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import get_global_engine
from app.core.exceptions import app_error

_settings = get_settings()

# 两个**互相独立**的缓存共用同一容器：kv=`_load_all` 的全量 system_config，mcp_active=
# mcp_tools.status='active'。各自有独立时间戳（ts / mcp_ts）——2026-09-10 走查 bug：曾共用
# 一个 ts，先刷新者顶掉后者的时间戳，后者的读路径就一直拿到空/陈旧数据（详见 get_active_mcp_tool_ids）。
_CACHE: dict = {"ts": 0.0, "kv": {}, "mcp_ts": 0.0}
_CACHE_TTL = 60


async def _load_all() -> dict[str, str]:
    """读全部 system_config 到缓存（60s TTL）。"""
    now = time.time()
    if now - _CACHE["ts"] < _CACHE_TTL:
        return _CACHE["kv"]
    engine = get_global_engine()
    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT key, value FROM system_config"))).all()
    _CACHE["ts"] = now
    _CACHE["kv"] = {r[0]: r[1] for r in rows}
    return _CACHE["kv"]


# 合法思考强度档位（2026-08-11：旧配置曾出现 effort=medium 静默生效导致 LLM 不遵守
# 计划流程/工具路由——非法档位回落 high 兜底）
# 2026-08-17（官方三档适配）：官方 reasoning_effort 支持 low/high/max（medium→high、
# xhigh→high 映射，见 docs/官方接口规范/deepseek接口规范/思考模式.md 映射表）——
# 低档 low 合法；medium/xhigh 等旧值按官方映射回落 high
_VALID_EFFORTS = {"low", "high", "max"}

# ---------- 上传限制（2026-09-16 用户定：动态化、管理端统一维护） ----------

# 为什么动态：原先散在 config.py 各处（静态值 + 改一处要改代码重发）。现统一收进
# system_config 的 upload_limits 键，管理端「配置管理 → 上传限制」维护，写入即失效缓存
# （≤60s，与其它配置一致）。
# 每项：(静态默认来源属性, 名字（说人话）, 一句说明, 硬上限MB)——默认值取 config.py（.env 兜底覆盖），
# 管理员没配过的项回落默认，新增键不会失效。
UPLOAD_LIMITS: dict[str, tuple[str, str, str, int]] = {
    "session_doc_mb": ("session_doc_upload_max_mb", "问答 · 文档",
                       "在问答里上传 PDF / Word / Excel 等文档的最大体积。", 102400),
    "media_mb": ("media_upload_max_mb", "问答 / 媒体工具 · 音视频",
                 "上传录音、视频、音频文件的最大体积（转写与画面分析按此判断）。", 102400),
    "kb_mb": ("max_upload_size_mb", "知识库 · 数据表格 · 简历",
              "知识库文档、数据表格（xlsx/csv）、简历文件的最大体积（三处共用）。", 102400),
    "video_mb": ("video_max_size_mb", "媒体工具 · 会议纪要",
                 "视频理解与会议录音文件的最大体积（两者共用）。", 102400),
    "tool_pkg_mb": ("tool_download_max_size_mb", "工具包",
                    "管理员上传工具包（供同事下载）的最大体积。", 102400),
}
_UPLOAD_LIMITS_KEY = "upload_limits"


async def get_upload_limits() -> dict[str, int]:
    """当前生效的上传上限（MB）：upload_limits 键覆盖默认，缺项回落 config.py 默认。"""
    raw = (await _load_all()).get(_UPLOAD_LIMITS_KEY) or ""
    saved: dict = {}
    if raw.strip():
        try:
            parsed = json.loads(raw)
            saved = parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            saved = {}
    settings = get_settings()
    out: dict[str, int] = {}
    for key, (attr, _name, _desc, hard_max) in UPLOAD_LIMITS.items():
        default = int(getattr(settings, attr, 100))
        try:
            val = int(saved.get(key, default))
        except (TypeError, ValueError):
            val = default
        out[key] = val if 0 < val <= hard_max else default
    return out


async def upload_limit_mb(key: str) -> int:
    """单个上限快捷读（消费点一行改完）：limit = await upload_limit_mb("kb_mb") * 1024 * 1024。"""
    return (await get_upload_limits())[key]


async def set_upload_limits(patch: dict) -> dict[str, int]:
    """更新上传上限（只认白名单键；越界/非法值回退默认）。返回更新后的全量。"""
    current = await get_upload_limits()
    for key, val in (patch or {}).items():
        if key not in UPLOAD_LIMITS:
            raise app_error("E003", f"未知的上传限制项: {key}", status_code=400)
        try:
            n = int(val)
        except (TypeError, ValueError):
            raise app_error("E003", f"{UPLOAD_LIMITS[key][1]} 必须是整数（MB）", status_code=400)
        if n < 1 or n > UPLOAD_LIMITS[key][3]:
            raise app_error("E003", f"{UPLOAD_LIMITS[key][1]} 需在 1~{UPLOAD_LIMITS[key][3]}MB 之间",
                            status_code=400)
        current[key] = n
    engine = get_global_engine()
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO system_config (key, value, description, updated_at) "
                 "VALUES (:k, :v, :d, NOW()) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()"),
            {"k": _UPLOAD_LIMITS_KEY, "v": json.dumps(current, ensure_ascii=False),
             "d": "各入口上传大小上限（MB，管理端配置管理维护）"})
    _CACHE["ts"] = 0    # 写后即失效，下一次读立刻生效
    return current


def upload_limits_meta() -> list[dict]:
    """管理端展示用元数据（顺序稳定；项名/文案全在后端，前端只渲染）。"""
    settings = get_settings()
    return [{"key": k, "name": v[1], "desc": v[2], "default": int(getattr(settings, v[0], 100)),
             "max": v[3]} for k, v in UPLOAD_LIMITS.items()]


# 合法思考强度档位（2026-08-11：旧配置曾出现 effort=medium 静默生效导致 LLM 不遵守
# 计划流程/工具路由——非法档位回落 high 兜底）
# 2026-08-17（官方三档适配）：官方 reasoning_effort 支持 low/high/max（medium→high、
# xhigh→high 映射，见 docs/官方接口规范/deepseek接口规范/思考模式.md 映射表）——
# 低档 low 合法；medium/xhigh 等旧值按官方映射回落 high
_VALID_EFFORTS = {"low", "high", "max"}


def _sanitize_effort(effort: str | None) -> str:
    e = str(effort or "high").strip().lower()
    return e if e in _VALID_EFFORTS else "high"


def _parse_segment(seg, kind: str) -> dict | None:
    """解析 model_layer 某角色段中的 kind 配置。

    新结构 {"employee": {"llm": {platform,model,effort}, "vision": {...}, "image": {...}}, ...}
    旧结构兼容：seg 直接含 "model" 键（{"model","effort"}）视为 llm 段（platform 默认 deepseek）。
    """
    if not isinstance(seg, dict):
        return None
    if "model" in seg:
        if kind != "llm":
            return None
        result = {"platform": str(seg.get("platform") or "deepseek"), "model": str(seg["model"]),
                  "effort": _sanitize_effort(seg.get("effort"))}
        if "thinking" in seg:
            result["thinking"] = bool(seg["thinking"])
        return result
    sub = seg.get(kind)
    if not isinstance(sub, dict) or not sub.get("model"):
        return None
    result = {"platform": str(sub.get("platform") or "deepseek"), "model": str(sub["model"]),
              "effort": _sanitize_effort(sub.get("effort"))}
    if "thinking" in sub:
        result["thinking"] = bool(sub["thinking"])
    if kind in ("llm_aux", "llm_tools"):
        # 使用方向（任务 key 数组 JSON 字符串）：配置页下拉选项，消费侧按任务匹配
        # （llm_tools=定制化工具独立档，2026-09-02）
        result["usage"] = sub.get("usage")
    return result


async def get_model_config(dept_id: str, role: str, kind: str = "llm") -> dict | None:
    """按团队/角色/类别返回 {platform, model, effort}；未配置返回 None（调用方回退 env）。"""
    kv = await _load_all()
    keys = [f"model_layer.{dept_id}", "model_layer.default"] if dept_id else ["model_layer.default"]
    for key in keys:
        raw = kv.get(key)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        cfg = _parse_segment(data.get(role) if isinstance(data, dict) else None, kind)
        if cfg:
            return cfg
    return None


async def get_model_layer(dept_id: str, role: str) -> tuple[str, str] | None:
    """兼容旧调用方：按团队/角色返回 (model, reasoning_effort)（llm 段）。"""
    cfg = await get_model_config(dept_id, role, "llm")
    if cfg:
        return cfg["model"], cfg.get("effort") or "high"
    return None


async def get_aux_model_cfg(dept_id: str | None, role: str | None) -> dict | None:
    """LLM 辅助任务模型配置（llm_aux 段）：按团队/角色分层；未配置返回 None（调用方回退 env employee）。"""
    if not dept_id or not role:
        return None
    return await get_model_config(dept_id, role, "llm_aux")


def _aux_usage_tasks(cfg: dict | None) -> set[str] | None:
    """解析 llm_aux.usage（任务 key 数组，可能为 JSON 数组或 JSON 数组字符串）；解析失败/为空/未配置返回 None（=未限定，全任务可用）。

    兼容旧数据：usage 曾为自由文本（中文描述），解析失败视为未限定。
    """
    if not cfg:
        return None
    usage = cfg.get("usage")
    if not usage:
        return None
    if isinstance(usage, list):
        return {str(t) for t in usage}
    if isinstance(usage, str):
        try:
            tasks = json.loads(usage)
        except (json.JSONDecodeError, TypeError):
            return None
        return {str(t) for t in tasks} if isinstance(tasks, list) else None
    return None


# 2026-09-02：定制化工具任务（独立 llm_tools 档）——简历/会议纪要约工具任务，
# 配置页「定制化工具模型配置」栏勾选；辅助任务（kb 检索/摘要/记忆/历史摘要等后台静默、
# 质量低不影响使用的）走 llm_aux 档（口径：后台静默执行的质量低任务=辅助任务）
TOOL_TASK_KEYS = {
    "video", "resume", "meeting",
}


async def _aux_task_layers(dept_id: str | None, role: str | None, kind: str) -> list[dict]:
    """该档的全部候选层，按「团队档 → 默认档」优先级。

    2026-09-16（费用事故）：原实现走 get_model_config——它**只返回第一个解析成功的层**，
    于是"团队档有这一段、但 usage 没勾这个任务"会整体回退到 env 默认（= DeepSeek 官方计费），
    而不是文档写的"回退默认模型"（model_layer.default 同档）。**新增辅助任务时务必确认
    各档 usage 已勾选**，否则后台静默任务会整批打到付费默认档。
    """
    kv = await _load_all()
    keys = [f"model_layer.{dept_id}", "model_layer.default"] if dept_id else ["model_layer.default"]
    out: list[dict] = []
    for key in keys:
        raw = kv.get(key)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        cfg = _parse_segment(data.get(role) if isinstance(data, dict) else None, kind)
        if cfg:
            out.append(cfg)
    return out


def _usage_hit(cfg: dict | None, task: str) -> bool:
    """该档是否认领此任务：usage 未配置/解析失败 = 不限任务（兼容旧数据）；否则须显式勾选。"""
    tasks = _aux_usage_tasks(cfg)
    return tasks is None or task in tasks


def _free_llm_cfg() -> dict:
    """免费档兜底（agnes-3.0-flash，$0）——辅助/工具任务**不得**因配置缺口落到计费模型。"""
    return {"platform": _settings.free_llm_platform, "model": _settings.free_llm_model}


async def get_aux_model_cfg_for_task(dept_id: str | None, role: str | None, task: str) -> dict | None:
    """按团队/角色/任务解析 LLM 辅助/工具任务模型配置。

    2026-09-02：工具任务先查 llm_tools 独立档 → 未配置/未勾选回退 llm_aux
    （存量团队无 llm_tools 档不受影响，原 llm_aux 勾选继续生效）。
    2026-09-16：**逐层**按 usage 过滤（团队档 → 默认档，命中即用）；两层都没认领 → 回退**免费档**
    （原来回退 None = 调用方落 env 默认的 DeepSeek 官方，静默计费）。
    """
    if task in TOOL_TASK_KEYS:
        for cfg in await _aux_task_layers(dept_id, role, "llm_tools"):
            if _usage_hit(cfg, task):
                return cfg
    for cfg in await _aux_task_layers(dept_id, role, "llm_aux"):
        if _usage_hit(cfg, task):
            return cfg
    return _free_llm_cfg()


async def get_aux_model_cfg_for_user(user_id: int, task: str) -> dict | None:
    """按用户解析 LLM 辅助任务模型配置（查 users 表拿团队/角色；用户不存在回退免费档，见下）。

    用于后台任务（知识库摘要/记忆提取/简历评分）——任务只有 user_id，无运行期 dept/role。
    task：辅助任务 key（与配置页"使用方向"下拉一致，见 model_catalog.AUX_TASKS）。
    """
    engine = get_global_engine()
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT department_id, role FROM users WHERE id=:uid"), {"uid": user_id}
            )
        ).first()
    if row:
        # 2026-08-14：用户级覆盖优先（AI 技能管理页记忆库档，system_config KV）
        # 2026-08-17 修复（问题 11 横向）：原硬编码 "memory" key 忽略 task 参数——一个"记忆库"
        # 覆盖会污染 video/resume/kb_summary 等全部辅助任务；改为按 task 匹配（兼容旧数据
        # "memory" 键：无 task 专属键时回退 memory 键，避免存量配置失效）
        overrides = await get_user_aux_overrides(user_id)
        ov = (overrides or {}).get(task) or (overrides or {}).get("memory")
        if ov and ov.get("model"):
            # 2026-08-18：thinking 透传（AI 技能页思考强度配置；None/缺省=跟随平台默认）
            cfg: dict = {"platform": str(ov.get("platform") or "deepseek"), "model": str(ov["model"])}
            if ov.get("thinking") is not None:
                cfg["thinking"] = ov["thinking"]
            return cfg
        return await get_aux_model_cfg_for_task(str(row.department_id), str(row.role), task)
    # 用户不存在（如 uploaded_by 为空传 0）→ 免费档，不再回退 None（= env 默认的 DeepSeek 官方计费）
    return _free_llm_cfg()


async def get_user_aux_overrides(user_id: int) -> dict | None:
    """用户级辅助模型覆盖（key=user_aux_models.{user_id}，2026-08-14 AI 技能管理页配置）。

    值：JSON 字符串 {"image_recognition"|"image_generation"|"memory"|"video_generate": {platform, model}}。
    消费侧：media_tools/video_gen/chat_service 注入 ctx.aux_overrides（优先于团队档）。
    """
    if not user_id:
        return None
    kv = await _load_all()
    raw = kv.get(f"user_aux_models.{user_id}")
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


async def set_user_aux_overrides(user_id: int, overrides: dict) -> None:
    """保存用户级辅助模型覆盖（system_config KV；空 dict 删行=恢复团队档）。"""
    if not user_id:
        return
    engine = get_global_engine()
    key = f"user_aux_models.{user_id}"
    if not overrides:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM system_config WHERE key=:k"), {"k": key})
        _CACHE["ts"] = 0  # 失效缓存（下次 _load_all 重读）
        return
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO system_config (key, value, description, updated_at) "
                 "VALUES (:k, :v, :d, NOW()) "
                 "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()"),
            {"k": key, "v": json.dumps(overrides, ensure_ascii=False),
             "d": "用户级辅助模型覆盖（AI 技能管理页配置）"},
        )
    _CACHE["ts"] = 0


async def get_dept_tools(dept_id: str) -> list[str] | None:
    """团队工具黑名单（2026-09-01 白名单→黑名单：勾选=禁用）；None=无禁用（全部可用），[]=无禁用。
    key=dept_block.{dept_id}（换新 key：存量 dept_tools.* 白名单作废=默认全开，管理员重新勾选禁用）。"""
    if not dept_id:
        return None
    kv = await _load_all()
    raw = kv.get(f"dept_block.{dept_id}")
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        return [str(x) for x in data] if isinstance(data, list) else None
    except json.JSONDecodeError:
        return None


async def get_user_tools(user_id: int) -> list[str] | None:
    """用户级工具黑名单（key=user_block.{user_id}，2026-09-01 白名单→黑名单：勾选=禁用）。

    语义：勾选=禁用；与团队黑名单取并集（任一禁用即禁用）；None=未配置（无禁用）。
    换新 key：存量 user_tools.* 白名单作废=无禁用。消费侧 skill_router：allowed_tools - user_blocked（含 run_script 可禁）。
    """
    if not user_id:
        return None
    kv = await _load_all()
    raw = kv.get(f"user_block.{user_id}")
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        return [str(x) for x in data] if isinstance(data, list) else None
    except json.JSONDecodeError:
        return None


async def get_active_mcp_tool_ids() -> set[str]:
    """运维启用的 MCP 外部工具（mcp_tools.status='active'，60s 缓存）。

    消费侧 skill_router：group=mcp 的工具仅在该集合内注入（admin 停用即全端下线）。

    **独立时间戳 `mcp_ts`**（2026-09-10 走查 bug）：原实现与 `_load_all` 的 kv 缓存**共用
    `_CACHE["ts"]`**，而 skill_router 的顺序是"先本函数、后 get_user_mcp_enabled"——
    本函数刷新 ts 却不填 kv，于是紧接着的 `_load_all()` 判定"缓存新鲜"直接返回**空的 kv**，
    `user_mcp.{uid}` 永远读不到 → 员工个人启用的外部工具**永不注入**；
    同一把 ts 还会让 kv 在稳态下**再也不刷新**（model_layer/dept_tools/custom_allow/
    dept_tools/custom_allow 全部读到空配置＝回落默认值）。危害远超"某个工具看不见"。
    """
    if time.time() - _CACHE["mcp_ts"] < _CACHE_TTL and "mcp_active" in _CACHE:
        return _CACHE["mcp_active"]
    engine = get_global_engine()
    async with engine.connect() as conn:
        rows = (await conn.execute(text(
            "SELECT id FROM mcp_tools WHERE status='active'"))).all()
    out = {r[0] for r in rows}
    _CACHE["mcp_active"] = out
    _CACHE["mcp_ts"] = time.time()
    return out


def invalidate_config_cache() -> None:
    """system_config 写入后立即失效（供 API 层直写者调用）。

    2026-09-10 同类排查：`_load_all` 的 60s TTL 是兜底，不是免失效理由——API 层直接
    `INSERT INTO system_config` 的写点（admin 改团队/员工工具黑名单、`PUT /config/{key}`）
    原先一处失效都不打，运维改完配置后最长 60s 内 agent 仍按旧配置跑（model_layer 改模型、
    沙盒联网开关、工具黑名单都踩得到）。
    """
    _CACHE["ts"] = 0.0


def invalidate_mcp_cache() -> None:
    """外部工具启停立即生效（原只等 60s TTL 自然过期）。

    2026-09-10 走查同类排查：写路径的失效点要**成对覆盖**——`user_mcp.*` 的写入方
    （set_user_mcp_enabled）会 `_CACHE["ts"]=0`，但 `mcp_tools.status` 的写入方
    （admin 启停/删除）一处失效都没做，运维点完开关最多 60s 才生效，与"我开了他却看不见"
    的观感同源。
    """
    _CACHE["mcp_ts"] = 0.0


async def get_user_mcp_enabled(user_id: int) -> list[str] | None:
    """员工个人启用的 MCP 外部工具（key=user_mcp.{user_id}，2026-09-03，运维全局闸门之下）。

    语义：显式启用制——None/缺省=全禁（外部工具需员工在「AI外部工具」页自行勾选启用）；
    消费侧 skill_router：mcp 组工具仅放行此处启用项（与运维 active 闸门交集生效）。
    """
    if not user_id:
        return None
    kv = await _load_all()
    raw = kv.get(f"user_mcp.{user_id}")
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        return [str(x) for x in data] if isinstance(data, list) else None
    except json.JSONDecodeError:
        return None


async def set_user_mcp_enabled(user_id: int, tool_ids: list[str]) -> None:
    """保存员工 MCP 外部工具启用集（空数组=全禁）。"""
    engine = get_global_engine()
    key = f"user_mcp.{user_id}"
    async with engine.begin() as conn:
        if tool_ids:
            await conn.execute(text(
                "INSERT INTO system_config (key, value) VALUES (:k, :v) "
                "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value"),
                {"k": key, "v": json.dumps([str(x) for x in tool_ids])})
        else:
            await conn.execute(text("DELETE FROM system_config WHERE key=:k"), {"k": key})
    _CACHE["ts"] = 0


async def get_dept_global_skills(dept_id: str) -> list[int] | None:
    """团队对全局技能（默认AI技能）的白名单（key=dept_global_skills.{dept_id}，2026-08-21）。

    语义：None=全部允许；[]=全禁（团队管理员整组关闭）；与运维全局启停、员工个人启停取交集（最严生效）。
    消费侧 skill_file_service.list_active_global_skills / 团队开关 API。
    """
    if not dept_id:
        return None
    kv = await _load_all()
    raw = kv.get(f"dept_global_skills.{dept_id}")
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        return [int(x) for x in data] if isinstance(data, list) else None
    except (json.JSONDecodeError, ValueError):
        return None


async def set_dept_global_skills(dept_id: str, skill_ids: list[int] | None) -> None:
    """设置团队对全局技能的白名单；None=删 key（全部允许），[]=全禁，非空=仅允许清单内。"""
    if not dept_id:
        return
    engine = get_global_engine()
    key = f"dept_global_skills.{dept_id}"
    if skill_ids is None:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM system_config WHERE key=:k"), {"k": key})
        _CACHE["ts"] = 0
        return
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO system_config (key, value, description, updated_at) "
                 "VALUES (:k, :v, :d, NOW()) "
                 "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()"),
            {"k": key, "v": json.dumps([int(x) for x in skill_ids]), "d": "团队全局技能白名单（默认AI技能）"},
        )
    _CACHE["ts"] = 0
