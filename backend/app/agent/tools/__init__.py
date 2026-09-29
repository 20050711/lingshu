"""工具注册表：Tool 是代码层（Agent 实际执行的函数）。

每个工具注册：{name, description, parameters(JSON Schema), queue, handler}
handler 签名: async (args: dict, ctx: ToolContext) -> dict | str

ToolContext 携带会话/角色/目录等运行期信息（不入 LangGraph state）。
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from app.core.config import get_settings

_settings = get_settings()


@dataclass
class ToolContext:
    session_id: str
    round_id: int
    department_id: str
    user_role: str
    client_id: str
    output_dir: str          # /data/outputs/{session_id}/{round_id}/
    user_id: int = 0         # 用户 ID（个人记忆等按用户隔离的工具使用；0 兜底不崩）
    # 同一 ask 轮内的图表注册表（chart_id → {option, session_id}）：
    # DB 落库在轮末 persist_round，doc_export 同轮内嵌图表时先查内存再查 DB
    chart_registry: dict[str, dict] | None = None
    # 支柱 1（2026-08-10）：子代理执行体通道——主循环 run_agent 补传 events/llm；
    # 只读工具桥路径（只放行 write=False 工具）天然为 None 不误触
    events: asyncio.Queue | None = None     # 主事件队列（子代理内部事件透传；None=无事件通道）
    llm: object | None = None               # 子代理循环 LLM（主 LLM 同档实例）
    subagent_id: str | None = None          # "s-1"/"s-2"（事件标识 + run_script 子目录 + 交付禁用）
    work_subdir: str | None = None          # run_script work 子目录（子代理隔离写域）
    subagent_counter: int = 0               # 主 ctx 上自增（tool_exec 顺序执行无并发）
    # 2026-09-10 知识库原文件开放（用户决策：统一读取路径，kb_read 下线）：
    # `kb/{global|dept|u{uid}}/` 三段布局按三态映射成前缀白名单
    kb_roots: list[str] | None = None
    # 用户级辅助模型覆盖（2026-08-14，AI 技能管理页配置，users.aux_model_overrides）：
    # {image_recognition/image_generation/memory/video_generate: {platform, model}}
    aux_overrides: dict | None = None


ToolHandler = Callable[[dict, ToolContext], Awaitable[dict | str]]

# 图标名表（2026-09-18）：全站图标统一 Phosphor Light，前端按**语义名**查表映射成组件。
#
# 为什么不是 emoji：此前各处（本文件默认值 + 28 个工具注册 + MCP_DEFAULTS）直接写 emoji。
# 那是把"表现"塞进了后端数据——跨平台字形不一致、无法跟随文字色、读屏会把"拼图块"按字面念出来，
# 而且前端只能靠一张 emoji→组件的对照表兜底（后端换个 emoji 就又漏到界面上）。
# 改成语义名后，**后端只声明"这是什么类别的工具"，画成什么图标由前端决定**。
#
# 消费方（改动本表时同步）：
#   - 前端 `frontend/src/components/Icon.tsx` 的 KEY_ICONS（名字 → Phosphor 组件，未收录落默认）
#   - 运维后台 MCP 图标下拉（app/api/mcp.py 的 /mcp/icon-keys 下发）
ICON_KEYS: tuple[str, ...] = (
    "tool",      # 默认：通用工具
    "chart", "video", "audio", "image", "film", "screen", "code", "brain",
    "file", "note", "book", "book-open", "folder", "database", "package", "compress",
    "search", "link", "download", "globe", "mail", "calendar", "building", "chat",
    "check", "question", "refresh", "palette",
)
DEFAULT_ICON = "tool"

# 并发队列的中文名（2026-09-24 用户要求：工具调用**过程**不许出现英语）。
# tool_exec 的"队列 X，立即执行"用它渲染；**新增 queue 取值时在这里补一行**
# （守卫在 tests/skill_smoke.py：未登记的队列名会让冒烟失败）。
QUEUE_LABELS: dict[str, str] = {
    "default": "通用", "read": "只读", "report": "产出", "sandbox": "沙盒",
    "subagent": "子代理", "transcribe": "转写", "video": "视频", "video_gen": "视频",
    "vision": "视觉", "image_gen": "图片",
}


def queue_label(queue: str) -> str:
    """队列中文名；未登记的一律回落"通用"（宁可笼统也不让英文漏到界面上）。"""
    return QUEUE_LABELS.get(queue or "", "通用")


def tool_label(name: str) -> str:
    """工具的中文名：优先 display_name，未注册的工具回落原 id。

    时间线/事件里带这个字段，前端就不必靠 /skills 反查（内部工具不在那份清单里，
    早先会退化成显示英文 id——2026-09-24 走查实报）。
    """
    t = _REGISTRY.get(name)
    return (t.display_name if t else None) or name


class ToolSpec:
    def __init__(self, name: str, description: str, parameters: dict, queue: str, handler: ToolHandler,
                 write: bool = False, display_name: str | None = None, icon: str = DEFAULT_ICON,
                 status: str = "active", sort_order: int = 0, group: str = "other",
                 summary: str | None = None, user_description: str | None = None,
                 select_mode: str = "multi", progress_keys: tuple[str, ...] | None = None,
                 hidden: bool = False):
        """write=False 为只读工具（默认，可直接执行）。
        write=True 为产出/写工具（v2 框架：工具由 agent 直接执行，无授权卡；
        write 标记用于复杂任务「计划批准前写门禁」拒绝，见 tool_exec）。

        四期重构元数据（替代原技能 yaml）：display_name（功能栏中文名）/ icon /
        status（active/planning）/ sort_order（展示排序）/ group（功能栏分组）。
        summary：给人看的一句话说明（功能页/浮窗展示）；description 是给 Agent 的详细说明
        （4.1 起四段结构 What/When/How/Result）；user_description：给人看的完整介绍（技能页弹窗展示，
        4.1 与 description 分离——description 含内部指令，不再直接展示给用户）。
        select_mode（4.1 补充）：multi=功能栏多选（auto 自动选择包含）；single=功能栏单选
        （skill 类，auto 模式不自动启用，须用户明确单选）。
        hidden（2026-09-16）：不在「AI技能」功能栏/浮窗展示——工具由别的入口管（如 MCP 外部工具
        子工具只在「AI 外部工具」浮窗里作为一个条目启用）。**只影响展示，不影响注入与闸门**。
        progress_keys（2026-09-08 护栏重构）：工具结果 dict 中命中任一键即视为"有信息量的进展"
        （无进展计数的正判据）。None=从宽默认（成功结果一律算进展，防误伤）；空元组=永不判进展。
        """
        self.name = name
        self.description = description
        self.parameters = parameters
        self.queue = queue
        self.handler = handler
        self.write = write
        self.display_name = display_name or name
        self.icon = icon
        self.status = status
        self.sort_order = sort_order
        self.group = group
        self.summary = summary or display_name or name
        self.user_description = user_description or summary or display_name or name
        self.select_mode = select_mode
        self.progress_keys = progress_keys
        self.hidden = hidden

    def to_openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def to_meta(self) -> dict:
        """功能栏/技能页元数据（原技能 yaml 的 id/name/icon/description/status/sort_order 收编于此）。"""
        return {
            "id": self.name,
            "name": self.display_name,
            "icon": self.icon,
            "summary": self.summary,          # 给人看的一句话（功能页/浮窗）
            "description": self.description,  # 给 Agent 的详细说明（4.1 起不进详情浮窗）
            "user_description": self.user_description,  # 给人看的完整介绍（4.1：详情浮窗展示）
            "select_mode": self.select_mode,  # 4.1：multi=多选（auto 包含）/ single=单选（skill 类）
            "status": self.status,
            "sort_order": self.sort_order,
            "group": self.group,
        }


_REGISTRY: dict[str, ToolSpec] = {}


def register_tool(spec: ToolSpec) -> None:
    _REGISTRY[spec.name] = spec


def get_tool(name: str) -> ToolSpec | None:
    return _REGISTRY.get(name)


def get_all_tools() -> list[ToolSpec]:
    return list(_REGISTRY.values())


# 2026-08-17（问题 8）：系统内部工具——恒注入（skill_router always_inject）、默认开启、
# 不出现在前端技能列表/工具白名单（勾选本就是 no-op，展示误导用户）
# 2026-08-21：zip_extract/zip_pack 同列（会话级基础能力，恒注入，无需用户勾选）
# 2026-08-26：skill_read 同列（Q8 技能按需加载的取说明通道，只读）
_INTERNAL_TOOLS = {"intent_event", "result_event", "todo_step", "ask_user",
                   "zip_extract", "zip_pack", "skill_read"}


def get_all_tool_meta(include_hidden: bool = False) -> list[dict]:
    """全部工具元数据（按 sort_order 排序），供 /skills 功能栏展示。

    问题 8：过滤系统内部工具（intent_event/result_event/todo_step/ask_user——恒注入、
    用户不可关，展示在前端技能列表属误导）。

    include_hidden=True（2026-09-17 bug 修复）：**连 hidden 工具一起给**——hidden 只表示
    "不在功能栏展示"，不等于"不能给模型用"。MCP 外部工具靠 /mcp 的勾选启用，
    需要在闸门放行后把描述注入提示词（见 skill_router / build_skills_prompt）。
    """
    return sorted(
        (t.to_meta() for t in _REGISTRY.values()
         if t.name not in _INTERNAL_TOOLS and (include_hidden or not t.hidden)),
        key=lambda m: (m["sort_order"], m["id"]),
    )


def tools_to_schemas(names: list[str]) -> list[dict]:
    return [t.to_openai_schema() for n in names if (t := _REGISTRY.get(n))]


def get_mcp_tool_names() -> set[str]:
    """MCP 外部工具（group='mcp'）名集合——skill_router 按运维 active + 员工个人启用过滤。"""
    return {t.name for t in _REGISTRY.values() if t.group == "mcp"}


def mcp_id_of(tool_name: str, mcp_ids: set[str]) -> str:
    """agent 工具名 → 它归属的 MCP 条目 id（闸门用）。

    一个 MCP 条目可以对应多个 agent 工具：工具名以 `{id}_` 开头即视为该条目的子工具
    （2026-09-16：条目 id → 该条目的多个子工具，如 {id}_chats / {id}_export / {id}_search）。
    同名也算（条目 id 与工具同名）。找不到返回空串（=不属于任何 MCP 条目）。
    """
    if tool_name in mcp_ids:
        return tool_name
    for mid in mcp_ids:
        if tool_name.startswith(f"{mid}_"):
            return mid
    return ""


def get_queue_for(name: str) -> str:
    t = _REGISTRY.get(name)
    return t.queue if t else "default"


def _sandbox_session_root(ctx: ToolContext) -> Path | None:
    """A1（D1）：本会话沙盒根（全 UUID 会话目录，见 script_sandbox._work_dir）。"""

    root = Path(_settings.sandbox_dir) / str(ctx.session_id)
    return root if root.exists() else None


def resolve_output_url(file_path: str, ctx: ToolContext) -> str | None:
    """产出 URL（/api/v1/outputs/{sid}/{round}/{file}）→ 磁盘路径；非 URL 或非本会话返回 None。

    2026-08-20（走查实锤）：doc_export 等工具返回的 file_path 是 output_url 形式（前端
    下载用），LLM 拿它调 read_output/file_parse 直接失败（FileNotFoundError/无权访问）
    ——读取工具需兼容 URL 输入。仅放行本会话（sid 不匹配返回 None，走原路径校验拒绝）。
    """
    from pathlib import Path
    from urllib.parse import unquote

    prefix = f"{_settings.api_prefix.rstrip('/')}/outputs/"
    s = file_path.strip()
    if not s.startswith(prefix):
        return None
    rel = s[len(prefix):]
    parts = rel.split("/")
    if len(parts) < 3:
        return None
    sid, round_no = parts[0], parts[1]
    name = unquote("/".join(parts[2:]))  # 2026-09-15：output_url 已百分号编码（#/?/% 等），此处反解
    if sid != ctx.session_id:
        return None
    return str(Path(_settings.output_dir) / sid / round_no / name)


def validate_readable_path(file_path: str, ctx: ToolContext) -> str | None:
    """工具文件读取越狱防护：仅允许读取当前用户当前会话的上传/产出目录、沙盒工作目录与本团队技能目录内文件。

    返回 None=允许；否则返回拒绝原因（工具返回 error 用）。防 Agent 编造路径读取他人文件/系统文件。
    A1（2026-08-10）：追加本会话沙盒根（work 中间产物可被 file_parse/read_output 按需部分读取）。
    2026-08-20：入口兼容产出 URL 形式（resolve_output_url 转换后校验，读取点须用转换路径）。
    2026-08-20：追加本团队技能目录（{skill_files_dir}/{dept_id}/**，团队隔离；dept 为空不放行）。
    2026-09-10：追加知识库可见根（ctx.kb_roots，三态前缀白名单）——知识库原文件对 agent 开放，
    读取路径统一为 file_parse（kb_read 下线）。路径比较用 Path.resolve() +
    is_relative_to（**组件级**，非字符串前缀），故 kb/u1 不会误放行 kb/u12。
    """
    from pathlib import Path

    resolved = resolve_output_url(file_path, ctx)
    if resolved:
        file_path = resolved
    try:
        p = Path(file_path).resolve()
    except OSError:
        return f"非法路径: {file_path[:80]}"
    upload_root = (Path(_settings.upload_dir) / "users" / str(ctx.user_id) / ctx.session_id).resolve()
    out_root = Path(ctx.output_dir).resolve()
    # ⑥（全局 Explore 排查）：file_parse 放行本会话全部轮次产出根（跨轮读自己产出，
    # 与 read_output 语义一致——避免 LLM 读上轮产出被拒后改走 read_output 重解析放大重复开销）
    session_out_root = (Path(_settings.output_dir) / ctx.session_id).resolve()
    sb_root = _sandbox_session_root(ctx)
    skill_root = (Path(_settings.skill_files_dir) / ctx.department_id).resolve() if ctx.department_id else None
    # 2026-08-21：全局技能目录（{skill_files_dir}/global/**）对所有业务用户可读（注入与 run_script
    # 白名单是主闸；此处放行读取供 file_parse 浏览技能 md/脚本）
    global_skill_root = (Path(_settings.skill_files_dir) / "global").resolve()
    # 2026-09-10 知识库原文件：可见知识库根（global/dept/u{uid} 三态前缀白名单）
    kb_roots = [Path(r).resolve() for r in (ctx.kb_roots or []) if r]
    if p.is_relative_to(upload_root) or p.is_relative_to(out_root) or p.is_relative_to(session_out_root) \
            or (sb_root and p.is_relative_to(sb_root)) \
            or (skill_root and p.is_relative_to(skill_root)) or p.is_relative_to(global_skill_root) \
            or any(p.is_relative_to(r) for r in kb_roots):
        return None
    return ("无权访问该路径（file_path 须为完整服务器路径——从 file_search 返回的 file_path、"
            "【上传文件】清单或【产出记录】原样复制；禁止裸文件名/数字 id。"
            "本会话产出请改用 read_output）")


def validate_output_readable(file_path: str, ctx: ToolContext) -> str | None:
    """read_output 专用校验（B10）：放行本会话全部轮次产出根与本人上传根（跨轮读回自己产出）。

    安全边界：仅本人 session 的 /data/outputs/{session_id}/** 与
    /data/uploads/users/{user_id}/{session_id}/** 及本会话沙盒根；kb 物理文件不放行
    （知识库原文件走 file_parse 的 kb_roots 白名单，不经 read_output）；其他会话/系统路径不放行。
    A1（2026-08-10）：追加本会话沙盒根（work 中间产物可读回）。
    2026-08-20：入口兼容产出 URL 形式（resolve_output_url 转换后校验，读取点须用转换路径）。
    """
    from pathlib import Path

    resolved = resolve_output_url(file_path, ctx)
    if resolved:
        file_path = resolved
    try:
        p = Path(file_path).resolve()
    except OSError:
        return f"非法路径: {file_path[:80]}"
    session_out_root = (Path(_settings.output_dir) / ctx.session_id).resolve()
    upload_root = (Path(_settings.upload_dir) / "users" / str(ctx.user_id) / ctx.session_id).resolve()
    sb_root = _sandbox_session_root(ctx)
    if p.is_relative_to(session_out_root) or p.is_relative_to(upload_root) or (sb_root and p.is_relative_to(sb_root)):
        return None
    return "无权访问该路径（file_path 须为完整服务器路径——从【产出记录】原样复制，禁止裸文件名/数字 id）"


# 导入工具实现以触发注册（新增工具在此追加 import）
from app.agent.tools import ask_user, av_tools, chart_gen, doc_tools, file_search, html_report, intent_events, media_tools, memory_tools, read_output, script_sandbox, search_tools, skill_read, subagent, todo_step, video_gen, zip_tools  # noqa: E402,F401
