"""共用沙盒执行器（4.1 抽取）：run_script 与 skill 工具共用的进程隔离执行。

隔离措施（Linux 最小可靠沙箱，WSL 单用户无法降权）：
- 独立工作目录 + 干净 env（无 API key 等敏感变量；skill 场景仅追加内部短时令牌）
- preexec_fn 资源限制：RLIMIT_AS / RLIMIT_CPU / RLIMIT_NPROC（防 fork 炸弹）
- start_new_session + 超时 killpg(SIGKILL)：杀整个进程组（含子进程），asyncio 原生子进程不阻塞事件循环
- 输出截断（截断须提示，HANDOVER 踩坑 26）
- 解释器参数化：run_script 用 python3 -S + aip purelib（行为不变）；skill 用 skillenv python（完整 site-packages）
"""
from __future__ import annotations

import asyncio
import os
import resource
import signal
import time
from pathlib import Path

from app.core.config import get_settings

_settings = get_settings()

LANGS = {"python", "bash", "node"}

MAX_OUTPUT_CHARS = 200_000


def _set_rlimits(timeout_s: float, memory_mb: int | None, nproc_limit: int, fsize_mb: int | None) -> None:
    """子进程 preexec_fn：资源限制（地址空间/CPU 秒/进程数/单文件大小）。

    4.1：
    - NPROC 按语言参数化——WSL2 的 RLIMIT_NPROC 计数异常膨胀
      （node 启动 libuv 线程池，实测 256 仍崩溃、1024 才通过），node 放宽到 1024
    - memory_mb=None 时不设 RLIMIT_AS（保留 CPU/NPROC/超时 killpg）——
      供 framework skill 使用（observable build 的 undici wasm 解析器在
      RLIMIT_AS 下无法分配内存；skill 脚本为平台内置代码，非用户任意代码）
    - fsize_mb（D5，2026-08-10）：单文件写入上限（RLIMIT_FSIZE，超限 SIGXFSZ 杀进程）——
      run_script 传配置 sandbox_fsize_mb（512MB）；skill 场景不设（输出大文件）
    """
    if memory_mb is not None:
        mb = memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mb, mb))
    resource.setrlimit(resource.RLIMIT_CPU, (int(timeout_s), int(timeout_s) + 5))
    resource.setrlimit(resource.RLIMIT_NPROC, (nproc_limit, nproc_limit))
    if fsize_mb is not None:
        fsize = fsize_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))


