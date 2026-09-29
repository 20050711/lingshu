"""DeepSeek LLM 客户端（思考模式 + Tool Calls）。

关键协议约束（docs/官方接口规范/deepseek接口规范/思考模式.md）：
1. 思考模式经 extra_body={"thinking":{"type":"enabled"}} 开启；思考强度 reasoning_effort
2. 思考模式下不支持 temperature/top_p 等采样参数（传了也会被忽略）
3. **有工具调用轮次的 assistant 消息，后续所有请求必须回传 reasoning_content，否则 400**
4. 思维链经流式 chunk.choices[0].delta.reasoning_content 返回

因此：messages 全用原生 dict，assistant 消息完整保留 reasoning_content 字段。
"""
from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

from openai import AsyncOpenAI

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger("agent.llm")
_settings = get_settings()

StreamCb = Callable[[str], Awaitable[None]]

# GLM 文本模型严格 1 并发串行锁（2026-08-24）：GLM-4-Flash-250414 官方并发 2，
# 用户要求串行——所有 glm 辅助调用（create_text_client / DeepSeekLLM 两个入口）
# 经此锁排队。进程内锁即可（同各服务的串行锁做法
# 单进程假设）；多 worker 部署时需换 agent/queue/queue_manager.py 的 Redis 租约模式。
_glm_serial_lock = asyncio.Lock()


def _resolve_thinking(thinking, effort):
    """思考档位字符串归一化（2026-09-02 修复：工具页下拉传 off/low/high/max 字符串，
    build_thinking_kwargs 用 is True/is False 严格判定 → 字符串全 truthy："off" 反转为开启、
    档位不映射 effort）。

    - "off" → (False, effort) 关闭思考
    - "low"/"high"/"max" → (True, 档位) 开启且 effort=档位
    - ""/未知字符串 → (None, effort) 平台默认
    - bool/None 原样
    """
    if isinstance(thinking, str):
        t = thinking.strip().lower()
        if t == "off":
            return False, effort
        if t in ("low", "high", "max"):
            return True, t
        return None, effort
    return thinking, effort


def build_thinking_kwargs(platform: str, thinking: bool | None, effort: str | None) -> dict:
    """思考参数构造（2026-08-12：从 DeepSeekLLM._ainvoke_once 抽出共享，供原始 AsyncOpenAI 消费点复用）。

    与 DeepSeekLLM 同一协议（docs/官方接口规范/deepseek接口规范/思考模式.md）：
    - deepseek：thinking=False 不发任何思考参数（官方文档：非思考模式支持 Tool Calls）；
      None/True 发 thinking.enabled + reasoning_effort（effort 有值时）
    - agnes：仅 thinking=True 发 chat_template_kwargs.enable_thinking（None 按平台默认=非思考）
    - glm（2026-09-01）：glm-4.7 支持思考开关（官方 ChatThinking；实测思考开提示词遵循更好——
      纯净 JSON vs markdown 包装）；False 显式传 disabled（4.7 可关），None/True 传 enabled
    """
    logger.debug("thinking 参数 platform=%s thinking=%s effort=%s", platform, thinking, effort)
    if platform == "agnes":
        if thinking is True:
            return {"extra_body": {"chat_template_kwargs": {"enable_thinking": True}}}
        return {}
    if platform == "glm":
        if thinking is False:
            return {"extra_body": {"thinking": {"type": "disabled"}}}
        kwargs: dict = {"extra_body": {"thinking": {"type": "enabled"}}}
        if effort:
            kwargs["reasoning_effort"] = effort
        return kwargs
    if thinking is False:
        return {}
    kwargs: dict = {"extra_body": {"thinking": {"type": "enabled"}}}
    if effort:
        kwargs["reasoning_effort"] = effort
    return kwargs


