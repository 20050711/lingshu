"""模型分层配置回归（2026-09-02 新增 llm_tools 定制化工具独立档）。

覆盖：任务分组一致性（catalog group 与 TOOL_TASK_KEYS 无漂移）/ llm_tools 独立档分流 /
未配置回退 llm_aux / usage 语义（勾选才用；空数组与缺省=不限任务，同 llm_aux 既有语义）。

用法（本地 DB 直连，无需后端服务/LLM）：
    cd backend && source scripts/env_aip.sh && python -u tests/model_layer_smoke.py
原则：测试自净——使用隔离团队 model_smoke_dept 写入，finally 删除该团队全部 model_layer key
（不触碰 demo/default 等真实团队配置）。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from app.core.database import get_global_engine

TEST_DEPT = "model_smoke_dept"
RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"{'✓' if ok else '✗'} {name}" + (f"  {detail}" if detail else ""))


async def main() -> None:
    from app.services import config_service as cs
    from app.services.config_service import TOOL_TASK_KEYS, get_aux_model_cfg_for_task, get_model_config
    from app.services.model_catalog import AUX_TASKS

    def _invalidate_cache() -> None:
        # 测试直改 DB（不经服务端写接口）——手动失效 config_service 60s TTL 缓存，防断言读旧值
        cs._CACHE["ts"] = 0.0

    def _key(kind: str) -> str:
        return f"model_layer.{TEST_DEPT}"

    async def _write(segments: dict) -> None:
        # segments={role: {kind: {...}}}，覆盖写入（原值在 finally 删除，无需保留）
        async with get_global_engine().begin() as conn:
            await conn.execute(
                text("INSERT INTO system_config (key, value, description, updated_at) VALUES (:k, :v, :d, NOW()) "
                     "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()"),
                {"k": _key(""), "v": json.dumps({"employee": segments}, ensure_ascii=False),
                 "d": "model_layer_smoke 测试"})
        _invalidate_cache()

    # ---- 1) 分组一致性：catalog group=tools 的任务 == TOOL_TASK_KEYS（防两处漂移）----
    tools_group = {t["key"] for t in AUX_TASKS if t.get("group") == "tools"}
    aux_group = {t["key"] for t in AUX_TASKS if t.get("group") != "tools"}
    record("分组一致性：catalog tools 组 == TOOL_TASK_KEYS",
           tools_group == TOOL_TASK_KEYS,
           f"tools={sorted(tools_group)} keys={sorted(TOOL_TASK_KEYS)}")
    record("分组完整性：任务无重复",
           len({t['key'] for t in AUX_TASKS}) == len(AUX_TASKS), f"n={len(AUX_TASKS)}")
    missing = {"video", "resume", "meeting", "feishu_bot", "ai_customer_distill", "ai_customer_chat"}
    record("分组覆盖：六个用户主动工具在 tools 组 + 辅助任务分类在 aux 组",
           missing <= tools_group and "customer_classify" in aux_group,
           f"缺={sorted(missing - tools_group)}")

    # ---- 2) 分流/回退（隔离团队 model_smoke_dept；finally 删除）----
    # 分层链注意：团队无档 → model_layer.default 兜底（default 当前有用户真实 llm_tools 档，
    # 断言用动态期望，不硬编码；本测试不触碰 default/demo 等真实配置）
    default_tools = await get_model_config("default", "employee", "llm_tools")
    try:
        # 2a. 团队仅配 llm_aux（agnes）→ 工具任务无团队 llm_tools 时按 default 链兜底
        # （default 有 llm_tools 则用 default 档；没有才落团队 llm_aux）
        await _write({"llm_aux": {"platform": "agnes", "model": "agnes-2.5-flash",
                                  "usage": ["video", "kb_rank"]}})
        aux_ref = await get_model_config(TEST_DEPT, "employee", "llm_aux")
        expected_a = default_tools if default_tools else aux_ref
        cfg_video = await get_aux_model_cfg_for_task(TEST_DEPT, "employee", "video")
        record("团队无 llm_tools：按分层链解析（default 档兜底或团队 llm_aux）",
               cfg_video == expected_a,
               f"video={cfg_video.get('model') if cfg_video else None}")

        # 2b. 团队加 llm_tools（deepseek，usage=[video, meeting]）→ 团队档优先于 default
        await _write({"llm_aux": {"platform": "agnes", "model": "agnes-2.5-flash",
                                  "usage": ["video", "kb_rank", "ai_customer_distill"]},
                      "llm_tools": {"platform": "deepseek", "model": "deepseek-v4-flash",
                                    "usage": ["video", "meeting"]}})
        cfg_video = await get_aux_model_cfg_for_task(TEST_DEPT, "employee", "video")
        cfg_meeting = await get_aux_model_cfg_for_task(TEST_DEPT, "employee", "meeting")
        record("团队 llm_tools 生效：勾选任务 video/meeting → 团队独立档（优先 default）",
               cfg_video is not None and cfg_video.get("platform") == "deepseek"
               and cfg_meeting is not None and cfg_meeting.get("platform") == "deepseek",
               f"video={cfg_video.get('model') if cfg_video else None} "
               f"meeting={cfg_meeting.get('model') if cfg_meeting else None}")
        # llm_tools 未勾选工具任务（ai_customer_distill）→ 回退团队 llm_aux（usage 勾选则生效）
        aux_ref = await get_model_config(TEST_DEPT, "employee", "llm_aux")  # 2b 写入后重读
        cfg_ai = await get_aux_model_cfg_for_task(TEST_DEPT, "employee", "ai_customer_distill")
        record("llm_tools 未勾选任务 → 回退团队 llm_aux",
               cfg_ai == aux_ref, f"ai_customer_distill={cfg_ai.get('model') if cfg_ai else None}")
        # 辅助任务（kb_rank）不受 llm_tools 影响
        cfg_kb = await get_aux_model_cfg_for_task(TEST_DEPT, "employee", "kb_rank")
        record("辅助任务不受 llm_tools 档影响", cfg_kb == aux_ref,
               f"kb_rank={cfg_kb.get('model') if cfg_kb else None}")

        # 2c. usage 语义：缺省与空数组 = 不限任务（同 llm_aux 既有语义——配置页"不勾选=全部任务"）
        await _write({"llm_tools": {"platform": "glm", "model": "glm-4.7-flash"}})
        cfg_video = await get_aux_model_cfg_for_task(TEST_DEPT, "employee", "video")
        record("llm_tools usage 缺省 → 不限任务（video 用独立档）",
               cfg_video is not None and cfg_video.get("platform") == "glm",
               f"video={cfg_video.get('model') if cfg_video else None}")
        await _write({"llm_tools": {"platform": "glm", "model": "glm-4.7-flash", "usage": []}})
        cfg_video = await get_aux_model_cfg_for_task(TEST_DEPT, "employee", "video")
        record("llm_tools usage=[] → 不限任务（同缺省语义）",
               cfg_video is not None and cfg_video.get("platform") == "glm",
               f"video={cfg_video.get('model') if cfg_video else None}")

        # 2d. llm_tools usage 限定排除后回退：usage 只勾 resume → video 回退 llm_aux
        await _write({"llm_aux": {"platform": "agnes", "model": "agnes-2.5-flash",
                                  "usage": ["video", "kb_rank"]},
                      "llm_tools": {"platform": "deepseek", "model": "deepseek-v4-flash",
                                    "usage": ["resume"]}})
        aux_ref = await get_model_config(TEST_DEPT, "employee", "llm_aux")  # 2d 写入后重读
        cfg_video = await get_aux_model_cfg_for_task(TEST_DEPT, "employee", "video")
        cfg_resume = await get_aux_model_cfg_for_task(TEST_DEPT, "employee", "resume")
        record("llm_tools usage 未含该任务 → 回退 llm_aux；含的任务用独立档",
               cfg_video == aux_ref and cfg_resume is not None
               and cfg_resume.get("platform") == "deepseek",
               f"video={cfg_video.get('model') if cfg_video else None} "
               f"resume={cfg_resume.get('model') if cfg_resume else None}")
    finally:
        async with get_global_engine().begin() as conn:
            await conn.execute(
                text("DELETE FROM system_config WHERE key LIKE 'model_layer.model_smoke_dept%'"))
        _invalidate_cache()

    # 清理后抽查：隔离团队无配置 → 走 default 档兜底（无残留团队档）
    after = await get_aux_model_cfg_for_task(TEST_DEPT, "employee", "video")
    record("清理后隔离团队无残留 → 回 default 分层档", after == default_tools,
           f"video={after.get('model') if after else None}")

    print("\n==== 结果汇总 ====")
    failed = [x for x in RESULTS if not x[1]]
    for name, ok, detail in RESULTS:
        print(f"{'✓' if ok else '✗'} {name}" + (f"  {detail}" if detail else ""))
    print(f"\n通过 {len(RESULTS) - len(failed)}/{len(RESULTS)}")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
