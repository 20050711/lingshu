"""清空全部历史数据（含团队业务数据与 Agent 产出物）。

清理范围（四期重构扩展）：
- 全局库：业务数据表全清（会话/消息/文件/图表/记忆/反馈/同步日志/审计/知识库文档/技能勾选/视频简历批次）
- 团队库（全部已注册团队，动态枚举）：DROP 全部业务表（2026-09-17 数据查询线下线：
  不再重建 _schema_meta 空底表——导入管线已不存在，该表无人读）
- tardis_ceo_db：同团队库处理
- 物理文件：/data/outputs/*、/data/uploads/*、/data/sandbox/*（沙箱脚本）
保留：users / departments / system_config / mcp_tools / skill_configs(死表) / skill_files(团队技能) /
       memory_thresholds / kb_categories（基础设施与配置；知识库文档随后由 seed 恢复示例）

用法: cd backend && source scripts/env_aip.sh && python -m scripts.reset_data
"""
import asyncio
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import get_dept_engine, get_global_engine
from app.services.dept_service import list_depts

_settings = get_settings()

# 全局库：业务数据表（users/departments/system_config/mcp_tools/skill_configs/skill_files/
# memory_thresholds/kb_categories 保留）
CLEAN_TABLES = [
    "chat_messages", "chat_files", "chart_outputs", "sessions",
    "security_events", "feedback", "audit_log",
    "user_memory", "department_memory", "user_skill_prefs",
    "kb_documents", "resume_batches", "resume_items",
]


async def _reset_dept_db(dept_id: str) -> None:
    """团队库：DROP 全部表（数据查询线下线后已无写入方，留空库即可）。"""
    engine = get_dept_engine(dept_id)
    async with engine.begin() as conn:
        tables = (
            await conn.execute(
                text("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
            )
        ).all()
        for (t,) in tables:
            await conn.execute(text(f'DROP TABLE IF EXISTS "{t}" CASCADE'))
    print(f"已重置(团队库 {dept_id}): 业务表全删")


async def main() -> None:
    engine = get_global_engine()
    async with engine.begin() as conn:
        for t in CLEAN_TABLES:
            await conn.execute(text(f"DELETE FROM {t}"))
            print(f"已清空(全局): {t}")

    # 全部已注册团队（含 market/sales/…；ceo 走 tardis_ceo_db 单独处理）
    depts = [d["dept_id"] for d in await list_depts() if d["dept_id"] not in ("ceo", "dept_root")]
    for dept in depts:
        await _reset_dept_db(dept)

    # tardis_ceo_db：业务表全删（2026-09-17：CEO 同步链已下线）
    await _reset_dept_db("ceo")

    # 物理文件清理（agent 生成的图片/文档 + 上传文件 + 沙箱脚本）
    for key in ("output", "upload", "sandbox"):
        base = Path(getattr(_settings, f"{key}_dir"))
        if base.exists():
            for child in base.iterdir():
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
        print(f"已清理物理目录: {base}")

    print("=== reset done（历史数据已清空；团队库/tardis_ceo_db 业务表已删空；")
    print("    保留用户/团队/配置/团队技能/知识库分类；示例知识文档可用 scripts/seed 恢复）===")


if __name__ == "__main__":
    asyncio.run(main())