def _normalize_usage(u) -> dict:
    """平台无关的 usage 归一（2026-09-11 走查：agnes/GLM 会话输入 token 恒 0，看着像计费坏了）。

    DeepSeek 返回 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`（命中/未命中拆分）；
    OpenAI 兼容系（agnes / GLM）只给 `prompt_tokens`，缓存命中在 `prompt_tokens_details.cached_tokens`。
    原实现直接取 DeepSeek 专有字段 → 非 DeepSeek 平台命中/未命中恒 0、**输入 token 整段丢失**
    （实测：agnes 会话 10 轮只记到 completion 2212，输入 0/0）。这里统一补齐，两种结构都吃。
    """
    def g(name: str):
        v = getattr(u, name, None)
        if v is None and isinstance(u, dict):
            v = u.get(name)
        return v

    prompt = int(g("prompt_tokens") or 0)
    hit = g("prompt_cache_hit_tokens")
    if hit is None:
        det = g("prompt_tokens_details")
        hit = det.get("cached_tokens") if isinstance(det, dict) else getattr(det, "cached_tokens", None)
    miss = g("prompt_cache_miss_tokens")
    if miss is None:
        miss = max(0, prompt - int(hit or 0))
    return {
        "prompt_tokens": prompt,
        "prompt_cache_hit_tokens": int(hit or 0),
        "prompt_cache_miss_tokens": int(miss or 0),
        "completion_tokens": int(g("completion_tokens") or 0),
    }


def json_mode_kwargs(cfg: dict | None) -> dict:
    """期望 JSON 输出的调用点：GLM 平台加 response_format={"type":"json_object"}（2026-09-11 走查问题10）。

    实测（60 条评论长输出 ~5k 字符）：glm-4-flash-250414 不带参 → ```json 包装（json.loads 失败，
    靠 parse_llm_json 兜底）；带参 → 纯 JSON 直接可解析。glm-4.7-flash（思考开）不带参时
    8000 token 全被思考烧完、正文为空；带参 → 纯 JSON 正常。用户决策：只推广到 GLM
    （deepseek/agnes 保持原样，避免动主链协议）。
    """
    platform = str((cfg or {}).get("platform") or "deepseek")
    return {"response_format": {"type": "json_object"}} if platform == "glm" else {}


class _MockAuxCompletions:
    """llm_mock 下 create_text_client 的假 completions（辅助调用路径零费用、零配额）。

    2026-08-19（回归零 LLM 化）：kb 摘要/关键词提取/精排、简历解析等
    llm_aux 调用点（client.chat.completions.create 模式）统一走此假客户端——
    返回固定"理想回复"，断言只验流程不验内容。
    """

    def __init__(self, delay_s: float):
        self._delay = delay_s

    async def create(self, **kwargs) -> object:
        if self._delay > 0:
            await asyncio.sleep(self._delay)
        from types import SimpleNamespace

        # 2026-09-02：mock 内容加长——会议总结等断言 summary>50 的用例在 mock 档
        # 曾因 10 字符固定回复失败（每次免费档回归才过）；前缀保留兼容既有断言
        msg = SimpleNamespace(
            content="（模拟辅助调用回复）会议讨论围绕既定议题展开，明确了各方分工与时间节点，"
                    "对风险事项制定了应对预案，并安排了下一步的行动计划与跟进责任人。",
            reasoning_content="（模拟）", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=None)


class _MockAuxChat:
    def __init__(self, delay_s: float):
        self.completions = _MockAuxCompletions(delay_s)


class _MockAuxClient:
    """create_text_client 的 mock 返回（与 AsyncOpenAI 的 chat.completions.create 同形）。"""

    def __init__(self, delay_s: float):
        self.chat = _MockAuxChat(delay_s)


class _SerialGLMCompletions:
    """GLM 文本模型 completions 串行包装：每次 create 先拿 _glm_serial_lock（严格 1 并发）。

    限流退避（2026-09-01）：429 / 1302 / 1305（glm-4.7-flash 实测「访问量过大」间歇性限流）
    重试 8 次、间隔 2/4/6/8/10/12/14/16s（用户要求 8 次；免费档限流须重试队列化）；其余异常直接抛（由调用方重试）。"""
    _LIMIT_STATUS = {429, 1302, 1305}

    def __init__(self, raw: AsyncOpenAI):
        self._raw = raw

    async def create(self, **kwargs) -> object:
        async with _glm_serial_lock:
            for attempt in range(9):
                try:
                    return await self._raw.chat.completions.create(**kwargs)
                except Exception as e:
                    status = getattr(getattr(e, "response", None), "status_code", None)
                    if status in self._LIMIT_STATUS and attempt < 8:
                        logger.warning("GLM 限流 status=%s 退避 %.0fs（attempt %d）", status, 2 * (attempt + 1), attempt + 1)
                        await asyncio.sleep(2 * (attempt + 1))
                        continue
                    raise


class _SerialGLMChat:
    def __init__(self, raw: AsyncOpenAI):
        self.completions = _SerialGLMCompletions(raw)


