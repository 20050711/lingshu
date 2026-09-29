"""run_script 沙箱工具（三期 M17 + 四期重构 + 4.1 多语言）：隔离环境执行用户代码，结果回传对话。

执行器已抽取到 services/sandbox_exec.py（与 skill 工具共用，隔离措施单份实现）：
- 解释器：python（python3 -S + 显式 PYTHONPATH=aip site-packages，标准依赖可用）/ bash / node
- 独立工作目录 /data/sandbox/{session}/{round}/{uuid} + 干净 env（无 API key 等敏感变量）
- preexec_fn 资源限制：RLIMIT_AS=512MB / RLIMIT_CPU=30s / RLIMIT_NPROC（防 fork 炸弹）
- start_new_session + 超时 killpg(SIGKILL)：杀整个进程组（含子进程），asyncio 原生子进程不阻塞事件循环
- 输出截断 200KB（HANDOVER 踩坑 26 原则：截断须提示）
- ToolSpec write=True：执行类工具（v2 框架无授权卡，沙盒隔离兜底；恒注入）
"""
from __future__ import annotations

import shutil
import sysconfig
import uuid
from pathlib import Path

from app.agent.tools import ToolContext, ToolSpec, register_tool
from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.url_utils import output_url

logger = get_logger("tools.script_sandbox")
from app.services import sandbox_exec

# aip 环境的 site-packages（沙箱 -S 模式下显式挂载标准依赖）
_SITE_PACKAGES = sysconfig.get_paths()["purelib"]

_settings = get_settings()

LANGS = {"python", "bash", "node"}


def _work_dir(ctx: ToolContext) -> Path:
    """A6（D19）：work 目录按 (session_id, round_id) 隔离——round_id 即 ask_id（B10 原子分配无复用）：
    同 ask 内多工具轮共享 work（中间产物复用设计保留），并发 ask 天然隔离；
    全 UUID 会话目录消灭原 session[:8] 前缀碰撞串台。跨 ask 复用经白名单显式读（A1）。
    支柱 1（2026-08-10）：子代理 ctx 携带 work_subdir → work/{sub_1}/ 子目录（隔离写域，
    父 work 中间产物经 bwrap 只读白名单可见；None 时行为完全不变）。
    """
    base = Path(_settings.sandbox_dir) / str(ctx.session_id) / str(ctx.round_id) / "work"
    if not ctx.work_subdir:
        return base
    # F1（红队二次）：work_subdir 兜底 containment——子代理层已白名单校验（subagent.py），
    # 此处纵深防御：resolve 后必须仍在 work 根内，否则回落根目录（拒绝逃逸而非报错）
    try:
        sub = (base / ctx.work_subdir).resolve()
    except OSError:
        return base
    if not sub.is_relative_to(base.resolve()):
        return base
    return base / ctx.work_subdir


def _work_files_snapshot(work_dir: Path) -> dict:
    """A5（D5）：work 文件清单（带大小）+ note 提示（共 N 个、已列前 20）。
    T3（2026-08-10）：≤2KB 小文件附内容预览（首 N 字符，最多 8 个）——21 轮事故中
    work_files 只有名称+大小，LLM 无法确认 extracted/*.txt 提取质量 → 只能反复重读 1.2MB 模板；
    带预览后先看预览即可判断复用，掐断验证性重读。"""
    # F3（红队二次）：过滤指向沙盒外的 symlink（宿主侧不跟随，防外泄文件进入清单/预览）
    files = sorted((p for p in work_dir.iterdir() if p.is_file() and _assert_contained(p, work_dir)),
                   key=lambda p: p.name)
    listed: list[dict] = []
    previews = 0
    for p in files[:20]:
        # 2026-08-31（信息完备性 G1）：补绝对路径——agent 拼不出沙盒根（session_id 全 UUID+round
        # 不在上下文），裸文件名传 file_parse/zip_pack 必失败；path 可直接原样传给读取/打包工具
        item = {"name": p.name, "size": p.stat().st_size, "path": str(p)}
        if item["size"] <= 2048 and previews < 8:
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
                item["preview"] = " ".join(text.split())[:_settings.sandbox_workfile_preview_chars]
                previews += 1
            except OSError:
                pass
        listed.append(item)
    names = "、".join(f["name"] for f in listed) or "无"
    note = (
        f"沙箱工作目录（本 ask 内共享，可经白名单显式读其他轮次产物）现有 {len(files)} 个文件"
        + (f"，已列前 20（其余 {len(files) - 20} 个省略，可用 mode=grep 搜索定位）" if len(files) > 20 else "：")
        + f"{names}。"
        + ("≤2KB 文件已含内容预览，先看预览确认复用质量；大文件用 mode=grep 定位后按需读取。"
           if previews else "")
        + "回答用户时给出执行结果与结论，不要复述本 note。"
    )
    return {"work_files": listed, "note": note}


