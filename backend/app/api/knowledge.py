"""知识库 API（二期浏览 + 三期 M14 两级化：全局/团队可见性过滤 + 员工上传本团队）。

可见性规则（M14）：department_id IS NULL = 全局文档（所有团队可见）；非 NULL = 团队文档（仅本团队）。
管理端（全局上传/删除/摘要）在 admin.py 的 /admin/knowledge/**。
搜索默认 ILIKE（文档量级小足够）；zhparser GIN 留开关（KNOWLEDGE_FTS=on 时评估启用）。
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, File, Form, UploadFile
from pydantic import BaseModel
from sqlalchemy import text
from typing import Literal

from app.core.config import get_settings
from app.services.config_service import upload_limit_mb
from app.core.database import get_global_engine
from app.core.exceptions import app_error
from app.core.file_utils import read_limited
from app.api.tools_guard import require_custom_tool
from app.core.middleware import get_business_user, get_current_user


class KbDocumentStatus(BaseModel):
    status: Literal["active", "archived"]


class KbCategoryCreate(BaseModel):
    """业务端分类创建（归属三态 2026-08-24）：scope=dept 团队分类 / personal 个人分类。"""
    name: str
    parent_id: int | None = None
    scope: Literal["dept", "personal"] = "dept"


class KbCategoryRename(BaseModel):
    name: str


class KbDocRecategorize(BaseModel):
    """业务端文档改分类（仅改分类，其他字段不可动）。"""
    category_id: int | None = None


_settings = get_settings()

router = APIRouter(tags=["knowledge"])


@router.get("/knowledge/categories")
async def kb_categories(user: dict = Depends(require_custom_tool("kb"))):
    # 归属三态（2026-08-24）：全局 + 本团队 + 本人个人分类（响应带 user_id 供前端分区渲染）
    # 注意：全局分类判定必须 AND user_id IS NULL——个人分类 dept_id 也为 NULL，否则对所有团队可见（越权）
    engine = get_global_engine()
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT id, name, parent_id, sort_order, user_id, dept_id FROM kb_categories "
                     "WHERE (dept_id IS NULL AND user_id IS NULL) OR dept_id=:dept OR user_id=:uid "
                     "ORDER BY sort_order, id"),
                {"dept": user["dept_id"], "uid": user["user_id"]},
            )
        ).all()
    # 平铺返回（前端按 parent_id 递归渲染树；dept_id 供前端区分全局分类与团队分类）
    return {
        "categories": [
            {"id": r.id, "name": r.name, "parent_id": r.parent_id, "sort_order": r.sort_order,
             "user_id": r.user_id, "dept_id": r.dept_id}
            for r in rows
        ]
    }


@router.get("/knowledge/search")
async def kb_search(q: str = "", category_id: int | None = None, user: dict = Depends(require_custom_tool("kb"))):
    engine = get_global_engine()
    params: dict = {"dept": user["dept_id"], "uid": user["user_id"]}
    # D23（2026-08-10）：检索过滤 status='active'（下架文档不可见；file_search 同口径过滤）
    # 归属三态（2026-08-24）：全局/本团队文档 + 本人个人文档合并可见；
    # 全局判定必须 AND user_id IS NULL——个人文档 department_id 也为 NULL（防个人文档对所有团队可见）
    where = "((department_id IS NULL AND user_id IS NULL) OR department_id=:dept OR user_id=:uid) AND status='active'"
    if q.strip():
        where += " AND (title ILIKE :kw OR content ILIKE :kw OR summary ILIKE :kw)"
        params["kw"] = f"%{q.strip()}%"
    if category_id is not None:
        where += " AND category_id = :cid"
        params["cid"] = category_id
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(f"SELECT id, title, summary, category_id, file_type, file_size, updated_at, "
                     f"user_id, department_id FROM kb_documents WHERE {where} "
                     f"ORDER BY updated_at DESC LIMIT 100"),
                params,
            )
        ).all()
    return {
        "documents": [
            {
                "id": r.id, "title": r.title, "summary": r.summary, "category_id": r.category_id,
                "file_type": r.file_type, "file_size": r.file_size,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                "user_id": r.user_id, "department_id": r.department_id,
            }
            for r in rows
        ]
    }


async def _assert_category_manageable(cat_id: int, user: dict, conn) -> tuple[int | None, int | None]:
    """分类管理权校验（归属三态）：返回 (dept_id, user_id)；无权/不存在抛错。

    个人分类（user_id 非空）→ 仅本人；团队分类 → 本团队 dept_admin/ceo；全局分类 → 403（归 admin 接口管）。
    """
    row = (
        await conn.execute(
            text("SELECT dept_id, user_id FROM kb_categories WHERE id=:id"), {"id": cat_id}
        )
    ).first()
    if row is None:
        raise app_error("E011", "分类不存在", status_code=404)
    c_dept, c_uid = row[0], row[1]
    if c_uid is not None:
        if c_uid != user["user_id"]:
            raise app_error("E006", "无权操作该分类", status_code=403)
    elif c_dept is not None:
        if user["role"] not in ("dept_admin", "ceo") or c_dept != user["dept_id"]:
            raise app_error("E006", "无权操作该分类", status_code=403)
    else:
        raise app_error("E006", "全局分类归运维管理，无权操作", status_code=403)
    return c_dept, c_uid


@router.post("/knowledge/categories")
async def kb_category_create(req: KbCategoryCreate, user: dict = Depends(require_custom_tool("kb"))):
    """创建分类（归属三态）：scope=dept 本团队分类（仅 dept_admin/ceo）；scope=personal 本人个人分类（任何人）。

    角色校验必须在后端（require_custom_tool 只校验工具开通）。
    """
    if req.scope == "dept" and user["role"] not in ("dept_admin", "ceo"):
        raise app_error("E006", "仅团队管理员可管理团队分类", status_code=403)
    engine = get_global_engine()
    async with engine.begin() as conn:
        if req.parent_id is not None:
            p_dept, p_uid = await _assert_category_manageable(req.parent_id, user, conn)
            # parent 归属须与新建分类一致：dept 分类的父须为团队/全局分类；personal 的父须为本人个人分类
            if req.scope == "dept" and p_uid is not None:
                raise app_error("E011", "父分类不能是个人分类", status_code=400)
            if req.scope == "personal" and (p_uid != user["user_id"] or p_dept is not None):
                raise app_error("E011", "个人分类的父分类必须是您的个人分类", status_code=400)
        if req.scope == "dept":
            new_id = (
                await conn.execute(
                    text("INSERT INTO kb_categories (name, parent_id, dept_id, sort_order) "
                         "VALUES (:n, :p, :d, 0) RETURNING id"),
                    {"n": req.name.strip(), "p": req.parent_id, "d": user["dept_id"]},
                )
            ).scalar()
        else:
            new_id = (
                await conn.execute(
                    text("INSERT INTO kb_categories (name, parent_id, user_id, sort_order) "
                         "VALUES (:n, :p, :u, 0) RETURNING id"),
                    {"n": req.name.strip(), "p": req.parent_id, "u": user["user_id"]},
                )
            ).scalar()
    return {"id": new_id}


@router.put("/knowledge/categories/{cat_id}")
async def kb_category_rename(cat_id: int, req: KbCategoryRename, user: dict = Depends(require_custom_tool("kb"))):
    """分类改名（按归属校验：团队→本团队 dept_admin、个人→本人、全局→403）。"""
    if not req.name.strip():
        raise app_error("E011", "分类名不能为空", status_code=400)
    engine = get_global_engine()
    async with engine.begin() as conn:
        await _assert_category_manageable(cat_id, user, conn)
        await conn.execute(
            text("UPDATE kb_categories SET name=:n WHERE id=:id"),
            {"n": req.name.strip()[:100], "id": cat_id},
        )
    # 2026-09-10 同类排查：分类名进检索结果（file_search 命中块带 category）——改了必须清缓存，
    # 否则 5 分钟内仍返回旧分类名（同文件的上传/摘要路径早已清，这两条是漏网）
    from app.services.kb_service import clear_kb_match_cache

    await clear_kb_match_cache()
    return {"ok": True}


@router.delete("/knowledge/categories/{cat_id}")
async def kb_category_delete(cat_id: int, user: dict = Depends(require_custom_tool("kb"))):
    """删除分类（按归属校验）：有子分类拒绝；其下文档 category_id 置 NULL（保留文档）。"""
    engine = get_global_engine()
    async with engine.begin() as conn:
        await _assert_category_manageable(cat_id, user, conn)
        child = (
            await conn.execute(
                text("SELECT 1 FROM kb_categories WHERE parent_id=:id LIMIT 1"), {"id": cat_id}
            )
        ).first()
        if child:
            raise app_error("E011", "该分类存在子分类，请先删除子分类", status_code=400)
        await conn.execute(text("UPDATE kb_documents SET category_id=NULL WHERE category_id=:id"), {"id": cat_id})
        await conn.execute(text("DELETE FROM kb_categories WHERE id=:id"), {"id": cat_id})
    from app.services.kb_service import clear_kb_match_cache   # 同上：删分类后缓存里仍有旧分类名

    await clear_kb_match_cache()
    return {"ok": True}


@router.put("/knowledge/documents/{doc_id}")
async def kb_document_recategorize(
    doc_id: int, req: KbDocRecategorize, user: dict = Depends(require_custom_tool("kb"))
):
    """文档改分类（仅改分类）：团队文档→新分类须全局或本团队分类（本团队 dept_admin/ceo 操作）；
    个人文档→须本人个人分类（本人操作）；全局文档→403（归 admin 接口管）。"""
    engine = get_global_engine()
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text("SELECT department_id, user_id FROM kb_documents WHERE id=:id"), {"id": doc_id}
            )
        ).first()
        if row is None:
            raise app_error("E011", "文档不存在", status_code=404)
        d_dept, d_uid = row[0], row[1]
        if d_uid is not None:
            # 个人文档：仅本人可改分类；新分类须本人个人分类或 NULL
            if d_uid != user["user_id"]:
                raise app_error("E006", "无权修改该文档", status_code=403)
            if req.category_id is not None:
                c = (
                    await conn.execute(
                        text("SELECT user_id FROM kb_categories WHERE id=:id"), {"id": req.category_id}
                    )
                ).first()
                if c is None or c[0] != user["user_id"]:
                    raise app_error("E011", "个人文档只能挂到您的个人分类下", status_code=400)
        elif d_dept is not None:
            # 团队文档：本团队 dept_admin/ceo 可改；新分类须全局或本团队分类或 NULL
            if user["role"] not in ("dept_admin", "ceo") or d_dept != user["dept_id"]:
                raise app_error("E006", "无权修改该文档", status_code=403)
            if req.category_id is not None:
                c = (
                    await conn.execute(
                        text("SELECT dept_id, user_id FROM kb_categories WHERE id=:id"), {"id": req.category_id}
                    )
                ).first()
                if c is None or c[1] is not None or (c[0] is not None and c[0] != user["dept_id"]):
                    raise app_error("E011", "团队文档只能挂到全局或本团队分类下", status_code=400)
        else:
            # 全局文档：业务端无权（admin 接口管理）
            raise app_error("E006", "全局文档归运维管理，无权修改", status_code=403)
        await conn.execute(
            text("UPDATE kb_documents SET category_id=:cid, updated_at=NOW() WHERE id=:id"),
            {"cid": req.category_id, "id": doc_id},
        )
    from app.services.kb_service import clear_kb_match_cache   # 改挂分类：缓存里的 category 字段要跟着失效

    await clear_kb_match_cache()
    return {"ok": True}


@router.get("/knowledge/documents/{doc_id}")
async def kb_document(doc_id: int, user: dict = Depends(require_custom_tool("kb"))):
    engine = get_global_engine()
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT id, title, content, summary, category_id, updated_at, department_id, status, user_id "
                     "FROM kb_documents WHERE id=:id"),
                {"id": doc_id},
            )
        ).first()
    if row is None or row.status != "active":  # D23：下架文档详情不可见
        raise app_error("E011", "文档不存在", status_code=404)
    # 可见性校验（M14 修复越权 + 归属三态 2026-08-24）：个人文档仅本人可见（他人一律 404 不暴露存在）
    if row.user_id is not None and row.user_id != user["user_id"]:
        raise app_error("E011", "文档不存在", status_code=404)
    if row.department_id is not None and row.department_id != user["dept_id"]:
        raise app_error("E011", "文档不存在", status_code=404)
    return {
        "id": row.id, "title": row.title, "content": row.content, "summary": row.summary,
        "category_id": row.category_id,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@router.post("/knowledge/documents")
async def kb_document_upload(
    file: UploadFile = File(...),
    category_id: int | None = Form(None),
    title: str = Form(""),
    scope: Literal["dept", "personal"] = Form("dept"),
    user: dict = Depends(require_custom_tool("kb")),
):
    """上传知识文档（归属三态 2026-08-24 权限收紧）。

    scope=dept：本团队文档——仅 dept_admin/ceo（employee 403 收权，原员工可传已废弃）；
    scope=personal：个人文档——任何人可传（department_id 恒 NULL，仅本人可见）。
    """
    from app.services.kb_service import upload_kb_document

    # 分类归属校验：dept 须全局/本团队分类；personal 须本人个人分类
    if category_id is not None:
        engine = get_global_engine()
        async with engine.connect() as conn:
            cat = (
                await conn.execute(
                    text("SELECT dept_id, user_id FROM kb_categories WHERE id=:id"), {"id": category_id}
                )
            ).first()
        if scope == "dept":
            if cat is None or (cat[0] is not None and cat[0] != user["dept_id"]) or cat[1] is not None:
                raise app_error("E011", "分类不存在或不属于本团队", status_code=400)
        else:
            if cat is None or cat[1] != user["user_id"]:
                raise app_error("E011", "分类不存在或不是您的个人分类", status_code=400)

    if scope == "dept" and user["role"] not in ("dept_admin", "ceo"):
        # 角色校验必须在后端（require_custom_tool 只校验工具开通，不校验角色）
        raise app_error("E006", "仅团队管理员可上传团队文档", status_code=403)

    fname = file.filename or "doc"
    try:  # M3：分块读取限流（KB 上限统一走配置，原无限制）
        content = await read_limited(file, await upload_limit_mb("kb_mb") * 1024 * 1024)
    except ValueError as e:
        raise app_error("E003", str(e), status_code=400)
    # E-13：记录上传人（员工自删/下架能力的前提；原未传 uploaded_by → NULL → 员工无法撤回）
    if scope == "dept":
        doc_id = await upload_kb_document(fname, content, title, category_id,
                                          department_id=user["dept_id"], uploaded_by=user["user_id"])
    else:
        doc_id = await upload_kb_document(fname, content, title, category_id,
                                          department_id=None, uploaded_by=user["user_id"],
                                          user_id=user["user_id"])
    return {"id": doc_id, "title": title.strip() or fname}


@router.delete("/knowledge/documents/{doc_id}")
async def kb_document_delete(doc_id: int, user: dict = Depends(get_current_user)):
    """删除知识文档（四期重构 + KB-REDESIGN + 归属三态 2026-08-24 权限收紧）。

    admin 任意删除；团队管理员（4.1：含 CEO 兼任本团队管理员）可删本团队文档 + 本人个人文档；
    employee 仅可删本人个人文档（原"删本人上传的本团队文档"权限已收掉——员工上传团队文档即已废弃）；
    级联删盘（M15）。
    """
    engine = get_global_engine()
    file_path: str | None = None
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text("SELECT id, department_id, file_path, uploaded_by, user_id FROM kb_documents WHERE id=:id"),
                {"id": doc_id},
            )
        ).first()
        if row is None:
            raise app_error("E011", "文档不存在", status_code=404)
        if user["role"] == "admin":
            pass  # 运维管理任意删除（个人文档只读+删）
        elif row.user_id is not None:
            # 个人文档：仅本人可删
            if row.user_id != user["user_id"]:
                raise app_error("E006", "无权删除该文档", status_code=403)
        elif user["role"] in ("dept_admin", "ceo"):
            # 团队管理员仅限本团队文档（全局文档归运维管理）
            if row.department_id is None or row.department_id != user["dept_id"]:
                raise app_error("E006", "无权删除该文档", status_code=403)
        else:
            # employee：仅可删本人个人文档（团队/全局文档无删除权）
            raise app_error("E006", "无权删除该文档", status_code=403)
        file_path = row.file_path  # 事务提交后再删盘（chunks 靠 ON DELETE CASCADE 自动清理）
        await conn.execute(text("DELETE FROM kb_documents WHERE id=:id"), {"id": doc_id})
    # KB-REDESIGN：物理文件路径入库后删除级联删盘（路径来自 DB 非用户输入，无穿越风险；to_thread 防阻塞）
    if file_path:
        from pathlib import Path

        await asyncio.to_thread(Path(file_path).unlink, missing_ok=True)
    from app.services.kb_service import clear_kb_match_cache

    await clear_kb_match_cache()  # G1：删除后清 kb_match 缓存
    return {"ok": True}


@router.put("/knowledge/documents/{doc_id}/status")
async def kb_document_status(doc_id: int, req: KbDocumentStatus, user: dict = Depends(get_current_user)):
    """D23（2026-08-10）：文档下架/恢复（激活 kb_documents.status 死字段）。

    下架后：检索（/knowledge/search、file_search）、详情均不可见（status='active' 过滤）。
    权限：admin 任意；dept_admin/ceo 本团队文档；employee 无权。
    """
    engine = get_global_engine()
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text("SELECT title, department_id FROM kb_documents WHERE id=:id"), {"id": doc_id}
            )
        ).first()
        if row is None:
            raise app_error("E011", "文档不存在", status_code=404)
        if user["role"] in ("dept_admin", "ceo") and (
            row.department_id is None or row.department_id != user["dept_id"]
        ):
            raise app_error("E006", "无权操作该文档", status_code=403)
        if user["role"] == "employee":
            raise app_error("E006", "仅管理员可下架文档", status_code=403)
        await conn.execute(
            text("UPDATE kb_documents SET status=:s, updated_at=NOW() WHERE id=:id"),
            {"s": req.status, "id": doc_id},
        )
    # 2026-09-10（缓存核查）：下架/恢复必须清检索缓存——否则下架后 10 分钟内仍能搜到文档内容
    from app.services.kb_service import clear_kb_match_cache

    await clear_kb_match_cache()
    return {"ok": True, "status": req.status, "title": row.title}