def _truncate(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    if len(text) > MAX_OUTPUT_CHARS:
        return text[:MAX_OUTPUT_CHARS] + f"\n...（输出过长，已截断前 {MAX_OUTPUT_CHARS} 字符）"
    return text


async def _sandbox_net_enabled() -> bool:
    """沙盒联网开关：system_config sandbox_net_enabled（admin 配置页在线改，60s 缓存生效）→ env 兜底。

    SEC-02：默认断网（--unshare-net）；仅调试/明确需要联网的场景开 True。
    """
    try:
        from app.services.config_service import _load_all
        kv = await _load_all()
        raw = kv.get("sandbox_net_enabled")
        if raw is not None:
            return str(raw).strip().lower() in ("1", "true", "yes", "on")
    except Exception:
        pass
    return _settings.sandbox_net_enabled


def _bwrap_wrap(argv: list[str], cwd: str, work_dirs: list[str], read_dirs: list[str],
                ro_files: list[str] | None = None, net: str = "off") -> list[str]:
    """bwrap 隔离包装（沙盒 v3，2026-08-07，OS 级命名空间隔离）：

    - 文件系统：系统路径只读；解释器/依赖目录（conda/nvm）只读；白名单读路径（本会话上传/产出）只读；
      仅工作目录可写 → 堵死"读他人文件/写任意路径"
    - 命名空间：--unshare-user（WSL 单用户无需 root 即可挂载）+ --unshare-pid（进程隔离）+ --die-with-parent
    - 网络三态（F2，2026-08-18）：off=--unshare-net 断网（SEC-02 默认，run_script）；loopback=断网 +
      bash 包装起 lo（skill 经回环调平台内部接口 platform_tool 取数，flatpak 同款模式）；
      on=保留网络（沙盒代码可外连，仅配置显式开启场景）
    - ro_files（F2）：文件级只读挂载——officecli 二进制在 /opt/bin（A9 整体移出挂载面），
      文件级挂回只绑二进制本身，避免同目录 cloudflared 等可见
    - usrmerge 注意：/bin /lib 等是符号链接，须显式挂载路径本身
    """
    cmd = ["bwrap", "--unshare-user", "--unshare-pid", "--die-with-parent"]
    if net != "on":
        cmd.append("--unshare-net")
    for d in ("/usr", "/bin", "/lib", "/lib64", "/etc", "/sbin"):
        cmd += ["--ro-bind", d, d]
    # DNS：WSL 的 /etc/resolv.conf 是符号链接 → /mnt/wsl/resolv.conf（/mnt 不挂则域名解析失败）
    if Path("/mnt/wsl").exists():
        cmd += ["--ro-bind", "/mnt/wsl", "/mnt/wsl"]
    elif Path("/run").exists():
        cmd += ["--ro-bind", "/run", "/run"]
    # F-01 收窄（2026-08-19，红队四次）：整目录 miniconda3/.nvm → 文件级最小集。
    # 盘点依据：python3.12/node 动态依赖全在 /lib /lib64（已整目录挂）；numpy/pandas 扩展为
    # manylinux 自包含（依赖系统 glibc）；仅需解释器二进制 + stdlib/site-packages（含
    # lib-dynload 扩展）+ libpython + node 二进制/node_modules。整目录移除后家目录不再可读。
    # 遗留：A9 曾移除的 /opt/.local 保持移除；盘点遗漏经 config.sandbox_extra_ro 逃生门追加
    # G2（架构 P1，2026-08-19）：路径一律从 config 派生（aip_python/skillenv_python/node_path），
    # 不硬编码——环境迁移只改 env，不碰代码
    _aip_root = Path(_settings.aip_python).parent.parent          # .../envs/aip
    _skill_root = Path(_settings.skillenv_python).parent.parent   # .../envs/skillenv
    _node_root = Path(_settings.node_path).parent.parent          # .../v20.20.2
    _file_ro_mounts = (
        _settings.aip_python,
        f"{_aip_root}/lib/libpython3.12.so.1.0",
        f"{_aip_root}/lib/libpython3.12.so",
        f"{_aip_root}/lib/python3.12",                            # stdlib + site-packages
        _settings.skillenv_python,
        f"{_skill_root}/lib/libpython3.12.so.1.0",
        f"{_skill_root}/lib/libpython3.12.so",
        f"{_skill_root}/lib/python3.12",
        _settings.node_path,
        f"{_node_root}/lib/node_modules",
    )
    for d in (*_file_ro_mounts, *(_settings.sandbox_extra_ro or [])):
        if Path(d).exists():
            cmd += ["--ro-bind", d, d]
    for f in (ro_files or []):
        if Path(f).exists():
            cmd += ["--ro-bind", f, f]
    # 挂载顺序：先只读白名单、后可写工作目录（后挂载覆盖先挂载的子路径）——
    # A1（2026-08-10）：read_dirs 可能包含 cwd 的祖先（本会话沙盒根），必须先挂 ro 再挂 cwd 的
    # bind rw，否则 cwd 被 ro 覆盖导致工作目录不可写
    for d in read_dirs:
        if Path(d).exists():
            cmd += ["--ro-bind", d, d]
    for d in work_dirs:
        if Path(d).exists():
            cmd += ["--bind", d, d]
    # loopback：--unshare-net 后 lo 处于 DOWN（bwrap userns 内 root 可 ip link set lo up）
    if net == "loopback":
        argv = ["/bin/bash", "-c", "/usr/sbin/ip link set lo up 2>/dev/null; exec \"$@\"", "bwrap-sh", *argv]
    cmd += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--chdir", cwd, *argv]
    return cmd


async def run(
    *,
    lang: str,
    cwd: str,
    env_extra: dict | None = None,
    code: str | None = None,
    script_path: str | None = None,
    args_extra: list[str] | None = None,
    timeout_s: float = 30,
    memory_mb: int | None = 512,   # None=不设 RLIMIT_AS（framework 构建场景）
    nproc_limit: int | None = None,
    interpreter: str | None = None,
    py_s_flag: bool = False,
    pythonpath: str | None = None,
    bwrap: bool = False,            # 沙盒 v3：bwrap 文件系统隔离（run_script 与 skill 共用）
    bwrap_read_dirs: list[str] | None = None,  # bwrap 白名单只读路径（本会话上传/产出目录）
    bwrap_write_dirs: list[str] | None = None,  # 2026-08-20：可写路径（技能目录——agent 可改技能文件）
    bwrap_ro_files: list[str] | None = None,   # F2：文件级只读挂载（officecli 二进制等）
    bwrap_net: str = "off",         # F2：off=断网（默认，兼容原 _sandbox_net_enabled 语义）/ loopback=仅回环 / on=联网
    fsize_mb: int | None = None,    # D5：单文件写入上限（run_script 传配置；skill 不设）
) -> dict:
    """在隔离沙箱中执行脚本，返回 {exit_code, stdout, stderr, duration_s}。

    code 与 script_path 二选一：code 走解释器 -c；script_path 直接执行脚本文件。
    python 场景参数化：interpreter 指定解释器绝对路径（run_script=python3，skill=skillenv python）；
    py_s_flag=True 时加 -S（禁用 site，仅显式 PYTHONPATH 挂载依赖）；
    args_extra 为脚本附加参数（skill 入口脚本的 argv[1] = args.json）；
    nproc_limit 默认 128（WSL2 计数膨胀，node 自动放宽 1024）。

    D2/D3（2026-08-10）：stderr 截断保尾（异常信息在 traceback 末尾）；超时回传终止前部分输出。
    """
    if lang not in LANGS:
        return {"error": f"暂不支持语言 {lang}（支持: {sorted(LANGS)}）"}
    if not code and not script_path:
        return {"error": "需要提供 code 或 script_path"}
    if nproc_limit is None:
        nproc_limit = 1024 if lang == "node" else 128
    # 沙盒 v3：bwrap 创建 user namespace 需要进程余量（NPROC=128 时 EAGAIN 实测失败）
    if bwrap and nproc_limit < 1024:
        nproc_limit = 1024

    if lang == "python":
        argv = [interpreter or "python3"]
        if py_s_flag:
            argv.append("-S")
        argv += ["-c", code] if code is not None else [str(script_path)]
    elif lang == "bash":
        argv = ["/bin/bash", "-c", code] if code is not None else ["/bin/bash", str(script_path)]
    else:  # node
        argv = [interpreter or "node"]
        argv += ["-e", code] if code is not None else [str(script_path)]
    argv += args_extra or []

    # 沙盒 v3：bwrap 文件系统隔离（根只读 + 工作目录可写 + 白名单读路径）
    if bwrap:
        # F2：net 三态——off 兼容原 _sandbox_net_enabled 语义（run_script 保持配置可开网）；
        # loopback/on 由调用方（skill）显式指定，不再咨询配置
        if bwrap_net == "off":
            net_enabled = await _sandbox_net_enabled()
            net_arg = "on" if net_enabled else "off"
        else:
            net_arg = bwrap_net
        argv = _bwrap_wrap(argv, cwd, work_dirs=[cwd, *(bwrap_write_dirs or [])], read_dirs=bwrap_read_dirs or [],
                           ro_files=bwrap_ro_files, net=net_arg)

    # 干净环境：不携带任何应用密钥/配置；skill 场景经 env_extra 追加内部令牌与工具路径
    clean_env = {
        "PATH": "/usr/bin:/bin",
        "HOME": cwd,
        "LANG": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
        # OpenBLAS（numpy 依赖）在 RLIMIT_AS 受限下的已知冲突：单线程减少内存预留
        "OPENBLAS_NUM_THREADS": "1",
        "OPENBLAS_MAIN_FREE": "1",
    }
    if pythonpath:
        clean_env["PYTHONPATH"] = pythonpath
    if env_extra:
        clean_env.update(env_extra)

    started = time.time()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=clean_env,
            start_new_session=True,       # 独立进程组（可整组击杀）
            preexec_fn=lambda: _set_rlimits(timeout_s, memory_mb, nproc_limit, fsize_mb),  # 资源限制（POSIX）
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return {"error": f"沙箱运行时不可用（{argv[0]} 未安装）"}

    # D3（2026-08-10）：communicate() 改手动泵读——超时/取消时保留已读部分输出（死前进度可见，
    # LLM 能判断是死循环还是计算量过大，而非盲改重跑）
    out_parts: list[bytes] = []
    err_parts: list[bytes] = []

    async def _pump(stream, parts: list[bytes]) -> None:
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                break
            parts.append(chunk)

    timed_out = False
    exit_code: int | None = None
    p_out = asyncio.create_task(_pump(proc.stdout, out_parts))
    p_err = asyncio.create_task(_pump(proc.stderr, err_parts))
    try:
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout_s)
            exit_code = proc.returncode
        except asyncio.TimeoutError:
            timed_out = True
            # 杀整个进程组（子进程可能再 fork）
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                pass
    finally:
        # S11：取消/异常路径（客户端断连 task.cancel() → wait 取消）也杀进程组。
        # asyncio wait 取消只杀单进程，脚本 fork 的孙进程可残留——finally 兜底整组击杀。
        if proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    # 泵收尾：进程已死 → EOF 必达；超时/取消场景加 3s 兜底防悬挂
    try:
        await asyncio.wait_for(asyncio.gather(p_out, p_err), timeout=3)
    except asyncio.TimeoutError:
        pass

    duration = round(time.time() - started, 1)
    stdout = _truncate(b"".join(out_parts))
    stderr = _truncate(b"".join(err_parts))
    if timed_out:
        return {
            "error": f"执行超时（>{timeout_s}s），已强制终止。以下为终止前的部分输出：",
            "exit_code": None,
            "stdout": stdout,
            "stderr": stderr,
            "duration_s": duration,
        }
    # D2（2026-08-10）：traceback 截断保尾——保留首行（异常类型）+ 尾部 6000 字符
    # （Python 最终异常消息在栈底，原截头丢掉了真正的错误）
    if exit_code != 0 and "Traceback" in stderr and len(stderr) > 6000:
        first = stderr.splitlines()[0] if stderr.strip() else ""
        stderr = first + "\n...（堆栈中间已省略）\n" + stderr[-6000:]
    return {"exit_code": exit_code, "stdout": stdout, "stderr": stderr, "duration_s": duration}
