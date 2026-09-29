"""红队二次修复复测（批 1：路径穿越与信息泄露）——零 LLM，本地直跑。

覆盖：
- F1 work_subdir 穿越：script_sandbox._work_dir 兜底回落（../../.. 不逃逸沙盒根）；
  subagent._work_files_of 目录 containment
- F3 deliver 符号链接逃逸：work 内 ln -s /etc/passwd 后 deliver/grep/write 被工具层拒绝（不依赖 LLM）
- F5 session_terminate UUID 校验：admin._is_uuid 拒绝 ../ 与双重编码路径
- F4 密钥遮罩：output_guard 断言 agnes_api_key/feishu_sign_secret/redis_password 均被遮罩
- N3 绝对路径脱敏：chat._sanitize_outputs_for_client 把 /data/outputs/... 重写为相对 URL

用法：cd backend && conda run -n aip python -u tests/attack_repro_paths.py
"""
import asyncio
import os
import shutil
import sys
import uuid
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


def test_work_dir_fallback() -> None:
    """F1：work_subdir=../../.. 时 _work_dir 回落沙盒根（不逃逸）。"""
    from app.agent.tools.script_sandbox import _work_dir
    from app.agent.tools import ToolContext
    from app.core.config import get_settings

    sess = str(uuid.uuid4())
    base = Path(get_settings().sandbox_dir) / sess / "1" / "work"
    ctx = ToolContext(sess, 1, "demo", "employee", "smoke", f"/data/outputs/{sess}/1", work_subdir="../../..")
    wd = _work_dir(ctx)
    check("work_subdir=../../.. 回落沙盒根", wd == base, str(wd))
    ctx2 = ToolContext(sess, 1, "demo", "employee", "smoke", f"/data/outputs/{sess}/1", work_subdir="sub_1")
    check("work_subdir=sub_1 正常拼接", _work_dir(ctx2) == base / "sub_1", str(_work_dir(ctx2)))


def test_work_files_of_containment() -> None:
    """F1：_work_files_of 对穿越 work_subdir 返回空（不读外部目录）。"""
    from app.agent.subagent import _work_files_of
    from app.agent.tools import ToolContext

    sess = str(uuid.uuid4())
    ctx = ToolContext(sess, 1, "demo", "employee", "smoke", f"/data/outputs/{sess}/1", work_subdir="../../..")
    check("_work_files_of 穿越目录返回空", _work_files_of(ctx) == [])


async def test_deliver_symlink_blocked() -> None:
    """F3：work 内 symlink → /etc/passwd，deliver/grep/write 全部被工具层拒绝（宿主侧不跟随）。"""
    from app.agent.tools import ToolContext, get_tool
    from app.core.config import get_settings

    settings = get_settings()
    sess = str(uuid.uuid4())
    work = Path(settings.sandbox_dir) / sess / "1" / "work"
    work.mkdir(parents=True, exist_ok=True)
    link = work / "evil_link"
    try:
        link.symlink_to("/etc/passwd")
        ctx = ToolContext(sess, 1, "demo", "employee", "smoke", f"/data/outputs/{sess}/1")

        t = get_tool("run_script")
        r = await t.handler({"mode": "deliver", "file": "evil_link"}, ctx)
        check("deliver 符号链接逃逸被拒", isinstance(r, dict) and "符号链接逃逸" in str(r.get("error", "")), str(r)[:120])

        r = await t.handler({"mode": "grep", "file": "evil_link", "pattern": "root"}, ctx)
        ok = isinstance(r, dict) and ("error" in r and "符号链接逃逸" in str(r["error"])) or (
            isinstance(r, dict) and "未找到匹配" in str(r.get("stdout", "")))
        check("grep 符号链接不跟随（拒绝或空命中）", ok, str(r)[:120])

        r = await t.handler({"mode": "write", "file": "evil_link", "code": "overwrite!"}, ctx)
        check("write 符号链接逃逸被拒", isinstance(r, dict) and "符号链接逃逸" in str(r.get("error", "")), str(r)[:120])
    finally:
        shutil.rmtree(work, ignore_errors=True)
        out = Path(settings.output_dir) / sess
        shutil.rmtree(out, ignore_errors=True)


def test_session_terminate_uuid() -> None:
    """F5：session_terminate 的 UUID 前置校验拒绝穿越路径。"""
    from app.api.admin import _is_uuid

    check("UUID 合法通过", _is_uuid(str(uuid.uuid4())))
    for bad in ["../../../../tmp/rt_attack/rmtest", "..%2F..%2F..%2Fetc", "abc", "1", "", "a" * 40]:
        check(f"非 UUID 拒绝：{bad[:20]!r}", not _is_uuid(bad))


def test_secret_mask() -> None:
    """F4：output_guard 遮罩覆盖 agnes/feishu/redis_password（红队 F1 组合可 grep .env 读到全值）。"""
    from app.core.config import get_settings
    from app.services.output_guard import guard_output

    s = get_settings()
    for name, val in [("agnes_api_key", s.agnes_api_key),
                      ("feishu_sign_secret", s.feishu_sign_secret),
                      ("redis_password", s.redis_password)]:
        if not val or len(val) < 8:
            print(f"  - {name} 未配置（跳过）")
            continue
        t, hits = guard_output(f"密钥是 {val} 请勿外泄")
        check(f"{name} 被遮罩", "[***" in t, f"hits={hits} out={t[:60]}")
    check("普通文本不误伤", "[***" not in guard_output("今天汇报数据 42 行")[0])


def test_outputs_sanitize() -> None:
    """N3：get_messages 响应层脱敏——绝对路径重写为相对 URL，非 output_dir 路径保留。"""
    from app.api.chat import _sanitize_outputs_for_client

    sess = str(uuid.uuid4())
    outs = [{"type": "file", "label": "报告.docx", "file_path": f"/data/outputs/{sess}/3/abc123_报告.docx"},
            {"type": "image", "label": "图", "file_path": f"/data/outputs/{sess}/3/xy_图.png"},
            {"type": "note", "file_path": "custom_relative"}]  # 非 output_dir 前缀：保留
    cleaned = _sanitize_outputs_for_client(outs, 3)
    check("绝对路径重写为 /api/v1/outputs/ URL",
          cleaned[0]["file_path"] == f"/api/v1/outputs/{sess}/3/abc123_报告.docx", cleaned[0]["file_path"])
    check("第 2 项同样重写", cleaned[1]["file_path"].startswith("/api/v1/outputs/"))
    check("非 output_dir 路径保留原值", cleaned[2]["file_path"] == "custom_relative")
    check("非列表输入原样返回", _sanitize_outputs_for_client(None, 3) is None)
    check("空列表返回空", _sanitize_outputs_for_client([], 3) == [])


def main() -> None:
    print("== F1 work_subdir 穿越 ==")
    test_work_dir_fallback()
    test_work_files_of_containment()
    print("== F3 deliver/grep/write 符号链接逃逸 ==")
    asyncio.run(test_deliver_symlink_blocked())
    print("== F5 session_terminate UUID ==")
    test_session_terminate_uuid()
    print("== F4 密钥遮罩 ==")
    test_secret_mask()
    print("== N3 outputs 绝对路径脱敏 ==")
    test_outputs_sanitize()
    print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