class _SerialGLMClient:
    """create_text_client 的 glm 平台返回（与 AsyncOpenAI 的 chat.completions.create 同形，内部串行）。"""

    def __init__(self, raw: AsyncOpenAI):
        self.chat = _SerialGLMChat(raw)


def create_text_client(cfg: dict | None) -> tuple:
    """平台感知文本 client（问题 11 修复，2026-08-17）：按 cfg.platform 分流 deepseek/agnes。

    历史缺陷模式：调用点 client 写死 deepseek + model 直接用 cfg（用户选了 GLM 视觉模型/
    测试期 llm_aux 切 agnes → glm/agnes 模型发 deepseek API → 400）。
    glm 或未知平台（非文本模型）回退 deepseek-flash + 调用方日志负责提示。
    返回 (client, model, thinking_kwargs)。
    llm_mock=True（2026-08-19）：返回假客户端（固定理想回复）——零费用覆盖全部
    llm_aux 调用点（kb/简历/历史摘要）。
    """
    if _settings.llm_mock:
        return _MockAuxClient(_settings.llm_mock_delay_s), "mock-aux", {}
    from openai import AsyncOpenAI

    # 2026-08-21（tools_smoke 卡死根因）：统一超时——AsyncOpenAI 默认 600s，
    # kb 精排/记忆提取等辅助调用点曾无 timeout 传入，LLM API 挂起时单次等 10 分钟
    # （检索类工具两次调用=20 分钟空转，实测吻合）；与主链 llm_call_timeout_seconds 对齐
    timeout = _settings.llm_call_timeout_seconds
    platform = str(cfg.get("platform") or "deepseek") if cfg else "deepseek"
    effort = str(cfg.get("effort") or "high") if cfg else None
    thinking = cfg.get("thinking") if cfg else None
    # 2026-09-02：字符串档位（off/low/high/max）归一化——工具页思考下拉直传字符串的路径
    thinking, effort = _resolve_thinking(thinking, effort)
    if platform == "agnes":
        client = AsyncOpenAI(api_key=_settings.agnes_api_key, base_url=_settings.agnes_base_url, timeout=timeout)
        model = str(cfg["model"]) if cfg else _settings.agnes_fallback_model
    elif platform == "glm":
        # 2026-09-01（用户决策）：glm-4.7-flash（4-flash 已停用）默认开启思考——实测思考开提示词遵循
        # 更好（纯净 JSON vs markdown 包装），且 4.7 平台默认即思考；显式选择关闭（off）尊重（4.7 可关）。
        # 官方并发 2、用户要求严格 1 并发 → _SerialGLMClient 串行包装。
        raw = AsyncOpenAI(api_key=_settings.zhipu_api_key, base_url=_settings.zhipu_base_url, timeout=timeout)
        client = _SerialGLMClient(raw)
        model = str(cfg["model"]) if cfg else _settings.glm_fallback_model
        if thinking is not False:
            thinking = True  # 默认强制开；显式关闭尊重
    elif platform == "deepseek":
        client = AsyncOpenAI(api_key=_settings.deepseek_api_key, base_url=_settings.deepseek_base_url, timeout=timeout)
        model = str(cfg["model"]) if cfg else _settings.deepseek_model_employee
    else:
        client = AsyncOpenAI(api_key=_settings.deepseek_api_key, base_url=_settings.deepseek_base_url, timeout=timeout)
        model = _settings.deepseek_model_employee
        thinking = None
    return client, model, build_thinking_kwargs(platform, thinking, effort)


def _strip_reasoning(messages: list[dict], keep_last: int) -> list[dict]:
    """支柱 5（2026-08-10 实测验证）：回传 LLM 前丢弃历史轮次 reasoning_content。

    背景：DeepSeek 官方文档声称"有工具调用轮次的 reasoning_content 必须完整回传否则 400"，
    但 2026-08-10 真实 API 实测（deepseek-v4-flash + thinking enabled，多轮工具链/跨 ask/
    截断/全丢弃 4 场景）**全部接受不回传**——文档为保守表述。21 轮事故中 reasoning_content
    累计 104K 占满上下文预算，实际可消除 ~98%。

    策略：仅保留最近 keep_last 条 assistant 消息的 reasoning_content（保持本轮上下文连续），
    更早轮次置 None（字段保留、值清空——语义等价"未提供"，且不破坏消息结构）。
    """
    if not _settings.strip_reasoning_history:
        return messages
    kept = 0
    out = list(messages)
    for i in range(len(out) - 1, -1, -1):
        m = out[i]
        if isinstance(m, dict) and m.get("role") == "assistant" and m.get("reasoning_content"):
            if kept < keep_last:
                kept += 1
            else:
                out[i] = {**m, "reasoning_content": None}
    return out


