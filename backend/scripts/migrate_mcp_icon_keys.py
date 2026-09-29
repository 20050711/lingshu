"""迁移：mcp_tools.icon 由 emoji 改为语义图标名（2026-09-18）。幂等可重跑。

背景：图标字段原先是自由文本，`MCP_DEFAULTS` 与运维后台都往里写 emoji——前端只能靠一张
emoji→组件的对照表兜底（后端换个 emoji 就又漏到界面上）。改为**语义名**后，后端只声明
"这是什么类别"，画成什么图标由前端 components/Icon.tsx 决定。

本脚本做四件事：
1. 列宽 VARCHAR(10) → VARCHAR(32)（keys 最长 9，留余量；PG 加宽是元数据操作，不重写表）
2. `mcp_tools.icon`：已知 emoji 逐一映射成语义名
3. `skill_configs.icon`（2026-09-22 补）：同样换成语义名——这张表四期重构后**已是死表**
   （`scripts/seed.py` 注明"不再写入"），但历史行里还留着 15 个 emoji；前端 `iconFromKey()` 收到
   emoji 只能落回拼图兜底图标，等于每张技能卡都没图标。按**技能名**精确映射（同一个 emoji 出现在
   不同技能上时语义不同，按 emoji 一刀切会张冠李戴）。
4. 兜底：任何**不在 ICON_KEYS 内**的残留值（包括日后误填的字符）统一收敛为默认 `tool`
   —— 宁可用默认图标，也不让 emoji 再漏到界面上

用法: cd backend && source scripts/env_aip.sh && python scripts/migrate_mcp_icon_keys.py
"""
import asyncio
import sys

from sqlalchemy import text

sys.path.insert(0, ".")

from app.agent.tools import DEFAULT_ICON, ICON_KEYS  # noqa: E402
from app.core.database import get_global_engine  # noqa: E402

# 已知 emoji → 语义名（与 MCP_DEFAULTS 改前取值一致）
EMOJI_MAP = {
    "📕": "book", "🗄️": "database", "📁": "folder", "🔗": "link",
    "🌐": "globe", "📧": "mail", "📅": "calendar",
}

# skill_configs：**按技能名**映射（语义为准，别看 emoji——🔄 在两条技能上分别表示"流程"和"审查"）
SKILL_NAME_MAP = {
    "审批流程": "refresh", "看板/图表": "chart", "代码执行": "code", "合同审查": "check",
    "文档解析": "file", "图片生成": "palette", "图片识别": "image", "知识检索": "search",
    "记忆": "brain", "多表查询": "database", "PPT 制作": "screen", "报告生成": "file",
    "表格分析": "chart", "联网搜索": "globe", "文案撰写": "note",
}
# 技能表里见过的 emoji → 语义名（名字没在上表里的行按它兜；再兜不住的才收敛为默认图标）
SKILL_EMOJI_MAP = {
    "🔄": "refresh", "📊": "chart", "💻": "code", "📝": "note", "🎨": "palette",
    "🖼️": "image", "🔍": "search", "🧠": "brain", "🔗": "link", "📽️": "screen",
    "📄": "file", "📈": "chart", "🌐": "globe", "✉️": "mail",
}


async def main() -> None:
    engine = get_global_engine()
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE mcp_tools ALTER COLUMN icon TYPE VARCHAR(32)"))

        mapped = 0
        for emoji, key in EMOJI_MAP.items():
            r = await conn.execute(
                text("UPDATE mcp_tools SET icon=:k WHERE icon=:e"), {"k": key, "e": emoji})
            mapped += r.rowcount or 0

        # 兜底：仍不在白名单内的（含历史遗留字符、空串）→ 默认图标
        r = await conn.execute(
            text("UPDATE mcp_tools SET icon=:d WHERE icon IS NULL OR icon = '' "
                 "OR icon <> ALL(:keys)"),
            {"d": DEFAULT_ICON, "keys": list(ICON_KEYS)})
        fallback = r.rowcount or 0

        # skill_configs（2026-09-22 补）：按技能名精确映射 → 再按 emoji 兜 → 最后收敛
        sk_mapped = 0
        for name, key in SKILL_NAME_MAP.items():
            r = await conn.execute(
                text("UPDATE skill_configs SET icon=:k WHERE name=:n AND icon <> :k"),
                {"k": key, "n": name})
            sk_mapped += r.rowcount or 0
        for emoji, key in SKILL_EMOJI_MAP.items():
            r = await conn.execute(
                text("UPDATE skill_configs SET icon=:k WHERE icon=:e"), {"k": key, "e": emoji})
            sk_mapped += r.rowcount or 0
        r = await conn.execute(
            text("UPDATE skill_configs SET icon=:d WHERE icon IS NULL OR icon = '' "
                 "OR icon <> ALL(:keys)"),
            {"d": DEFAULT_ICON, "keys": list(ICON_KEYS)})
        sk_mapped += r.rowcount or 0

        rows = (await conn.execute(text("SELECT id, icon FROM mcp_tools ORDER BY id"))).fetchall()
        sk_rows = (await conn.execute(
            text("SELECT name, icon FROM skill_configs ORDER BY name"))).fetchall()
    await engine.dispose()

    print(f"✓ 列宽已放宽至 VARCHAR(32)；mcp_tools 映射 {mapped} 行、兜底 {fallback} 行；"
          f"skill_configs 改写 {sk_mapped} 行")
    for rid, icon in rows:
        print(f"    mcp_tools  {rid:<12} → {icon}")
    for name, icon in sk_rows:
        print(f"    skill      {name:<12} → {icon}")


if __name__ == "__main__":
    asyncio.run(main())
