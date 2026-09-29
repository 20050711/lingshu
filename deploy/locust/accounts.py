"""locust 账号池（从环境变量构造，**口令不入库**）。

用法：
    export LOCUST_PASSWORD='<压测账号口令>'      # 必填（与 seed_press_dept.py 的口令一致）
    export LOCUST_DEPT=press                    # 可选，默认 press（压测团队）
    export LOCUST_COUNT=15                      # 可选，默认 15（press01..press15）

账号来源：`backend/scripts/seed_press_dept.py` 幂等创建（团队 press + press01..pressN）。
"""
import itertools
import os

_DEPT = os.environ.get("LOCUST_DEPT", "press")
_PASSWORD = os.environ.get("LOCUST_PASSWORD", "")
_COUNT = int(os.environ.get("LOCUST_COUNT", "15"))

if not _PASSWORD:
    raise RuntimeError("请先 export LOCUST_PASSWORD=<压测账号口令>（账号由 scripts/seed_press_dept.py 创建）")

# (department_id, username, password, 功能面)
ACCOUNTS = [
    (_DEPT, f"press{i:02d}", _PASSWORD, "press") for i in range(1, _COUNT + 1)
]

_pool = itertools.cycle(ACCOUNTS)


def pick_account() -> tuple[str, str, str, str]:
    """返回 (dept, username, password, 功能面)，账号循环取用。"""
    return next(_pool)