def get_model_config(role: str) -> tuple[str, str]:
    """按角色返回 (model, reasoning_effort)（env 兜底；团队级分层经 DeepSeekLLM.create）。"""
    if role == "ceo":
        return _settings.deepseek_model_ceo, _settings.reasoning_effort_ceo
    return _settings.deepseek_model_employee, _settings.reasoning_effort_employee


class DeepSeekLLM:
    def __init__(self, role: str = "employee", model: str | None = None, effort: str | None = None,
                 platform: str = "deepseek", thinking: bool | None = None):
        """model/effort 缺省时按 role 走 env 兜底（三期 M13 团队分层经 classmethod create）。
        platform（2026-08-10 Agnes 接入）：deepseek / agnes——按平台选 base_url/api_key。
        thinking（2026-08-11 思考开关）：None=平台默认（deepseek 开思考 / agnes 非思考）；
        显式 True/False 由 model_layer 配置（配置页「思考模式」开关）覆盖。"""
        resolved_model, resolved_effort = get_model_config(role)
        self.model = model or resolved_model
        # 2026-09-02：字符串档位（off/low/high/max）归一化后再用（修复 "off" 字符串 truthy 反转）
        thinking, effort = _resolve_thinking(thinking, effort)
        self.reasoning_effort = effort or resolved_effort
        self.platform = platform
        # 平台→客户端：agnes 走独立 base_url/key；glm 走智谱端点（2026-08-24）；其余回退 deepseek
        if platform == "agnes":
            base_url, api_key = _settings.agnes_base_url, _settings.agnes_api_key
            default_thinking = False  # agnes 默认非思考（OpenAI 格式开启思考需 chat_template_kwargs.enable_thinking）
        elif platform == "glm":
            # 2026-09-02：与 create_text_client 策略统一——glm-4.7 默认思考开（实测遵循更好）；
            # 显式选择关闭（off）尊重（4.7 可关）
            base_url, api_key = _settings.zhipu_base_url, _settings.zhipu_api_key
            default_thinking = True
        else:
            base_url, api_key = _settings.deepseek_base_url, _settings.deepseek_api_key
            default_thinking = True
        self.thinking = default_thinking if thinking is None else thinking
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=180.0,  # 单轮调用超时，防止挂死
            max_retries=0,  # 重试由 ainvoke 显式控制
        )

    @classmethod
    async def create(
        cls,
        role: str = "employee",
        dept_id: str | None = None,
        model_override: str | None = None,
        platform_override: str | None = None,
    ) -> "DeepSeekLLM":
        """按团队/角色解析模型分层（system_config → env 兜底）后实例化（含平台分派）。
        2026-08-20：model_override/platform_override——QA 页主对话模型切换
        （aux_overrides._main）覆盖配置档（其余字段仍取配置）。"""
        from app.services.config_service import get_model_config

        cfg = await get_model_config(dept_id, role, "llm")
        model = str(model_override) if model_override else (str(cfg.get("model")) if cfg else "")
        platform = (str(platform_override) if platform_override
                    else (str(cfg.get("platform") or "deepseek") if cfg else "deepseek"))
        if model:
            return cls(role, model=model,
                       effort=str(cfg.get("effort") or "high") if cfg else "high",
                       platform=platform,
                       thinking=cfg.get("thinking") if cfg else None)  # None=平台默认
        return cls(role)

    async def ainvoke(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        stream_cb: StreamCb | None = None,
    ) -> dict:
        """调用模型，返回 assistant 消息 dict（含 reasoning_content / tool_calls）。

        stream_cb 每收到一个文本 delta 调用一次（前端 SSE text 事件）。
        失败重试 1 次（指数退避 1s）。

        总时长上限：httpx timeout 是"空闲超时"（无数据才触发），DeepSeek 思考模式
        持续流式输出 reasoning 时永不触发 → 复杂多轮任务可能无限挂起。
        故用 asyncio.wait_for 加总时长硬上限（超时快速失败，不让用户无限等待）。
        """
        tool_names = [t["function"]["name"] for t in tools] if tools else None
        # 支柱 5（2026-08-10 实测验证）：丢弃历史轮次 reasoning_content（仅保留最近 keep_last 条）
        messages = _strip_reasoning(messages, _settings.reasoning_keep_last)
        logger.info(
            "LLM 调用开始 model=%s effort=%s thinking=%s tools=%s msgs=%d",
            self.model, self.reasoning_effort, self.thinking, tool_names, len(messages),
        )
        if _settings.llm_mock:
            return await self._mock_ainvoke(messages, tools, stream_cb)
        t0 = time.time()
        last_exc: Exception | None = None
        # GLM 严格 1 并发（2026-08-24）：glm 平台经 _ainvoke_serial 包 _glm_serial_lock
        call = self._ainvoke_serial if self.platform == "glm" else self._ainvoke_once
        # 2026-09-01：GLM 免费档限流重试——429/1302/1305 重试 8 次（退避递增 2/4/6/8/10/12/14/16s，
        # 用户要求 8 次；1305 高峰时段 5 次仍会耗尽），其余错误与非 GLM 平台保持原 1 次重试（sleep 1s）
        glm_limit = {429, 1302, 1305}
        max_retries = 8 if self.platform == "glm" else 1
        for attempt in range(max_retries + 1):
            try:
                msg = await asyncio.wait_for(
                    call(messages, tools, stream_cb),
                    timeout=_settings.llm_call_timeout_seconds,
                )
                calls = [c["function"]["name"] for c in (msg.get("tool_calls") or [])]
                logger.info(
                    "LLM 调用完成 耗时=%.1fs tool_calls=%s content_len=%d reasoning_len=%d",
                    time.time() - t0, calls, len(msg.get("content") or ""), len(msg.get("reasoning_content") or ""),
                )
                return msg
            except Exception as e:
                last_exc = e
                status = getattr(getattr(e, "response", None), "status_code", None)
                is_glm_limit = self.platform == "glm" and status in glm_limit
                logger.warning("LLM 调用失败 attempt=%d err=%s", attempt + 1, str(e)[:300])
                if attempt < max_retries and (is_glm_limit or attempt == 0):
                    await asyncio.sleep(2 * (attempt + 1) if is_glm_limit else 1)
        raise RuntimeError(f"LLM 调用失败: {last_exc}")

    async def _mock_ainvoke(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        stream_cb: StreamCb | None,
    ) -> dict:
        """模拟 LLM（llm_mock=True）：确定性响应 + 模拟延迟，零费用、零 API 配额。

        2026-08-17：高并发压测（concurrency/mock_load_test.py，50 并发）压测平台完整管线
        （队列/图路由/DB/SSE/落库）而不依赖真实 LLM 的并发能力。
        行为：历史无工具结果 → 返回固定 tool_calls（首选 file_search 压检索链路）；
        已有工具结果 → 返回固定最终回答（流程收尾）。
        """
        if _settings.llm_mock_delay_s > 0:
            await asyncio.sleep(_settings.llm_mock_delay_s)
        # 确定性序列（按最近一条 assistant 消息的 tool_calls 名判定阶段，避免按 tool 消息
        # 计数偏移——intent_event 等特判工具也回填 tool 消息）：
        #   阶段 1（无工具结果）：intent_event + file_search（覆盖意图广播 + 检索链路）
        #   阶段 2（最近工具含 file_search 结果）：result_event（覆盖结果广播）
        #   阶段 3+（最近工具含 result_event）：固定最终回答（收尾 done）
        # 2026-09-17：原首选工具 sql_query 随数据查询线下线，改用恒注入的 file_search
        last_tool_names: list[str] = []
        for m in reversed(messages):
            if m.get("role") == "assistant" and m.get("tool_calls"):
                last_tool_names = [tc["function"]["name"] for tc in m["tool_calls"]]
                break
        has_fs = bool(tools) and any(t["function"]["name"] == "file_search" for t in tools)
        if not last_tool_names and tools and has_fs:
            # 阶段 1（file_search 可用）：intent_event + file_search（压意图广播 + 检索链路）
            calls = [
                {"id": "mock_tc_intent", "type": "function",
                 "function": {"name": "intent_event", "arguments": '{"text": "（模拟）检索相关资料", "scope": "task"}'}},
                {"id": "mock_tc_1", "type": "function",
                 "function": {"name": "file_search", "arguments": '{"query": "（模拟）检索关键词"}'}},
            ]
            tool_calls, content = calls, ""
        elif not last_tool_names:
            # 无工具可用：理想回复 = 直接回答（不触发工具链/反问卡——快速轮直接 done 收尾）
            tool_calls, content = None, "（模拟回答）任务已完成。"
        elif "result_event" in last_tool_names:
            tool_calls, content = None, "（模拟回答）任务已完成，结果如上所示。"
        else:
            tool_calls = [{"id": "mock_tc_result", "type": "function",
                           "function": {"name": "result_event", "arguments": '{"text": "（模拟）已完成查询", "ok": true}'}}]
            content = ""
        if stream_cb and content:
            # 2026-08-19（mock 流式分段）：4 段 + 段间隔 = llm_mock_delay_s——模拟真实
            # LLM 流式时长（e2e_reconnect 断连窗口依赖任务持续 ~3-4s；秒级完成的
            # mock 会令断连发生在任务结束后，重连测试失义）
            seg = max(1, len(content) // 4)
            for i in range(0, len(content), seg):
                await stream_cb(content[i:i + seg])
                if i + seg < len(content):
                    await asyncio.sleep(_settings.llm_mock_delay_s)
        logger.info("LLM mock 响应 last_tools=%s tool_calls=%s content_len=%d",
                    last_tool_names, [c["function"]["name"] for c in (tool_calls or [])], len(content))
        return {"role": "assistant", "content": content, "reasoning_content": "（模拟思考）",
                "tool_calls": tool_calls, "usage": None}  # mock 无真实计费（计费框显示未知）

    async def _ainvoke_serial(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        stream_cb: StreamCb | None,
    ) -> dict:
        """GLM 平台调用串行包装（严格 1 并发，与 _SerialGLMCompletions 共用 _glm_serial_lock）。"""
        async with _glm_serial_lock:
            return await self._ainvoke_once(messages, tools, stream_cb)

    async def _ainvoke_once(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        stream_cb: StreamCb | None,
    ) -> dict:
        # 思考模式参数按平台与开关发送（2026-08-11 思考开关配置化；2026-08-12 抽共享函数 build_thinking_kwargs）
        kwargs: dict = {"stream_options": {"include_usage": True}}  # 四期缓存优化：流式返回 usage 观测缓存命中
        kwargs.update(build_thinking_kwargs(self.platform, self.thinking, self.reasoning_effort))
        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
            stream=True,
            **kwargs,
        )
        content, reasoning = "", ""
        tool_calls_acc: dict[int, dict] = {}
        usage: dict | None = None

        async for chunk in resp:
            # 流式最后 chunk 携带 usage（include_usage）：缓存命中观测
            if getattr(chunk, "usage", None):
                usage = _normalize_usage(chunk.usage)
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            # reasoning_content 是 DeepSeek 扩展字段，openai SDK 模型未声明，用 getattr 兼容
            reasoning_delta = getattr(delta, "reasoning_content", None)
            if reasoning_delta:
                reasoning += reasoning_delta
            if delta.content:
                content += delta.content
                if stream_cb:
                    await stream_cb(delta.content)
            if delta.tool_calls:
                # 增量聚合：以 index 为槽位，逐片拼接 arguments
                for tc in delta.tool_calls:
                    slot = tool_calls_acc.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function and tc.function.name:
                        slot["name"] = tc.function.name
                    if tc.function and tc.function.arguments:
                        slot["arguments"] += tc.function.arguments

        tool_calls = [
            {
                "id": slot["id"],
                "type": "function",
                "function": {"name": slot["name"], "arguments": slot["arguments"]},
            }
            for _, slot in sorted(tool_calls_acc.items())
        ] or None

        # 缓存命中观测（四期）：hit/miss 落日志，便于评估优化效果
        if usage and (usage.get("prompt_cache_hit_tokens") is not None):
            hit = usage.get("prompt_cache_hit_tokens") or 0
            miss = usage.get("prompt_cache_miss_tokens") or 0
            total = hit + miss
            rate = f"{hit / total * 100:.0f}%" if total else "-"
            logger.info("缓存命中 hit=%d miss=%d 命中率=%s prompt_total=%d", hit, miss, rate, total)

        return {
            "role": "assistant",
            "content": content,
            "reasoning_content": reasoning,
            "tool_calls": tool_calls,
            # 需求 3（2026-08-17）：usage 透出供 token 计费累计（usage 缺失时 None）
            "usage": usage,
        }
