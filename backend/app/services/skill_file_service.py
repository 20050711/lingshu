"""团队技能（SKILL.md / 技能包 zip）服务（四期重构 + 2026-08-20 zip 扩展）。

SKILL.md 格式（frontmatter yaml + Markdown 正文）：

    ---
    name: weekly-report-generator
    description: 生成团队周报。用户要求周报/周度报告时使用。
    tools: ["file_parse", "doc_export"]     # 可选：执行本技能可用的工具（提示作用，不改变工具白名单）
    ---
    正文：给 Agent 的指令文本（步骤/格式/口径等），纯指令型技能可无 tools 声明。

上传二选一（2026-08-20）：
- 单 .md：纯指令型，无磁盘文件（skill_dir=NULL）
- zip 包：根含 SKILL.md（描述源）+ 脚本任意子目录 → 落盘 {skill_files_dir}/{dept_id}/{skill_id}/md/ 与 /scripts/

存储：skill_files 表（dept_id 维度；团队管理员上传/启停，运维管理可管任意团队）。
注入策略（无关键词匹配）：active 技能常驻 system prompt，description + 正文（截断）+ 脚本目录/清单。
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import shutil
import stat
import uuid
import zipfile
from pathlib import Path

import yaml
from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import get_global_engine

logger = logging.getLogger("services.skill_file_service")

# 2026-08-26（Q8）：技能正文改按需加载——常驻只注入清单（名字+简介），
# 正文/脚本清单由 skill_read 工具按名获取；预算大幅收紧
_MAX_LIST_DESC_CHARS = 100
_MAX_TOTAL_CHARS = 1500

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.S)
# 删除/替换时对 skill_dir 的格式校验（防 DB 被篡改后路径穿越）：{dept_id}/{skill_id}
_SKILL_DIR_RE = re.compile(r"^[A-Za-z0-9_\-]+/\d+$")


def _skill_root() -> Path:
    return Path(get_settings().skill_files_dir)


def parse_skill_md(content: str) -> dict:
    """解析 SKILL.md → {name, description, tools(list|None), body}。缺 name/description 抛 ValueError。

    CRLF 兼容（2026-08-20）：zip 内 SKILL.md 可能 \r\n，frontmatter 按 \n 匹配前归一化。
    """
    content = content.replace("\r\n", "\n")
    m = _FRONTMATTER_RE.match(content.strip())
    if not m:
        raise ValueError("不是有效的 SKILL.md：缺少 --- frontmatter 块")
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except Exception as e:
        raise ValueError(f"SKILL.md frontmatter 解析失败: {str(e)[:100]}")
    if not isinstance(fm, dict):
        raise ValueError("SKILL.md frontmatter 必须是 yaml 对象")
    name = (fm.get("name") or "").strip()
    description = (fm.get("description") or "").strip()
    if not name or not description:
        raise ValueError("SKILL.md 必须包含 name 与 description 字段")
    tools = fm.get("tools")
    if tools is not None and not isinstance(tools, list):
        raise ValueError("tools 字段必须是字符串数组（如 [\"file_parse\", \"doc_export\"]）")
    return {"name": name[:100], "description": description[:500], "tools": tools, "body": m.group(2).strip()}


def parse_skill_zip(data: bytes) -> dict:
    """校验 + 内存解压技能包 zip → {meta, raw_md(bytes), files: {relpath: bytes}}。

    安全（2026-08-20）：
    - zip slip 三层防御：成员路径校验（反斜杠归一化/绝对路径/.. /符号链接拒绝）→ 内存读（CRC 校验）
      → 落盘时二次 containment（_write_script_files）
    - 大小/文件数限制：zip ≤ skill_zip_max_bytes；解压总大小 ≤ skill_zip_max_total_bytes；
      文件数 ≤ skill_zip_max_files
    - SKILL.md 必须在 zip 根（大小写敏感），frontmatter 解析失败即拒绝
    """
    settings = get_settings()
    if len(data) > settings.skill_zip_max_bytes:
        raise ValueError(f"技能包超过 {settings.skill_zip_max_bytes // 1048576}MB 限制")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("不是有效的 zip 文件")
    try:
        if zf.testzip():
            raise ValueError("zip 文件损坏")
        infos = zf.infolist()
        if len(infos) > settings.skill_zip_max_files:
            raise ValueError(f"技能包文件数超过 {settings.skill_zip_max_files} 限制")
        if sum(i.file_size for i in infos) > settings.skill_zip_max_total_bytes:
            raise ValueError("技能包解压总大小超过 20MB 限制")
        files: dict[str, bytes] = {}
        for info in infos:
            name = info.filename.replace("\\", "/")
            if not name or name.startswith("/") or name.split("/")[0] in ("..", "."):
                raise ValueError(f"非法成员路径: {info.filename[:80]}")
            if ".." in name.split("/"):
                raise ValueError(f"非法成员路径: {info.filename[:80]}")
            if stat.S_ISLNK(info.external_attr >> 16):  # zip 符号链接成员 → 拒绝
                raise ValueError(f"不支持符号链接成员: {info.filename[:80]}")
            if info.is_dir():
                continue
            if info.file_size > settings.skill_zip_max_total_bytes:  # 防谎报
                raise ValueError("技能包解压总大小超过 20MB 限制")
            content = zf.read(info)
            if len(content) > settings.skill_zip_max_total_bytes:
                raise ValueError("技能包解压总大小超过 20MB 限制")
            files[name] = content
    finally:
        zf.close()
    if "SKILL.md" not in files:
        raise ValueError("技能包根目录缺少 SKILL.md")
    raw_md = files.pop("SKILL.md")
    meta = parse_skill_md(decode_skill_text(raw_md))
    return {"meta": meta, "raw_md": raw_md, "files": files}


def decode_skill_text(data: bytes) -> str:
    """SKILL.md 文本解码（2026-08-21 编码隐患 M3）：优先 UTF-8 严格；失败回退 GBK
    （Windows 老记事本等 GBK 编辑器场景——原强制 UTF-8 replace 会静默把中文变 � 入库）。"""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("gbk", errors="replace")


def _write_skill_files(root: Path, raw_md: bytes, files: dict[str, bytes]) -> None:
    """落盘技能目录：root/md/SKILL.md（原样字节）+ root/scripts/**（含子目录，containment 二次校验）。"""
    md_dir = root / "md"
    md_dir.mkdir(parents=True, exist_ok=True)
    (md_dir / "SKILL.md").write_bytes(raw_md)
    if files:
        scripts_dir = root / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        target = scripts_dir.resolve()
        for rel, content in files.items():
            dest = (scripts_dir / rel).resolve()
            if not dest.is_relative_to(target):
                raise ValueError(f"非法成员路径: {rel[:80]}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content)


async def _rows(sql: str, params: dict | None = None) -> list:
    engine = get_global_engine()
    async with engine.connect() as conn:
        rows = (await conn.execute(text(sql), params or {})).all()
    return rows


async def list_skills(dept_id: str | None = None) -> list[dict]:
    """团队技能列表；dept_id=None 返回全部（运维管理），否则按团队过滤。"""
    sql = ("SELECT id, dept_id, skill_name, description, tools, status, sort_order, created_at, skill_dir "
           "FROM skill_files")
    params: dict = {}
    if dept_id is not None:
        sql += " WHERE dept_id=:d"
        params["d"] = dept_id
    sql += " ORDER BY sort_order, id"
    rows = await _rows(sql, params)
    return [
        {
            "id": r.id, "dept_id": r.dept_id, "name": r.skill_name, "description": r.description,
            "tools": json.loads(r.tools) if r.tools else None,
            "status": r.status, "sort_order": r.sort_order,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "skill_dir": r.skill_dir, "has_scripts": bool(r.skill_dir),
        }
        for r in rows
    ]


_DEPT_PREF_PREFIX = "dept:"  # user_skill_prefs.skill_id 命名空间（团队技能；避免与平台工具 id 冲突）
# 配置过偏好的哨兵行（保存时总是写入）——区分「未配置=全启用」与「配置了空集合=全关」
# （两者 DB 都是零 dept: 数字行，无哨兵无法区分）
_DEPT_PREF_SENTINEL = "dept:__configured__"
# 2026-08-21：全局技能（默认AI技能）个人偏好命名空间——与 dept: 并列隔离
_GLOBAL_PREF_PREFIX = "g:"
_GLOBAL_PREF_SENTINEL = "g:__configured__"


async def get_user_dept_skill_prefs(user_id: int) -> set[int] | None:
    """员工团队技能偏好；None=未配置（全部启用，兼容现状）。

    skill_id 形如 'dept:{skill_files.id}'；__auto__ 与平台工具 id 不在此列。
    """
    if not user_id:
        return None
    rows = await _rows(
        "SELECT skill_id, enabled FROM user_skill_prefs WHERE user_id=:uid AND skill_id LIKE :p",
        {"uid": user_id, "p": _DEPT_PREF_PREFIX + "%"},
    )
    if not rows:
        return None  # 无哨兵也无数字行 = 从未配置过（全启用）
    enabled = {
        int(r.skill_id[len(_DEPT_PREF_PREFIX):])
        for r in rows if r.skill_id != _DEPT_PREF_SENTINEL and r.enabled
    }
    return enabled  # 可能为空 set（=配置了全关）


async def get_user_global_skill_prefs(user_id: int) -> set[int] | None:
    """员工全局技能（默认AI技能）个人偏好；None=未配置（跟随团队开关，全部允许）。

    skill_id 形如 'g:{skill_files.id}'；与 dept: 命名空间、平台工具 id 互不干扰。
    """
    if not user_id:
        return None
    rows = await _rows(
        "SELECT skill_id, enabled FROM user_skill_prefs WHERE user_id=:uid AND skill_id LIKE :p",
        {"uid": user_id, "p": _GLOBAL_PREF_PREFIX + "%"},
    )
    if not rows:
        return None  # 无哨兵也无数字行 = 从未配置过（全启用）
    enabled = {
        int(r.skill_id[len(_GLOBAL_PREF_PREFIX):])
        for r in rows if r.skill_id != _GLOBAL_PREF_SENTINEL and r.enabled
    }
    return enabled  # 可能为空 set（=配置了全关）


async def list_active_global_skills(dept_id: str, user_id: int | None = None) -> list[dict]:
    """运维发布的全局技能（dept_id='global'）active 列表——三层交集（最严生效）：

    运维启停（status='active'）∩ 团队管理员开关（dept_global_skills.{dept_id}，None=全部允许）
    ∩ 员工个人偏好（g: 命名空间，None=跟随团队）。
    """
    rows = await _rows(
        "SELECT id, dept_id, skill_name, description, tools, content, status, sort_order, skill_dir "
        "FROM skill_files WHERE dept_id='global' AND status='active' ORDER BY sort_order, id",
    )
    if not rows:
        return []
    from app.services.config_service import get_dept_global_skills

    dept_whitelist = await get_dept_global_skills(dept_id)
    if dept_whitelist is not None:
        rows = [r for r in rows if r.id in dept_whitelist]
    if user_id is not None:
        prefs = await get_user_global_skill_prefs(user_id)
        if prefs is not None:
            rows = [r for r in rows if r.id in prefs]
    return [
        {
            "id": r.id, "dept_id": r.dept_id, "name": r.skill_name, "description": r.description,
            "tools": json.loads(r.tools) if r.tools else None, "body": r.content, "skill_dir": r.skill_dir,
        }
        for r in rows
    ]


async def get_active_global_skill_script_dirs(dept_id: str, user_id: int | None = None) -> list[str]:
    """全局技能 active 技能的 scripts 目录绝对路径列表（run_script file= 直通白名单用；三层交集过滤）。"""
    rows = await _rows(
        "SELECT skill_dir, id FROM skill_files WHERE dept_id='global' AND status='active' AND skill_dir IS NOT NULL",
    )
    if not rows:
        return []
    from app.services.config_service import get_dept_global_skills

    dept_whitelist = await get_dept_global_skills(dept_id)
    if dept_whitelist is not None:
        rows = [r for r in rows if r.id in dept_whitelist]
    if user_id is not None:
        prefs = await get_user_global_skill_prefs(user_id)
        if prefs is not None:
            rows = [r for r in rows if r.id in prefs]
    root = _skill_root()
    out: list[str] = []
    for r in rows:
        d = root / r.skill_dir / "scripts"
        if d.is_dir():
            out.append(str(d))
    return out


async def get_active_global_skill_roots(dept_id: str, user_id: int | None = None) -> list[str]:
    """全局技能 active 技能根目录列表（bwrap 只读挂载用；全局技能所有角色只读，无 rw 版本）。"""
    rows = await _rows(
        "SELECT skill_dir, id FROM skill_files WHERE dept_id='global' AND status='active' AND skill_dir IS NOT NULL",
    )
    if not rows:
        return []
    from app.services.config_service import get_dept_global_skills

    dept_whitelist = await get_dept_global_skills(dept_id)
    if dept_whitelist is not None:
        rows = [r for r in rows if r.id in dept_whitelist]
    if user_id is not None:
        prefs = await get_user_global_skill_prefs(user_id)
        if prefs is not None:
            rows = [r for r in rows if r.id in prefs]
    root = _skill_root()
    out: list[str] = []
    for r in rows:
        d = root / r.skill_dir
        if d.is_dir():
            out.append(str(d))
    return out


async def list_active_skills(dept_id: str, user_id: int | None = None) -> list[dict]:
    """本团队 active 技能（2026-08-21：user_id 提供时按员工偏好取交集——员工可自行启停）。"""
    rows = await _rows(
        "SELECT id, dept_id, skill_name, description, tools, content, status, sort_order, skill_dir "
        "FROM skill_files WHERE dept_id=:d AND status='active' ORDER BY sort_order, id",
        {"d": dept_id},
    )
    if user_id is not None:
        prefs = await get_user_dept_skill_prefs(user_id)
        if prefs is not None:
            rows = [r for r in rows if r.id in prefs]
    return [
        {
            "id": r.id, "dept_id": r.dept_id, "name": r.skill_name, "description": r.description,
            "tools": json.loads(r.tools) if r.tools else None, "body": r.content, "skill_dir": r.skill_dir,
        }
        for r in rows
    ]


async def get_active_skill_script_dirs(dept_id: str, user_id: int | None = None) -> list[str]:
    """本团队 active 技能的 scripts 目录绝对路径列表（沙盒执行直通判断用；
    2026-08-21：user_id 提供时按员工偏好过滤）。"""
    rows = await _rows(
        "SELECT skill_dir, id FROM skill_files WHERE dept_id=:d AND status='active' AND skill_dir IS NOT NULL",
        {"d": dept_id},
    )
    if user_id is not None:
        prefs = await get_user_dept_skill_prefs(user_id)
        if prefs is not None:
            rows = [r for r in rows if r.id in prefs]
    root = _skill_root()
    out: list[str] = []
    for r in rows:
        d = root / r.skill_dir / "scripts"
        if d.is_dir():
            out.append(str(d))
    return out


async def get_active_skill_rw_dirs(dept_id: str, user_id: int | None = None) -> list[str]:
    """本团队 active 技能根目录列表（沙盒可写挂载——agent 可修改技能文件；团队隔离；
    2026-08-21：user_id 提供时按员工偏好过滤）。"""
    rows = await _rows(
        "SELECT skill_dir, id FROM skill_files WHERE dept_id=:d AND status='active' AND skill_dir IS NOT NULL",
        {"d": dept_id},
    )
    if user_id is not None:
        prefs = await get_user_dept_skill_prefs(user_id)
        if prefs is not None:
            rows = [r for r in rows if r.id in prefs]
    root = _skill_root()
    out: list[str] = []
    for r in rows:
        d = root / r.skill_dir
        if d.is_dir():
            out.append(str(d))
    return out


async def create_skill(dept_id: str, name: str, description: str, body: str,
                       tools: list[str] | None = None, by_user: int = 0) -> int:
    """纯 DB 创建（skill_dir=NULL）；带文件落盘请用 create_skill_with_files。"""
    engine = get_global_engine()
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text("SELECT id FROM skill_files WHERE dept_id=:d AND skill_name=:n"), {"d": dept_id, "n": name}
            )
        ).first()
        if row:
            raise ValueError(f"技能 {name} 已存在")
        skill_id = (
            await conn.execute(
                text("INSERT INTO skill_files (dept_id, skill_name, description, content, tools, created_by) "
                     "VALUES (:d, :n, :desc, :body, :t, :by) RETURNING id"),
                {"d": dept_id, "n": name, "desc": description, "body": body,
                 "t": json.dumps(tools, ensure_ascii=False) if tools else None, "by": by_user},
            )
        ).scalar()
    return skill_id


async def create_skill_with_files(dept_id: str, name: str, description: str, body: str,
                                  raw_md: bytes | None, files: dict[str, bytes] | None,
                                  tools: list[str] | None = None, by_user: int = 0) -> int:
    """创建技能 + 落盘文件（md 原样 + scripts），失败回滚（删行 + 清目录）。"""
    skill_id = await create_skill(dept_id, name, description, body, tools, by_user)
    if not raw_md:
        return skill_id
    skill_dir = f"{dept_id}/{skill_id}"
    root = _skill_root() / skill_dir
    try:
        _write_skill_files(root, raw_md, files or {})
    except Exception:
        # 落盘失败：删行 + 清目录，保持与失败前一致
        try:
            await delete_skill(skill_id)
        except Exception:
            pass
        shutil.rmtree(root, ignore_errors=True)
        raise
    engine = get_global_engine()
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE skill_files SET skill_dir=:d, updated_at=NOW() WHERE id=:id"),
            {"d": skill_dir, "id": skill_id},
        )
    return skill_id


async def replace_skill_files(skill_id: int, raw_md: bytes, files: dict[str, bytes] | None) -> bool:
    """重传技能文件（md/zip 分流后）：tmp 目录写入成功 → 原子替换旧目录；失败保留旧文件。"""
    engine = get_global_engine()
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT skill_dir FROM skill_files WHERE id=:id"), {"id": skill_id}
            )
        ).first()
    if row is None or not row.skill_dir:
        raise ValueError("该技能没有文件目录（纯文本技能请用 content 更新）")
    skill_dir: str = row.skill_dir
    if not _SKILL_DIR_RE.match(skill_dir):
        raise ValueError(f"技能目录格式非法: {skill_dir}")
    root = _skill_root() / skill_dir
    tmp = _skill_root() / f"{skill_dir}.tmp-{uuid.uuid4().hex[:8]}"
    try:
        _write_skill_files(tmp, raw_md, files or {})
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    # 原子替换：删旧（md/scripts）→ rename tmp 为正式目录
    shutil.rmtree(root, ignore_errors=True)
    os.replace(tmp, root)
    return True


async def update_skill(skill_id: int, name: str | None = None, description: str | None = None,
                       body: str | None = None, tools: list[str] | None = None,
                       status: str | None = None, by_user: int = 0) -> bool:
    engine = get_global_engine()
    # 2026-08-20：body 更新时同步磁盘 md/SKILL.md（仅对有文件目录的技能；纯文本技能跳过）
    sync_disk = body is not None
    skill_dir: str | None = None
    sets, params = [], {}
    if name is not None:
        sets.append("skill_name=:n"); params["n"] = name
    if description is not None:
        sets.append("description=:desc"); params["desc"] = description
    if body is not None:
        sets.append("content=:body"); params["body"] = body
    if tools is not None:
        sets.append("tools=:t"); params["t"] = json.dumps(tools, ensure_ascii=False)
    if status is not None:
        if status not in ("active", "disabled"):
            raise ValueError("status 仅支持 active/disabled")
        sets.append("status=:s"); params["s"] = status
    if not sets:
        return False
    if sync_disk:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT skill_dir FROM skill_files WHERE id=:id"), {"id": skill_id}
                )
            ).first()
            skill_dir = row.skill_dir if row else None
    sets.append("updated_by=:by"); params["by"] = by_user
    params["id"] = skill_id
    async with engine.begin() as conn:
        r = await conn.execute(
            text(f"UPDATE skill_files SET {', '.join(sets)}, updated_at=NOW() WHERE id=:id"), params
        )
    if r.rowcount > 0 and skill_dir and _SKILL_DIR_RE.match(skill_dir):
        try:
            # 磁盘为规范形态（frontmatter + 正文重建，保持可再解析）
            md_text = f"---\nname: {name or ''}\ndescription: {description or ''}\n"
            if tools:
                md_text += f"tools: {json.dumps(tools, ensure_ascii=False)}\n"
            md_text += f"---\n\n{body or ''}\n"
            (_skill_root() / skill_dir / "md" / "SKILL.md").write_text(md_text, encoding="utf-8")
        except OSError as e:
            logger.warning("技能磁盘 md 同步失败 skill=%s: %s", skill_id, str(e)[:100])
    return r.rowcount > 0


async def delete_skill(skill_id: int) -> bool:
    engine = get_global_engine()
    async with engine.begin() as conn:  # begin()：DML 自动 commit（connect() 只读事务 DELETE 会回滚）
        row = (
            await conn.execute(
                text("SELECT skill_dir FROM skill_files WHERE id=:id"), {"id": skill_id}
            )
        ).first()
        r = await conn.execute(text("DELETE FROM skill_files WHERE id=:id"), {"id": skill_id})
    if r.rowcount > 0 and row and row.skill_dir:
        # 防 DB 被篡改后路径穿越：目录格式校验
        if _SKILL_DIR_RE.match(row.skill_dir):
            shutil.rmtree(_skill_root() / row.skill_dir, ignore_errors=True)
        else:
            logger.warning("技能目录格式非法，跳过清理: %s", row.skill_dir)
    return r.rowcount > 0


async def _sync_skill_from_file(s: dict, dept_id: str, operator: str = "") -> bool:
    """读技能文件夹 md/SKILL.md → 解析 → 内容变化则同步 DB + audit。返回是否可用（解析成功）。"""
    skill_dir = s.get("skill_dir")
    if not skill_dir:
        return True  # 纯文本技能（无文件）始终可用
    md_file = _skill_root() / skill_dir / "md" / "SKILL.md"
    if not md_file.is_file():
        return False
    try:
        raw = md_file.read_text(encoding="utf-8")
        parsed = parse_skill_md(raw)
    except (OSError, ValueError) as e:
        # 文件被写坏/不可读：跳过注入 + audit（管理员可修复，其他技能不受影响）
        logger.warning("技能文件解析失败 skill=%s: %s", skill_dir, str(e)[:100])
        try:
            engine = get_global_engine()
            async with engine.begin() as conn:
                await conn.execute(
                    text("INSERT INTO audit_log (operator, action, target, detail) "
                         "VALUES (:o, 'skill_file_broken', :t, :d)"),
                    {"o": operator or dept_id, "t": skill_dir,
                     "d": json.dumps({"error": str(e)[:200], "note": "技能文件解析失败，已跳过注入"})},
                )
        except Exception:
            pass
        return False
    changed = (parsed["name"] != s.get("name") or parsed["description"] != s.get("description")
               or parsed["body"] != s.get("body") or parsed["tools"] != s.get("tools"))
    if changed:
        try:
            engine = get_global_engine()
            async with engine.begin() as conn:
                await conn.execute(
                    text("UPDATE skill_files SET skill_name=:n, description=:d, content=:b, "
                         "tools=:t, updated_at=NOW() WHERE id=:id"),
                    {"n": parsed["name"], "d": parsed["description"], "b": parsed["body"],
                     "t": json.dumps(parsed["tools"], ensure_ascii=False) if parsed["tools"] else None,
                     "id": s["id"]},
                )
                await conn.execute(
                    text("INSERT INTO audit_log (operator, action, target, detail) "
                         "VALUES (:o, 'skill_file_updated', :t, :d)"),
                    {"o": operator or dept_id, "t": skill_dir,
                     "d": json.dumps({"skill": parsed["name"], "note": "技能文件被修改，已同步注入内容"})},
                )
        except Exception as e:
            logger.warning("技能文件同步失败 skill=%s: %s", skill_dir, str(e)[:100])
    # 同步内存副本（本次注入用文件内容）
    s["name"], s["description"], s["body"], s["tools"] = (
        parsed["name"], parsed["description"], parsed["body"], parsed["tools"])
    return True


async def build_skill_files_prompt(skills: list[dict], dept_id: str = "", operator: str = "",
                                   section_title: str = "团队技能", max_total_chars: int = _MAX_TOTAL_CHARS) -> str:
    """技能注入：技能清单（名字 + 一句话简介），常驻 system prompt。

    2026-08-26（Q8 按需加载）：正文/脚本清单不再常驻——清单引导 LLM 按名调用
    skill_read 工具获取完整说明。常驻预算 团队 8000/全局 4000 → 默认 1500。

    保留（与既有语义一致）：
    - 有文件技能的注入数据**实时读文件**（_sync_skill_from_file：文件为准 + 变化同步 DB + audit）
    - 文件解析失败（被写坏）→ 跳过注入 + audit 告警（管理员可修复，其他技能不受影响）
    - SEC-09 注入检测：description+body 过 InputFilter，不过则跳过该技能，末尾追加提示行
    """
    from app.core.input_filter import InputFilter

    if section_title == "团队技能":
        intro = ("以下为本团队可用技能（名字 + 一句话简介）。用户需求匹配某技能名时："
                 "**必须先调用 skill_read(name=技能名) 获取完整执行说明与脚本清单**，"
                 "再按说明执行（脚本经 run_script 执行，目录权限由会话角色决定：管理员可修改技能文件，员工只读）：")
    else:
        intro = ("以下为运维发布的全局可用技能（名字 + 一句话简介）。用户需求匹配某技能名时："
                 "**必须先调用 skill_read(name=技能名) 获取完整执行说明与脚本清单**，再按说明执行"
                 "（技能目录对所有角色只读，仅运维可经上传接口修改）：")
    parts: list[str] = [f"## {section_title}", intro]
    total = 0
    skipped: list[str] = []
    for s in skills:
        if not await _sync_skill_from_file(s, dept_id, operator):
            skipped.append(f"{s['name']}（文件损坏）")
            continue
        desc = s.get("description") or ""
        body = s.get("body") or ""
        # SEC-09：注入检测（写入口在管理员侧，拦截会打断保存流——注入端过滤只影响 LLM 侧）
        filt = InputFilter.check(f"{desc}\n{body}", dept_id)
        if not filt["passed"]:
            # 2026-09-11（走查：agent 只回一句"1 个技能未注入"，运维不知道是哪个/为什么）：
            # 该分支原为静默跳过（无日志无审计），补 WARNING——技能名+命中规则+命中片段。
            logger.warning("技能注入被安全过滤器拦截 skill=%s dept=%s rule=%s matched=%r",
                           s.get("name"), dept_id, filt.get("rule"),
                           (filt.get("matched_text") or "")[:80])
            skipped.append(f"{s['name']}（内容不合规）")
            continue
        if len(desc) > _MAX_LIST_DESC_CHARS:
            desc = desc[:_MAX_LIST_DESC_CHARS] + "…"
        line = f"- {s['name']}：{desc}"
        tools = s.get("tools")
        if tools:
            line += f"（可用工具：{', '.join(tools)}）"
        if total + len(line) > max_total_chars:
            parts.append("-（其余技能因长度限制未注入）")
            break
        parts.append(line)
        total += len(line)
    if skipped:
        who = "、".join(skipped[:3]) + ("…" if len(skipped) > 3 else "")
        parts.append(f"-（{len(skipped)} 个技能未注入：{who}）")
    return "\n".join(parts)