def _safe_work_file(work_dir: Path, file: str) -> Path | None:
    """A1：文件名安全校验——拒绝 /、..、绝对路径（与主流 Agent 工具的 Edit/Write 语义一致）。"""
    if not file or Path(file).name != file or file in (".", ".."):
        return None
    return work_dir / file


def _assert_contained(p: Path, root: Path) -> bool:
    """F1/F3（红队二次）：路径 containment 校验——resolve 后必须在 root 内。

    沙盒内脚本可创建指向宿主文件的符号链接（work 目录 --bind 可写），宿主侧
    is_file/read_text/write_text/stat/copy2 均跟随符号链接 → 读/写任意宿主文件。
    对齐 get_output 与 validate_readable_path 的 resolve 敏感模式；文件不存在时
    resolve 只解析父路径（work 根内）→ 放行（首次 write 场景）。
    """
    try:
        return p.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def _grep_work(work_dir: Path, target: Path | None, pattern: str) -> dict:
    """A1 mode=grep：work 目录内容搜索（子串/正则），返回 文件:行号:匹配行（≤50 条）。"""
    import re as _re

    try:
        rx = _re.compile(pattern)
    except _re.error as e:
        return {"error": f"正则编译失败：{e}——可改用普通子串（无正则语法）"}
    # F3（红队二次）：rglob 结果过滤——目录内 symlink 指向沙盒外时宿主侧不跟随
    paths = [p for p in ([target] if target else sorted(work_dir.rglob("*")))
             if _assert_contained(p, work_dir)]
    hits: list[dict] = []
    for p in paths:
        if not p.is_file():
            continue
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(content.splitlines(), 1):
            if rx.search(line) or pattern in line:
                hits.append({"file": p.name, "line": lineno, "text": line[:200]})
                if len(hits) >= 50:
                    break
        if len(hits) >= 50:
            break
    if not hits:
        return {"exit_code": 0, "stdout": f"未找到匹配 {pattern!r} 的内容", "stderr": "",
                "duration_s": 0, **(_work_files_snapshot(work_dir) if target is None else {})}
    return {
        "exit_code": 0,
        "stdout": f"匹配 {len(hits)} 处（文件:行号:内容）：\n" + "\n".join(
            f"{h['file']}:{h['line']}: {h['text']}" for h in hits),
        "stderr": "",
        "duration_s": 0,
    }


