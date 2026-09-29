"""SEC-01/SEC-15：SSRF 校验工具单测（零 LLM，本地直跑）。

用法：cd backend && conda run -n aip python -u tests/test_ssrf.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.ssrf import validate_public_url, validate_public_url_async

PASS = 0
FAIL = 0


def check(name: str, cond: bool) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}")


def test_sync() -> None:
    print("== 同步校验 ==")
    # 合法公网 URL（以下仅做格式/协议/端口校验，不做真实网络请求）
    check("合法 https 域名", validate_public_url("https://example.com/video.mp4") is None)
    check("合法 http 域名", validate_public_url("http://example.com/a.mp4") is None)
    check("带路径/查询", validate_public_url("https://example.com/a?b=1&c=2") is None)
    check("IPv4 公网直链", validate_public_url("http://93.184.216.34:80/x.mp4") is None)
    check("IPv6 公网直链", validate_public_url("http://[2606:2800:220:1:248:1893:25c8:1946]/x.mp4") is None)
    # 内网/保留段拒绝
    check("回环 127.0.0.1", validate_public_url("http://127.0.0.1:8001/api/v1/health") is not None)
    check("元数据 169.254.169.254", validate_public_url("http://169.254.169.254/latest/meta-data/") is not None)
    check("私网 10.x", validate_public_url("http://10.0.0.1/x.mp4") is not None)
    check("私网 192.168.x", validate_public_url("http://192.168.1.1/x.mp4") is not None)
    check("私网 172.16.x", validate_public_url("http://172.16.0.1/x.mp4") is not None)
    check("链路本地 fe80", validate_public_url("http://[fe80::1]/x.mp4") is not None)
    check("IPv6 回环 ::1", validate_public_url("http://[::1]/x.mp4") is not None)
    check("CGNAT 100.64", validate_public_url("http://100.64.0.1/x.mp4") is not None)
    check("0.0.0.0 段", validate_public_url("http://0.0.0.1/x.mp4") is not None)
    check("基准保留 198.18", validate_public_url("http://198.18.0.1/x.mp4") is not None)
    # 协议/格式
    check("非 http 协议（ftp）", validate_public_url("ftp://example.com/x.mp4") is not None)
    check("无协议", validate_public_url("example.com/x.mp4") is not None)
    check("空串", validate_public_url("") is not None)
    check("缺 host", validate_public_url("http:///x.mp4") is not None)
    check("userinfo 拒绝", validate_public_url("http://user:pass@example.com/x.mp4") is not None)
    check("非白名单端口 8081", validate_public_url("http://example.com:8081/x.mp4") is not None)
    check("白名单端口 8080", validate_public_url("http://example.com:8080/x.mp4") is None)
    check("白名单端口 8443", validate_public_url("https://example.com:8443/x.mp4") is None)


async def test_async() -> None:
    print("== 异步包装 ==")
    check("async 合法", await validate_public_url_async("https://example.com/x.mp4") is None)
    check("async 内网", await validate_public_url_async("http://127.0.0.1/x.mp4") is not None)


def main() -> None:
    test_sync()
    asyncio.run(test_async())
    print(f"\n结果：PASS={PASS} FAIL={FAIL}")
    if FAIL:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
