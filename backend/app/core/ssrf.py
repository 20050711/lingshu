"""SSRF 防护：URL 公网可达性校验（SEC-01 视频链接 / SEC-15 图片识别共用）。

校验维度：协议白名单（http/https）+ 主机解析后全部 IP 不在内网/保留段 + 端口白名单 + 禁 userinfo。
残余风险：DNS rebinding TOCTOU（校验与连接分离）——局域网威胁模型下接受；
云元数据段（169.254.169.254）在 WSL 不可达但本工具已覆盖该段，云部署同样受保护。
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

# 仅允许公网 http/https（视频/图片等）
ALLOWED_SCHEMES = {"http", "https"}
# 常用公网端口白名单（其余端口内网服务指纹风险高）
ALLOWED_PORTS = {80, 443, 8080, 8443}
# 显式补充保留段（ipaddress 的属性判定未覆盖）
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),      # CGNAT 共享地址段
    ipaddress.ip_network("198.18.0.0/15"),      # 网络基准测试保留
]
MAX_REDIRECTS = 5


def _ip_blocked(ip: str) -> str | None:
    """判定单个 IP 是否属内网/保留段；放行返回 None。"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return f"非法 IP 地址：{ip}"
    if (
        addr.is_private or addr.is_loopback or addr.is_link_local
        or addr.is_multicast or addr.is_reserved or addr.is_unspecified
    ):
        return f"IP 属于内网/保留地址段：{ip}"
    for net in _BLOCKED_NETWORKS:
        if addr in net:
            return f"IP 属于保留地址段：{ip}"
    return None


def validate_public_url(url: str) -> str | None:
    """校验 URL 可被公网访问；合法返回 None，非法返回错误信息（同步实现，阻塞 DNS 解析）。"""
    if not url or not isinstance(url, str):
        return "URL 不能为空"
    try:
        parts = urlsplit(url.strip())
    except ValueError as e:
        return f"URL 格式非法：{e}"
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        return f"仅支持 http/https 协议，收到：{parts.scheme or '空'}"
    if not parts.hostname:
        return "URL 缺少主机名"
    if parts.username or parts.password:
        return "URL 不允许携带用户认证信息（userinfo）"
    port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    if port not in ALLOWED_PORTS:
        return f"端口 {port} 不在允许列表（80/443/8080/8443）"
    host = parts.hostname.rstrip(".")
    # 解析主机全部 IP（防 DNS 多记录时绕过一个判定）
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return f"域名无法解析：{host}"
    for info in infos:
        err = _ip_blocked(info[4][0])
        if err:
            return err
    return None


async def validate_public_url_async(url: str) -> str | None:
    """异步包装（DNS 解析走线程池，不阻塞事件循环）。"""
    return await asyncio.to_thread(validate_public_url, url)


def validate_redirect_url(location: str) -> str | None:
    """重定向目标校验（与 validate_public_url 同规则，供手动重定向链复用）。"""
    return validate_public_url(location)
