"""架构扫描：检测 backend/app 内 Python 模块的循环导入（模块级 import 边）。

用法: python scripts/arch_scan_cycles.py [app_dir]

- 模块级 import（文件顶层）形成的环 → REAL，打印环路径并 exit 1；
- 仅函数/方法内 import（lazy，标准规避手段）参与的环 → 警告，exit 0。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "app"


class LazyCollector(ast.NodeVisitor):
    """收集函数体内部的 import（相对导入解析由外层负责）。"""

    def __init__(self, dst: list):
        self.dst = dst

    def visit_Import(self, node):
        for a in node.names:
            self.dst.append(a.name)

    def visit_ImportFrom(self, node):
        self.dst.append((node.level, node.module or ""))


class FileCollector(ast.NodeVisitor):
    def __init__(self, src: str):
        self.src = src
        self.module_imports: list = []  # 顶层 import：str 或 (level, module)
        self.lazy_imports: list = []    # 函数体内 import：同上

    def _collect(self, node, store: list):
        for a in node.names:
            store.append(a.name)

    def visit_Import(self, node):
        self._collect(node, self.module_imports)

    def visit_ImportFrom(self, node):
        self.module_imports.append((node.level, node.module or ""))

    def visit_FunctionDef(self, node):
        for child in node.body:
            LazyCollector(self.lazy_imports).visit(child)

    visit_AsyncFunctionDef = visit_FunctionDef


def resolve(name: str, level: int, src_pkg: str) -> str | None:
    """把 import 名解析为 app.* 模块名；外部模块返回 None。"""
    if level == 0:  # 绝对导入
        return name if (name == "app" or name.startswith("app.")) else None
    # 相对导入：src_pkg 是 "app.api.chat" 这类完整模块名
    parts = src_pkg.split(".")
    # node.level=1 → 去掉最后 1 段；level=N → 去掉 N 段
    base = parts[:-level] if level <= len(parts) else []
    target = ".".join(base + ([name] if name else [])).rstrip(".")
    return target if (target == "app" or target.startswith("app.")) else None


def collect_edges(app_dir: Path) -> tuple[set, set]:
    module_edges, lazy_edges = set(), set()
    for py in sorted(app_dir.rglob("*.py")):
        if "__pycache__" in py.parts:
            continue
        rel = py.relative_to(app_dir.parent)  # app 的父目录
        src = ".".join(rel.with_suffix("").parts)  # e.g. app.api.chat
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        except SyntaxError as e:
            print(f"[SKIP] {py} 语法错误: {e}", file=sys.stderr)
            continue
        fc = FileCollector(src)
        fc.visit(tree)
        for imp in fc.module_imports:
            dst = resolve(imp[1], imp[0], src) if isinstance(imp, tuple) else resolve(imp, 0, src)
            if dst:
                module_edges.add((src, dst))
        for imp in fc.lazy_imports:
            dst = resolve(imp[1], imp[0], src) if isinstance(imp, tuple) else resolve(imp, 0, src)
            if dst:
                lazy_edges.add((src, dst))
    return module_edges, lazy_edges


def find_cycles(edges: set[tuple[str, str]]) -> list[list[str]]:
    """DFS 找所有基本环（去重，按节点序列规范化）。"""
    succ: dict[str, list[str]] = {}
    for src, dst in edges:
        succ.setdefault(src, []).append(dst)

    cycles: set[tuple[str, ...]] = set()
    for start in sorted(succ):
        stack: list[str] = [start]
        visited_on_path: set[str] = set()

        def dfs(node: str):
            for nxt in succ.get(node, []):
                if nxt == start:
                    cyc = tuple(stack)
                    if len(cyc) > 1:
                        cycles.add(cyc)
                elif nxt not in visited_on_path and len(stack) < 30:
                    visited_on_path.add(nxt)
                    stack.append(nxt)
                    dfs(nxt)
                    stack.pop()
                    visited_on_path.discard(nxt)

        visited_on_path.add(start)
        dfs(start)
    return [list(c) for c in cycles]


def main() -> int:
    app_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else APP_DIR
    module_edges, lazy_edges = collect_edges(app_dir)
    print(f"扫描 {app_dir}：模块 {len({s for s, _ in module_edges} | {d for _, d in module_edges})} 个，"
          f"模块级边 {len(module_edges)} 条，lazy 边 {len(lazy_edges)} 条")

    real_cycles = find_cycles(module_edges)
    lazy_only = [c for c in find_cycles(module_edges | lazy_edges) if c not in real_cycles]

    if real_cycles:
        print(f"\n[REAL] 模块级循环导入 {len(real_cycles)} 个（必须修复）:")
        for c in sorted(real_cycles):
            print("  -> ".join(c + [c[0]]))
        print("\n=== REAL CYCLES FOUND (exit 1) ===")
        return 1
    print("[OK] 无模块级循环导入")
    if lazy_only:
        print(f"\n[WARN] 仅 lazy（函数内）import 参与的环 {len(lazy_only)} 个（非阻塞，结构耦合提示）:")
        for c in sorted(lazy_only):
            print("  -> ".join(c + [c[0]]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
