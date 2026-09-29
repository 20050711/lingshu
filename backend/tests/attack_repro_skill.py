"""红队二次修复复测（批 2：skill 沙箱化；2026-08-21 内置技能下线——argv 白名单段删除）——零 LLM。

覆盖：
- F2 bwrap 隔离：skill 进程读宿主文件失败（白名单外）；回环平台取数可用
- F2 开关回退：bwrap=False 时回退路径不启用隔离

用法：cd backend && conda run -n aip python -u tests/attack_repro_skill.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


async def test_bwrap_isolation() -> None:
    """F2：bwrap 下 skill 进程读白名单外宿主路径失败（不依赖 argv 白名单的第二道防线）。"""
    from app.services import sandbox_exec

    r = await sandbox_exec.run(
        lang="bash", code="cat /opt/.bashrc 2>&1 || true", cwd="/tmp",
        bwrap=True, bwrap_read_dirs=[], bwrap_net="loopback",
    )
    ok = "No such file" in str(r.get("stdout", "")) or "Permission denied" in str(r.get("stdout", "")) or "not found" in str(r.get("stdout", ""))
    check("bwrap 读宿主家目录失败", ok, f"stdout={r.get('stdout', '')[:80]}")

    r = await sandbox_exec.run(
        lang="bash", code="ls /data/sandbox 2>&1 || true", cwd="/tmp",
        bwrap=True, bwrap_read_dirs=[], bwrap_net="loopback",
    )
    check("bwrap 读白名单外 /data 失败", "No such file" in str(r.get("stdout", "")) or "Permission denied" in str(r.get("stdout", "")),
          f"stdout={r.get('stdout', '')[:80]}")


async def test_loopback_works() -> None:
    """F2：loopback 网络可连通回环（platform_tool 依赖）；外网不可达。"""
    from app.services import sandbox_exec

    # lo flags 含 UP（Linux lo 的 state 字段为 UNKNOWN 属正常现象）
    r2 = await sandbox_exec.run(
        lang="bash", code="/usr/sbin/ip link show lo 2>&1 | grep -o '<LOOPBACK,UP' || echo 'lo-down'", cwd="/tmp",
        bwrap=True, bwrap_read_dirs=[], bwrap_net="loopback",
    )
    check("bwrap loopback: lo UP", "<LOOPBACK,UP" in str(r2.get("stdout", "")), f"out={r2.get('stdout','')[:80]}")
    # 语义化验证：connect 未监听端口 → ConnectionRefusedError = 回环网络栈可用
    r3 = await sandbox_exec.run(
        lang="python", code=(
            "import socket\n"
            "try:\n"
            "    socket.create_connection(('127.0.0.1', 65533), timeout=2); print('connected')\n"
            "except ConnectionRefusedError: print('refused=lo OK')\n"
            "except OSError as e: print('OSError:', e)\n"), cwd="/tmp",
        bwrap=True, bwrap_read_dirs=[], bwrap_net="loopback",
    )
    check("bwrap loopback: 回环可连通", "refused=lo OK" in str(r3.get("stdout", "")), f"out={r3.get('stdout','')[:80]}")


async def test_bwrap_switch_off() -> None:
    """F2：skill_bwrap_enabled=False 时 bwrap 分支不启用（回退路径仍可执行）。"""
    from app.services import sandbox_exec

    # 直接测 sandbox_exec：bwrap=False 时宿主文件可读（回退语义；/etc/hostname 在 WSL 为空文件，改用 /etc/passwd）
    r = await sandbox_exec.run(
        lang="bash", code="head -1 /etc/passwd", cwd="/tmp",
        bwrap=False,
    )
    check("bwrap=False 回退路径可读宿主（开关语义）", "root:" in str(r.get("stdout", "")), f"out={r.get('stdout','')[:60]}")


def main() -> None:
    print("== F2 bwrap 隔离（第二道防线） ==")
    asyncio.run(test_bwrap_isolation())
    asyncio.run(test_loopback_works())
    print("== F2 开关回退语义 ==")
    asyncio.run(test_bwrap_switch_off())
    print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
