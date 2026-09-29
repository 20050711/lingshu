"""套件 C：22 用户池全功能同步调用并发（部署形态 :24426）。

- 15 个 press 账号（复用 press_users_test 的 15 个功能面）+ 7 个现有账号
  （demo/demo_admin/demo1/demo_admin1/ceo/admin/demo2-1）+ 1 个断连哨兵
- 核心观测：qm:lease:video_gen ≤1（容量 1 串行生效）、default 队列排队采样、
  E005 零触发、零 auth:fail、health <1s
- 全程无 logout、无 demo 同步

用法: cd backend && source scripts/env_aip.sh && python -u tests/concurrency/mixed_pool_test.py

费用档说明：默认全套需用户亲自执行（含 GLM 图片/视觉、Agnes 视频等付费/配额调用）。
FREE=1 免费档冒烟（如 FREE=1 python -u tests/concurrency/mixed_pool_test.py）：跳过
付费/配额面——press09 图片生成(GLM 图片)、press13 视频生成(Agnes)、press14 图片识别
(GLM 视觉)、press15 联网搜索，不计入全过判定；press10/video_batch 保留（断言依赖真实
GLM 拆解，费用档）。另：Redis 加密码后需设环境变量 REDIS_PASSWORD，redis-cli 采样自动加 -a。
"""
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tests.creds import pw  # 密码走环境变量/backend/.env.test（2026-09-10）

from press_users_test import (  # noqa: E402
    login as press_login,
    new_session,
    task_ask_query,
    task_chain,
    task_feedback,
    task_image_gen,
    task_image_recognition,
    task_kb,
    task_multi_round,
    task_resume,
    task_sandbox,
    task_subagent,
    task_upload,
    task_video_batch,
    task_video_gen,
    task_web,
    ask,
)

BASE = "http://127.0.0.1:24426/api/v1"
RESULTS: list[dict] = []
QUEUE_SAMPLES: dict[str, list[int]] = {"video_gen": [], "default": []}
E005_SEEN: list[str] = []

# FREE=1 免费档：跳过付费/配额任务面（GLM 图片/视觉、Agnes 视频、联网搜索），不计入 ok_all
FREE = os.environ.get("FREE") == "1"
FREE_SKIP_USERS = ("press09", "press13", "press14", "press15")
SKIPPED: list[str] = list(FREE_SKIP_USERS) if FREE else []


def rec(label: str, ok: bool, detail: str) -> None:
    RESULTS.append({"label": label, "ok": ok, "detail": detail})
    print(f"  {'✓' if ok else '✗'} {label} | {detail}")


async def task_skip(c, h, sid, label):
    """FREE 档跳过付费/配额面：rec ok=True（不显示失败）但不参与 ok_all 硬判定。"""
    rec(label, True, "skip(免费档)")


async def login(c: httpx.AsyncClient, dept: str, username: str, password: str) -> dict:
    r = await c.post("/auth/login", json={"department_id": dept, "username": username, "password": password})
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.cookies['access_token']}", "X-Client-ID": f"mix-{username}"}


# ---------- 现有账号功能面 ----------

async def task_demo_full(c, h, sid, label):
    """demo：多轮复杂链（查询→图表→docx→沙盒）+ 直调子代理。"""
    await task_chain(c, h, sid, f"{label}-a", [
        ("把这段话整理成三点要点：数据平稳、成本下降、转化提升", ["file_parse"]),
        ("把上面的要点画成柱状图（数值自拟）", ["generate_chart"]),
        ("生成包含这张图的 docx 报告", ["doc_export"]),
        ("用 Python 计算 1 到 100 的平方和", ["run_script"]),
    ])
    await task_subagent(c, h, sid, f"{label}-b", dept="demo")


async def task_demo_admin(c, h, sid, label):
    """demo_admin：知识库上传 + 团队技能查看。"""
    with open("/tmp/test_resume.docx", "rb") as f:
        r = await c.post("/knowledge/documents", headers=h,
                         data={"title": "演示团队知识文档"}, files={"file": ("ma_kb.docx", f)})
    r2 = await c.get("/skills", headers=h)
    rec(label, r.status_code in (200, 201) and r2.status_code == 200, f"上传={r.status_code} skills={r2.status_code}")


async def task_demo1_query(c, h, sid, label):
    """demo1：多轮查询链 + 沙盒。"""
    await task_chain(c, h, sid, label, [
        ("把这段话整理成要点：本周销售概况平稳", ["file_parse"]),
        ("用 Python 模拟计算 100 个随机数的平均值", []),
    ])


async def task_demo_admin1(c, h, sid, label):
    """demo_admin1：业务端点连通性（2026-09-17：原 /dashboard/data-freshness 已下线）。"""
    r = await c.get("/chat/sessions", headers=h)
    ok = r.status_code == 200
    rec(label, ok, f"status={r.status_code}")