async def run_script(args: dict, ctx: ToolContext) -> dict:
    # A1（D1，2026-08-10）：四态模式（与主流 Agent 工具语义一致）——
    #   run  执行代码（-c，不再冗余写 main.py）或执行 work 已有文件（file 参数）
    #   write 全量写 work 文件（对齐 Write）
    #   edit  精确替换 work 文件内容（对齐 Edit：old_text 唯一匹配才替换，免疫行号漂移、省 token）
    #   grep  搜索 work 目录内容（对齐 Grep：返回 文件:行号:匹配行）
    mode = str(args.get("mode") or "run").lower()
    lang = str(args.get("lang") or "python").lower()
    file = str(args.get("file") or "").strip()
    code = str(args.get("code") or "")
    old_text = str(args.get("old_text") or "")
    new_text = str(args.get("new_text") or "")
    pattern = str(args.get("pattern") or "")

    if not _settings.sandbox_enabled:
        return {"error": "沙箱执行已禁用（sandbox_enabled=false）"}
    if mode not in ("run", "write", "edit", "grep", "deliver"):
        return {"error": f"mode 仅支持 run/write/edit/grep/deliver（收到 {mode!r}）"}
    if lang not in LANGS:
        return {"error": f"暂不支持语言 {lang}（支持: {sorted(LANGS)}）"}

    # A6（D19）：round 级 work 目录（会话×round 隔离，全 UUID 消灭碰撞）
    work_dir = _work_dir(ctx)
    work_dir.mkdir(parents=True, exist_ok=True)

    target = None
    if file:
        # 2026-08-20：技能脚本执行——file 为本团队 active 技能 scripts 目录内绝对路径时直通
        # （技能目录 bwrap 只读挂载，脚本可执行；work 目录外仅此白名单可执行）
        skill_script = None
        p_file = Path(file)
        if p_file.is_absolute() and mode == "run":  # 技能脚本仅 run 模式可执行
            try:
                from app.services.skill_file_service import (
                    get_active_global_skill_script_dirs, get_active_skill_script_dirs,
                )

                # 2026-08-21：团队技能 + 全局技能（默认AI技能）脚本目录白名单
                for d in await get_active_skill_script_dirs(ctx.department_id, ctx.user_id):
                    if p_file.is_relative_to(Path(d)):
                        skill_script = p_file
                        break
                if skill_script is None:
                    for d in await get_active_global_skill_script_dirs(ctx.department_id, ctx.user_id):
                        if p_file.is_relative_to(Path(d)):
                            skill_script = p_file
                            break
            except Exception as e:
                logger.warning("技能脚本解析失败: %s", str(e)[:100])
        if skill_script:
            target = skill_script
        else:
            target = _safe_work_file(work_dir, file)
            if target is None:
                return {"error": f"非法文件名 {file!r}：仅允许 work 目录内文件名（不含 / 与 ..），"
                                 f"需要子目录请先用脚本创建"}
            # F3（红队二次）：统一 containment——沙箱内可创建指向宿主文件的 symlink，
            # 宿主侧 write/edit/grep/deliver/run 均会跟随；resolve 后出 work 根即拒绝
            # （技能脚本为白名单目录直通，跳过 work containment——技能目录本身只读挂载）
            if not _assert_contained(target, work_dir):
                return {"error": f"文件 {file!r} 指向工作目录之外（符号链接逃逸被拒）——"
                                 f"请删除 symlink 后改用真实文件"}

    # ===== grep：搜索 work 目录 =====
    if mode == "grep":
        if not pattern:
            return {"error": "grep 模式需要 pattern 参数（子串或正则）"}
        return _grep_work(work_dir, target, pattern)

    # ===== write：全量写文件 =====
    if mode == "write":
        if target is None:
            return {"error": "write 模式需要 file 参数（目标文件名）"}
        if not code:
            return {"error": "write 模式需要 code 参数（文件内容）"}
        if len(code) > _settings.sandbox_write_max_chars:
            return {"error": f"内容超过 {_settings.sandbox_write_max_chars} 字符限制——"
                             f"可拆分为多次 write（如 part1.html/part2.html）后 run 拼装，或用脚本循环生成"}
        target.write_text(code, encoding="utf-8")
        return {"exit_code": 0, "stdout": f"已写入 {file}（{len(code)} 字符），后续轮次可复用",
                "stderr": "", "duration_s": 0, **(_work_files_snapshot(work_dir))}

    # ===== edit：精确替换（old_text 必须唯一匹配）=====
    if mode == "edit":
        if target is None:
            return {"error": "edit 模式需要 file 参数（目标文件名）"}
        if not old_text:
            return {"error": "edit 模式需要 old_text 参数（要替换的原文，需唯一匹配）"}
        if not target.exists():
            return {"error": f"文件 {file} 不存在——请先 mode=run 生成或 mode=write 写入"}
        content = target.read_text(encoding="utf-8")
        cnt = content.count(old_text)
        if cnt == 0:
            return {"error": f"未在 {file} 中找到匹配文本——请先用 file_parse/read_output 读取确认"
                             f"（注意大小写、空白与换行），再重试 edit"}
        if cnt > 1:
            return {"error": f"匹配文本在 {file} 中出现 {cnt} 次、不唯一——请附带更多上下文"
                             f"（包含前后行）使匹配唯一后再重试"}
        target.write_text(content.replace(old_text, new_text), encoding="utf-8")
        return {"exit_code": 0, "stdout": f"已在 {file} 中替换 1 处（{len(new_text)} 字符 → 新内容）",
                "stderr": "", "duration_s": 0, **(_work_files_snapshot(work_dir))}

    # ===== deliver：把 work 中已完成文件发布为交付物（host 侧拷贝，不触碰 bwrap 挂载）=====
    # T4（2026-08-10）：沙盒产物→交付物的唯一桥接——21 轮事故根因之一：run_script 产物永远
    # 成不了交付物（bwrap 产出目录只读 + 结果无 file_path），LLM 做不出 HTML 也无法交付，
    # 从"做出东西"滑向"永远在准备"。deliver 在 host 侧 copy2 到产出目录，返回 file_path
    # → tool_exec 自动计入产出记录/前端预览。报告装配仍首选专用工具，deliver 兜底大体积产物。
    if mode == "deliver":
        # 支柱 1（2026-08-10）：子代理内禁止交付（交付永远由主代理经授权后执行——能力面收窄边界）
        if ctx.subagent_id:
            return {"error": f"子代理 {ctx.subagent_id} 不允许 deliver（交付由主代理执行）——"
                             f"请把产物留在子目录 {ctx.work_subdir or ''}/ 并在报告中说明"}
        if target is None:
            return {"error": "deliver 模式需要 file 参数（工作目录中的文件名）"}
        if not target.exists():
            return {"error": f"work 目录中没有文件 {file}——先 mode=run/write 生成后再 deliver"}
        size = target.stat().st_size
        if size > _settings.sandbox_deliver_max_bytes:
            return {"error": f"文件 {size} 字节超过 deliver 大小上限（{_settings.sandbox_deliver_max_bytes} 字节），"
                             f"请压缩/拆分后重试或改用其他方式交付"}
        out_dir = Path(ctx.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        fname = f"{uuid.uuid4().hex[:8]}_{target.name}"
        try:
            shutil.copy2(target, out_dir / fname)
        except OSError as e:
            return {"error": f"deliver 拷贝失败: {e}"}
        return {
            "file_path": output_url(ctx.session_id, ctx.round_id, fname),
            "label": target.name,
            "size": size,
            "note": f"已把工作目录文件 {target.name} 发布为交付物（{size} 字节），前端可预览/下载。",
        }

    # ===== run：执行代码或 work/技能目录已有文件 =====
    script_path = None
    if file:
        if code:
            return {"error": "mode=run 时 code 与 file 二选一（file=执行 work 目录或技能脚本目录中已有文件）"}
        # 2026-08-20：技能脚本执行——file 绝对路径命中本团队 active 技能 scripts 目录则直接执行
        # （技能目录 bwrap 只读挂载；否则按现状 work 目录相对路径）
        # 2026-08-21：追加全局技能（默认AI技能）脚本目录命中（三层交集过滤）
        skill_script = None
        try:
            from app.services.skill_file_service import (
                get_active_global_skill_script_dirs, get_active_skill_script_dirs,
            )

            for d in await get_active_skill_script_dirs(ctx.department_id, ctx.user_id):
                if target.is_relative_to(Path(d)):
                    skill_script = target
                    break
            if skill_script is None:
                for d in await get_active_global_skill_script_dirs(ctx.department_id, ctx.user_id):
                    if target.is_relative_to(Path(d)):
                        skill_script = target
                        break
        except Exception as e:
            logger.warning("技能脚本解析失败: %s", str(e)[:100])
        if skill_script:
            script_path = skill_script
            code = None
        else:
            if not target.exists():
                return {"error": f"work 目录中没有文件 {file}——可先用 mode=write 写入或 mode=run 生成"}
            script_path = target
            code = None
    if not code and script_path is None:
        return {"error": "需要提供 code 参数（或 mode=run + file 执行已有文件）"}
    if code and len(code) > 20_000:
        return {"error": "代码超过 20000 字符限制"}

    # 沙盒 v3（bwrap）：白名单只读路径 = 本会话上传目录 + **本会话产出根（全部轮次）** + 本会话沙盒根
    # （A1：file_parse/read_output 与 run_script 的 bwrap 均可见本会话其他轮次 work 产物，
    #  跨 ask 复用靠显式读；本 ask work 目录为 --bind 可写，挂载顺序见 _bwrap_wrap）
    # 2026-09-22：产出侧由"当轮 output_dir"改成**本会话产出根**——与 file_parse/read_output 的
    # 放行范围（本会话全部轮次）对齐。原口径下"第 1 轮导出的表格 → 第 2 轮用脚本处理"读不到，
    # agent 只能重导一遍（CLI 全局串行，代价高）。仍只读挂载、仍是同会话同用户。
    upload_root = Path(_settings.upload_dir) / "users" / str(ctx.user_id) / str(ctx.session_id)
    sandbox_session_root = Path(_settings.sandbox_dir) / str(ctx.session_id)
    output_session_root = Path(_settings.output_dir) / str(ctx.session_id)
    bwrap_read_dirs = [str(d) for d in (upload_root, output_session_root, sandbox_session_root) if d.exists()]
    # 2026-08-20：技能目录挂载——**仅 dept_admin/admin/ceo 可写**（agent 可修改技能文件，改后下轮注入
    # 自动同步；改坏则容错跳过）；**员工（employee）只读**（不可反向改写技能，只能浏览/执行）；
    # 团队隔离；查询失败降级不阻塞
    # 2026-08-21：全局技能（默认AI技能）目录**所有角色只读挂载**（含 admin/dept_admin——仅运维经
    # 上传接口修改，sandbox 内不可反向改写）；三层交集过滤（运维active ∩ 团队开关 ∩ 员工偏好）
    bwrap_write_dirs: list[str] = []
    try:
        from app.services.skill_file_service import (
            get_active_global_skill_roots, get_active_skill_rw_dirs, get_active_skill_script_dirs,
        )

        skill_roots = [d for d in await get_active_skill_rw_dirs(ctx.department_id, ctx.user_id) if Path(d).exists()]
        if ctx.user_role in ("dept_admin", "admin", "ceo"):
            bwrap_write_dirs = skill_roots
        else:
            bwrap_read_dirs += skill_roots
        global_roots = [d for d in await get_active_global_skill_roots(ctx.department_id, ctx.user_id) if Path(d).exists()]
        bwrap_read_dirs += global_roots
    except Exception as e:
        logger.warning("技能目录挂载失败: %s", str(e)[:100])
    # 2026-09-10 知识库原文件：可见知识库根逐目录只读挂载（bind mount 零拷贝；存在才挂，降级不阻塞）
    try:
        for root in (ctx.kb_roots or []):
            if root and Path(root).exists():
                bwrap_read_dirs.append(root)
    except Exception as e:
        logger.warning("知识库根挂载失败（降级）: %s", str(e)[:100])

    result = await sandbox_exec.run(
        lang=lang,
        code=code,
        script_path=script_path,
        cwd=str(work_dir),
        timeout_s=_settings.sandbox_timeout_seconds,
        py_s_flag=True,
        pythonpath=_SITE_PACKAGES,
        # 2026-08-07：node 从配置绝对路径取（clean_env PATH 无 nvm 节点，之前报"未安装"）；
        # node V8 预留虚拟内存（CodeRange）在 RLIMIT_AS=512MB 下 OOM——SEC-11（2026-08-17）：
        # 由 unlimited 改为 sandbox_node_memory_mb=2048 封顶（保留 CodeRange 余量同时堵内存耗尽面）
        interpreter=_settings.node_path if lang == "node" else None,
        memory_mb=_settings.sandbox_node_memory_mb if lang == "node" else _settings.sandbox_memory_mb,
        bwrap=True,
        bwrap_read_dirs=bwrap_read_dirs,
        bwrap_write_dirs=bwrap_write_dirs,
        fsize_mb=_settings.sandbox_fsize_mb,  # A5（D5）：单文件写入上限（SIGXFSZ 杀进程）
    )
    if "error" in result:
        return result
    # T2（2026-08-10）：stdout 预截断（首 1/5 + 尾 4/5，总上限 sandbox_stdout_cap_chars）——
    # 21 轮事故：每轮完整 stdout 回填最高 200K，累计远超 150K 历史预算 → 早期轮次上下文被挤掉
    # （结尾废稿"复述轮 1 中间文本"的直接诱因）。降权后单轮 ~8-10K；stderr 保持原样
    # （D2 已保尾 6000，异常堆栈诊断需要）；_brief/summary 取 stdout 尾段语义不变。
    if isinstance(result.get("stdout"), str):
        out = result["stdout"]
        cap = _settings.sandbox_stdout_cap_chars
        if len(out) > cap:
            head_n = max(cap // 5, 1)
            # 说明文本按字符计（中文），预留 200 字符余量，保证总长 ≤ cap
            tail_n = max(cap - head_n - 200, 0)
            result["stdout"] = (f"{out[:head_n]}\n...（stdout 过长，已截断为"
                                f"首 {head_n} + 尾 {tail_n} 字符，总上限 {cap}）\n{out[-tail_n:]}")
    # A5：文件清单回传（带大小；LLM 判断读取量）+ note 提示总数
    try:
        result.update(_work_files_snapshot(work_dir))
    except OSError:
        result["note"] = "脚本在隔离沙箱中执行（受限环境）。回答用户时给出执行结果与结论，不要复述本 note。"
    return result


register_tool(
    ToolSpec(
        name="run_script", progress_keys=("stdout", "stderr", "work_files", "file_path"),
        write=True,  # 执行类工具：沙盒隔离内直接执行（v2 框架无授权卡；write 供计划批准前写门禁）
        display_name="沙盒脚本",
        icon="code",
        summary="运行 Python / Bash / Node 脚本处理数据（通用兜底）",
        group="代码",
        sort_order=10,
        user_description=(
            "在隔离沙箱中运行 Python/Bash/Node 脚本（受限环境：超时/内存限制/独立目录），"
            "用于计算、数据处理、文件批处理、验证等通用场景。"
        ),
        description=(
            "What：在隔离沙箱中执行 Python/Bash/Node 代码，并管理沙箱工作目录文件"
            "（兜底工具：当其他专用工具无法完成任务时使用；工作目录 = 本 ask 内共享的沙盒区）。\n"
            "When：计算、数据处理/转换、文件批处理、验证、多步骤逻辑等无专用工具的场景。\n"
            "**沙盒离线（SEC-02）**：沙盒无网络访问——需要联网获取数据/爬取外部网站时用 web_search 或如实告知，"
            "不要在沙盒内写联网代码（必然失败）。\n"
            "How 五种模式（mode 参数）：\n"
            "  run：执行代码（code 参数，结果用 print 输出）；或执行工作目录已有文件（file 参数，不传 code）；"
            "倾向一轮完成一个完整子任务（如一次遍历模板提取全部组件落盘），禁止每轮只提取一个片段。\n"
            "  write：把内容写入工作目录文件（file + code），供后续轮次复用（对齐 Write）。"
            "**支持 ≤10 万字符的大文件**（如完整 HTML 可直接一次写入，无需脚本绕行）；"
            "建议单次 ≤5 万字符（受单轮调用时长约束），更大内容拆分为多次 write（part1.html/part2.html）后 run 拼装；"
            "数据密集（成百上千行重复数据）场景才用脚本循环生成。\n"
            "  edit：精确替换工作目录文件内容（file + old_text + new_text）——old_text 必须唯一匹配，"
            "不唯一/未找到会返回错误并提示你读取确认；改小片段只传片段，无需重传整个文件（对齐 Edit）。\n"
            "  grep：搜索工作目录内容（pattern 子串或正则；file 可选限定单文件）返回 文件:行号:匹配行（对齐 Grep）。\n"
            "  deliver：把工作目录中已完成的大文件（file 参数）发布为交付物（host 拷贝到产出目录，"
            "前端可预览/下载）——适用于最终文件已在沙盒内拼装好、体积过大无法经工具参数传递的场景。\n"
            "禁止事项：本工具**不生成 HTML/报告/文档等交付物**（沙盒内产出目录只读）——"
            "提取/清洗等中间结果落盘工作目录供复用，最终文件交付走 doc_export / html_report / framework，"
            "或在 run_script 中 mode=deliver 发布 work 中已完成文件。\n"
            "工作目录按本条消息隔离：本条消息内多轮调用共享，跨消息不复用（读旧产出走 read_output）；"
            "同一条消息内此前的中间文件可用 mode=grep 定位 + mode=run file= 复用，"
            "或用 file_parse/read_output 按路径读取（path 以工作目录文件名为准）。\n"
            "Result：返回 exit_code/stdout/stderr/duration_s/work_files；回答给出执行结果与结论，不复述输出原文。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["run", "write", "edit", "grep", "deliver"],
                         "description": "run=执行代码或已有文件（默认）；write=写入文件；edit=精确替换文件内容；grep=搜索工作目录；deliver=发布工作目录文件为交付物（file 参数）"},
                "lang": {"type": "string", "enum": ["python", "bash", "node"], "description": "语言（mode=run 时用；默认 python）"},
                "code": {"type": "string", "description": "mode=run 要执行的代码（print 输出结果）；mode=write 为文件内容"},
                "file": {"type": "string", "description": "工作目录内文件名（mode=run 执行已有文件 / write/edit 目标）"},
                "old_text": {"type": "string", "description": "mode=edit 要替换的原文（须在文件中唯一匹配）"},
                "new_text": {"type": "string", "description": "mode=edit 替换成的新文本"},
                "pattern": {"type": "string", "description": "mode=grep 的搜索模式（子串或正则）"},
            },
            "required": ["mode"],
        },
        queue="sandbox",
        handler=run_script,
    )
)
