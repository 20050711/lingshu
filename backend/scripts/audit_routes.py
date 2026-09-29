"""路由/API 清单重导出（2026-08-19，架构 P1）：import app.main 枚举路由 + openapi。

背景：/docs /openapi.json 已按 F21 关闭（docs_url=None）——但 app.openapi() 方法仍可调用；
旧架构报告/routes_audit.txt 与 openapi.json 为 08-07 基线（含已下线 /chat/confirm、缺
/chat/answer|plan-approve|interrupt|stop|status 与视频新路由）——本脚本重新生成。

用法（在 backend 目录）：
  source scripts/env_aip.sh && python scripts/audit_routes.py
输出：架构报告/routes_audit.txt + 架构报告/openapi.json（覆盖旧基线）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend 目录（脚本在 scripts/ 下）

from app.main import app

REPORT_DIR = Path(__file__).resolve().parent.parent.parent / "架构报告"

# app.routes 含 _IncludedRouter（FastAPI 惰性挂载不展开）——从 openapi schema 生成
# 路由清单（与 HTTP 实际暴露一致；docs_url=None 不影响 app.openapi()）
schema = app.openapi()
routes = sorted(
    f"{m.upper():8s} {path}"
    for path, ops in schema.get("paths", {}).items()
    for m in ops
)
out = "\n".join(routes) + "\n"
(REPORT_DIR / "routes_audit.txt").write_text(out, encoding="utf-8")

(REPORT_DIR / "openapi.json").write_text(
    json.dumps(schema, ensure_ascii=False, indent=1), encoding="utf-8")

print(f"路由 {len(routes)} 条（含方法）→ 架构报告/routes_audit.txt")
print(f"openapi paths {len(schema.get('paths', {}))} 个 → 架构报告/openapi.json")