async def task_ceo(c, h, sid, label):
    """ceo：业务端点连通性 + 多轮产出链（2026-09-17：CEO 看板 3 接口已随下线下线）。"""
    r = await c.get("/chat/sessions", headers=h)
    await task_chain(c, h, sid, f"{label}-a", [
        ("把这段话整理成三点要点：数据平稳、成本下降、转化提升", ["file_parse"]),
        ("把结果导出成 docx 报告", ["doc_export"]),
    ])
    rec(label, r.status_code == 200, f"会话列表={r.status_code}")


async def task_admin(c, h, sid, label):
    """admin：并发登录 + 管理面 + 预期失败面（业务接口 403）。"""
    r1 = await c.get("/admin/users", headers=h)
    r2 = await c.get("/admin/overview", headers=h)
    # 预期失败面：admin 访问业务端点 → 403
    r3 = await c.post("/chat/sessions", headers=h, json={"client_id": "admin-mix"})
    r4 = await c.get("/chat/sessions", headers=h)
    ok = r1.status_code == 200 and r2.status_code == 200 and r3.status_code in (403, 422) and r4.status_code in (403, 422)
    rec(label, ok, f"users={r1.status_code} overview={r2.status_code} 建会话={r3.status_code}(预期403) 列表={r4.status_code}(预期403)")


async def task_demo2(c, h, sid, label):
    """demo2：闲聊。"""
    done, errors, _ = await ask(c, h, sid, "你好，介绍一下你", [])
    rec(label, done and not errors, f"done={done} errors={errors}")


async def task_abort_sentinel(c, h, sid, label):
    """哨兵：SSE 中途断连 → 后端不崩 → 随后正常问答。"""
    ev = None
    async with c.stream("POST", "/chat/ask", headers=h, json={
        "session_id": sid, "question": "把这段话整理成要点：数据平稳", "active_skills": ["file_parse"], "auto_skill": False,
    }) as resp:
        n = 0
        async for line in resp.aiter_lines():
            n += 1
            if n >= 2:
                await resp.aclose()
                break
    await asyncio.sleep(2)
    # 2026-08-19（任务后台化语义适配）：断连任务继续后台执行（08-18 起）——等待其
    # 完成再发恢复问答（同会话 running 任务 → ask 必 409——这是新语义的正确防御）
    for _ in range(20):
        st = (await c.get(f"/chat/sessions/{sid}/status", headers=h)).json()
        if not st.get("running"):
            break
        await asyncio.sleep(1)
    done, errors, _ = await ask(c, h, sid, "你好", [])
    rec(label, done and not errors, f"断连后恢复 done={done} errors={errors}")


# ---------- 22 用户池 ----------

async def run_press(c, username):
    try:
        h = await press_login(c, username, "Press#2026$Test", "press")
        sid = await new_session(c, h)
        fns = {
            # 与套件 B 相同的多轮工具链
            "press01": (task_chain, ([
                ("把这段话整理成三点要点：数据平稳、成本下降、转化提升", ["file_parse"]),
                ("按城市类型汇总展现量，前 5 名并画柱状图", ["file_parse", "generate_chart"]),
            ],)),
            "press02": (task_chain, ([
                ("把这段话写得更简洁：本周数据整体平稳", ["file_parse"]),
                ("把这段话翻译成英文：本周数据整体平稳", ["file_parse"]),
                ("把汇总结果画成柱状图", ["generate_chart"]),
            ],)),
            "press03": (task_chain, ([
                ("列出三条写作建议", ["file_parse"]),
                ("搜索推广的展现量和点击量是多少？", ["file_parse"]),
                ("把两次结果合并成一张汇总表展示", ["file_parse", "generate_chart"]),
            ],)),
            "press04": (task_multi_round, ()),
            "press05": (task_chain, ([
                ("生成一份渠道分析 docx 报告（含消费汇总表）", ["doc_export", "file_parse"]),
                ("再生成一份 pptx 版本", ["doc_export"]),
            ],)),
            "press06": (task_upload, ()),
            "press07": (task_kb, ()),
            "press08": (task_chain, ([
                ("用 Python 计算 1 到 100 的和", []),
                ("用 Python 生成前 20 个斐波那契数", []),
            ],)),
            "press09": (task_image_gen, ()),
            "press10": (task_video_batch, ()),
            "press11": (task_resume, ()),
            "press12": (task_feedback, ()),
            "press13": (task_video_gen, ()),
            "press14": (task_image_recognition, ()),
            "press15": (task_chain, ([
                ("用联网搜索查询最近的 AI 行业新闻", ["web_search"]),
                ("根据刚才的结果总结 3 条要点", []),
            ],)),
        }
        if FREE and username in FREE_SKIP_USERS:
            await task_skip(c, h, sid, username)
            return
        fn, args = fns[username]
        if args:
            await fn(c, h, sid, username, *args)
        else:
            await fn(c, h, sid, username)
    except Exception as e:
        rec(username, False, f"例外: {str(e)[:120]}")


# ---------- 队列观测 ----------

