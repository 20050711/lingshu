"""audit_legacy_tests.py — 旧测试脚本过时性审查（PLAN-v5 批次 B0 自动化版本）。

不依赖旧脚本运行结果，而是静态扫描其断言契约是否与当前代码一致：
1. 旧工具名引用（db_schema/xlsx_parse/doc_parse/memory_add/memory_list/memory_delete）
   ——判断是"断言已移除"（合法）还是"仍在使用"（过时）
2. 接口路径字面量 → 与 main.py 路由注册比对
3. 登录参数形态（三要素 vs 旧两要素）
4. 每脚本输出契约审查结论

用法: conda run -n aip python tests/audit_legacy_tests.py
"""
import os
import re
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).parent
APP_DIR = Path(__file__).parent.parent / "app"

OLD_TOOL_NAMES = ["db_schema", "xlsx_parse", "doc_parse", "memory_add", "memory_list", "memory_delete"]
OLD_TERMS = ["skill_configs", "skills/yaml", "ceo_memory", "trigger_conditions"]


def scan_old_refs() -> list[tuple[str, str, int, str]]:
    """返回 (脚本, 术语, 行号, 行内容)。"""
    hits = []
    for f in sorted(TESTS_DIR.glob("*.py")):
        if f.name.startswith("abuse_") or f.name == "audit_legacy_tests.py":
            continue
        for lineno, line in enumerate(f.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            for term in OLD_TOOL_NAMES + OLD_TERMS:
                if term in line:
                    hits.append((f.name, term, lineno, line.strip()[:90]))
    return hits


def scan_api_paths() -> list[tuple[str, str, int, str]]:
    hits = []
    for f in sorted(TESTS_DIR.glob("*.py")):
        if f.name.startswith("abuse_"):
            continue
        for lineno, line in enumerate(f.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            for m in re.finditer(r'"/[a-z][a-z0-9_/{}.-]*"', line):
                p = m.group(0)
                if p.startswith(('"/api', '"/auth', '"/chat', '"/admin', '"/skills', '"/knowledge',
                                  '"/tools', '"/upload', '"/feedback', '"/dashboard', '"/mcp',
                                  '"/internal', '"/outputs', '"/sync', '"/ceo')):
                    hits.append((f.name, p, lineno, line.strip()[:90]))
    return hits


def registered_routes() -> set[str]:
    """从 main.py 与 api 目录提取注册的路径（简化匹配，仅报告差异）。"""
    routes = set()
    main = (APP_DIR / "main.py").read_text(encoding="utf-8")
    # prefix=/api/v1 + include_router 的各 router 内部 @router.<method>("<path>")
    for f in sorted((APP_DIR / "api").glob("*.py")):
        for m in re.finditer(r'@router\.(?:get|post|put|delete)\(["\']([^"\']+)', f.read_text(encoding="utf-8")):
            routes.add(m.group(1))
    for m in re.finditer(r'@app\.(?:get|post)\(["\']([^"\']+)', main):
        routes.add(m.group(1))
    return routes


def main() -> int:
    print("== 1. 旧工具名/旧术语引用 ==")
    old_refs = scan_old_refs()
    if not old_refs:
        print("  无旧术语引用")
    for f, term, ln, content in old_refs:
        print(f"  {f}:{ln} [{term}] {content}")

    print("\n== 2. 接口路径字面量（抽查是否与当前注册路由一致）==")
    routes = registered_routes()
    api_hits = scan_api_paths()
    for f, p, ln, content in api_hits:
        # 路径中含 { 的动态段：去掉参数段后与路由集合匹配
        p2 = p.strip('"')
        # 判断该路径前缀是否存在（含参数化路由）
        p3 = re.sub(r"\{[^}]+\}", "X", p2)
        match = any(
            re.fullmatch(re.sub(r"\{[^}]+\}", "X", r), p3)
            for r in routes if r.startswith(p3[: max(3, p3.find("/", 1))] if "/" in p3[1:] else p3)
        ) or p3 in routes
        tag = "✓" if match or p2.startswith(("/api/v1", "/outputs", "/internal")) else "？"
        print(f"  {tag} {f}:{ln} {p}  ← {content[:60]}")
        if tag == "？":
            print(f"       （路由表无匹配，需人工核对：{p}）")

    print("\n== 3. 登录参数形态（三要素 vs 旧两要素）==")
    for f in sorted(TESTS_DIR.glob("*.py")):
        if f.name.startswith("abuse_"):
            continue
        txt = f.read_text(encoding="utf-8", errors="ignore")
        if "auth/login" in txt:
            if "department_id" in txt:
                print(f"  ✓ {f.name}: 含 department_id（三要素）")
            else:
                print(f"  ⚠ {f.name}: auth/login 无 department_id（旧两要素，过时）")

    print("\n== 4. 结论 ==")
    stale = [x for x in old_refs if "assert" not in x[3] and "get_tool(" not in x[3] and "#" not in x[3] and "注释" not in x[3]]
    print(f"  旧术语引用 {len(old_refs)} 处；疑似过时断言（非注释/非断言移除）{len(stale)} 处：")
    for x in stale:
        print(f"    {x[0]}:{x[2]}")
    print("=== 审查完成 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
