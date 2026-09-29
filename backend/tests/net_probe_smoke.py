"""外部连通性探针冒烟（2026-09-11 走查加固项）。

覆盖：握手成败判定 / 全端点不可达才告警 / 连续阈值 + 复报节流 / 恢复清零 / 告警文案带处置命令。
零花费：只做 TCP+TLS 握手（对真实端点）+ 本地黑洞地址（必然失败），**不调 LLM**。

用法：cd backend && source scripts/env_aip.sh && python -u tests/net_probe_smoke.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import net_probe as np  # noqa: E402

_RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    _RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")


async def main() -> int:
    # 本用例会故意制造"连续失败"以验证告警——日志**就地捕获**，不落共享 /data/logs/app.log
    # （2026-09-10 走查教训：测试进程与后端共用该文件，运维会误判成线上故障）。
    import logging

    logs: list[str] = []

    class _Cap(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            logs.append(record.getMessage())

    cap = _Cap(level=logging.INFO)
    probe_logger = logging.getLogger("services.net_probe")
    probe_logger.addHandler(cap)
    old_propagate = probe_logger.propagate
    probe_logger.propagate = False
    try:
        return await _run()
    finally:
        probe_logger.removeHandler(cap)
        probe_logger.propagate = old_propagate


async def _run() -> int:
    # 1) 真实端点握手（config 里的 LLM 端点；只握手不发请求）
    r = await np.probe_external()
    record("配置端点全部可达（TCP+TLS 握手）", r["ok"] and not r["all_down"], r["summary"])

    # 2) 黑洞地址必失败 + 全不可达判定
    np._settings = type("S", (), {"deepseek_base_url": "https://127.0.0.1:1",
                                  "agnes_base_url": "https://127.0.0.1:2"})()
    r2 = await np.probe_external()
    record("全端点不可达 → all_down", r2["all_down"] and not r2["ok"], r2["summary"][:80])

    # 3) 告警阈值与节流：连续 1 次不告警、第 2 次告警且 1 小时内不重复；恢复后清零
    sent: list[str] = []

    async def _fake_notify(title: str, body: str) -> None:
        sent.append(f"{title}|{body}")     # 记全文（断言"处置命令"在末尾）

    import app.services.alert as alert_mod

    orig_notify, orig_settings = alert_mod.notify, np._settings
    alert_mod.notify = _fake_notify          # type: ignore[assignment]
    # 恢复态用**本机临时监听端口**（确定性，不依赖公网）
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    np._fail_streak = 0
    np._last_alert_at = 0.0
    try:
        await np.run_probe_job()
        record("第 1 次失败不告警（阈值 2）", len(sent) == 0, f"sent={len(sent)}")
        await np.run_probe_job()
        record("第 2 次失败触发告警", len(sent) == 1, sent[0][:60] if sent else "")
        record("告警含处置命令", bool(sent) and "stop.sh" in sent[0], sent[0][-60:] if sent else "")
        await np.run_probe_job()
        record("1 小时内不重复告警（节流）", len(sent) == 1, f"sent={len(sent)}")
        np._settings = type("S", (), {"deepseek_base_url": f"http://127.0.0.1:{port}",
                                      "agnes_base_url": f"http://127.0.0.1:{port}"})()
        await np.run_probe_job()      # 本机监听可达 → 视为恢复
        record("恢复后计数清零", np._fail_streak == 0, f"streak={np._fail_streak}")
    finally:
        server.close()
        alert_mod.notify, np._settings = orig_notify, orig_settings   # type: ignore[assignment]
        np._fail_streak = 0
        np._last_alert_at = 0.0

    failed = [x for x in _RESULTS if not x[1]]
    print(f"\n== {len(_RESULTS) - len(failed)}/{len(_RESULTS)} PASS ==")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