def _redis_cli_args() -> list[str]:
    """SEC-18：Redis 加密码后裸 redis-cli 需 -a；-a 的 warning 打到 stderr，采样只解析 stdout 不受影响。"""
    pw = os.environ.get("REDIS_PASSWORD", "")
    return ["-a", pw] if pw else []


def _redis_zcard(key: str) -> int:
    try:
        r = subprocess.run(["redis-cli", *_redis_cli_args(), "zcard", key], capture_output=True, text=True, timeout=5)
        return int(r.stdout.strip() or 0)
    except Exception:
        return -1


def _redis_auth_fail() -> int:
    try:
        r = subprocess.run(["redis-cli", *_redis_cli_args(), "scan", "0", "match", "auth:fail*", "count", "100"],
                           capture_output=True, text=True, timeout=5)
        return len(r.stdout.strip().splitlines()) - 1 if r.stdout.strip() else 0
    except Exception:
        return 0


async def queue_monitor():
    """每 5s 采样视频生成/默认队列租约 + auth:fail 计数。"""
    while True:
        vg = _redis_zcard("qm:lease:video_gen")
        df = _redis_zcard("qm:lease:default")
        af = _redis_auth_fail()
        QUEUE_SAMPLES["video_gen"].append(vg)
        QUEUE_SAMPLES["default"].append(df)
        if vg > 1:
            E005_SEEN.append(f"video_gen 租约 {vg} > 1（容量 1 串行被破坏）")
        if af > 0:
            E005_SEEN.append(f"auth:fail key {af} 个（登录防爆破计数残留）")
        await asyncio.sleep(5)


async def main() -> int:
    health_lags: list[float] = []

    async def monitor():
        while True:
            t0 = time.time()
            try:
                await c.get("/health")
                health_lags.append(round(time.time() - t0, 3))
            except Exception:
                health_lags.append(999.0)
            await asyncio.sleep(1)

    print("=== 套件 C：22 用户池全功能同步调用（:24426）===")
    if FREE:
        print(f"  [FREE 免费档] 跳过付费/配额面: {'、'.join(FREE_SKIP_USERS)}"
              "（press10 保留，其断言依赖真实 GLM 拆解，费用档）")
    wall_t0 = time.time()
    async with httpx.AsyncClient(base_url=BASE, timeout=400) as c:
        hm = asyncio.create_task(monitor())
        qm = asyncio.create_task(queue_monitor())
        await asyncio.gather(*[run_press(c, f"press{i:02d}") for i in range(1, 16)])

        # 现有账号
        async def run_existing(dept, username, password, fn, extra_sid=False):
            try:
                h = await login(c, dept, username, password)
                sid = await new_session(c, h) if extra_sid else None
                await fn(c, h, sid, username)
            except Exception as e:
                rec(username, False, f"例外: {str(e)[:120]}")

        await asyncio.gather(
            run_existing("demo", "demo", pw("demo"), task_demo_full, True),
            run_existing("demo", "demo_admin", pw("demo_admin"), task_demo_admin, True),
            run_existing("demo1", "demo1", pw("demo1"), task_demo1_query, True),
            run_existing("demo1", "demo_admin1", pw("demo_admin1"), task_demo_admin1, False),
            run_existing("ceo", "ceo", pw("ceo"), task_ceo, True),
            run_existing("dept_root", "admin", pw("admin"), task_admin, False),
            run_existing("demo2", "1", pw("demo2"), task_demo2, True),
            run_existing("press", "press01", "Press#2026$Test", task_abort_sentinel, True),
        )
        wall = round(time.time() - wall_t0, 1)
        hm.cancel()
        qm.cancel()

    print("\n=== 结果明细 ===")
    for r in RESULTS:
        print(f"  {r['label']:<12} {'✓' if r['ok'] else '✗'} | {r['detail']}")
    max_lag = max(health_lags) if health_lags else 999
    ok_all = all(r["ok"] for r in RESULTS if r["label"] not in SKIPPED)  # FREE 跳过项不参与硬判定
    vg_max = max(QUEUE_SAMPLES["video_gen"]) if QUEUE_SAMPLES["video_gen"] else 0
    df_max = max(QUEUE_SAMPLES["default"]) if QUEUE_SAMPLES["default"] else 0
    n_skip = len(SKIPPED)
    extra = f"（FREE 跳过 {n_skip} 个付费面）" if n_skip else ""
    print(f"\n=== 汇总 ===")
    print(f"  23 任务全过: {ok_all}{extra} | 墙钟: {wall}s | health max={max_lag}s")
    print(f"  队列租约 video_gen max={vg_max}（≤1 通过）| default max={df_max}（采样排队长度）")
    print(f"  E005/异常: {E005_SEEN if E005_SEEN else '零触发'}")
    ok = ok_all and max_lag < 1.0 and vg_max <= 1 and not E005_SEEN
    print("=== PASS ===" if ok else "=== FAIL ===")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
