"""输出防护：Agent 回答中不得泄露 .env 密钥信息（用户需求：确保 env 文件信息不泄露）。

策略：收集 settings 中全部敏感值（API key / secret_key / 数据库密码段），
对输出文本做子串匹配（含值的前 8 字符前缀片段，防模型截断式泄露），
命中即替换为占位提示。纯规则匹配，零 LLM 成本，接入流式输出与落库两处。

E-05（2026-08-10）：动态敏感集合按 TTL 惰性清理（原只增不删——skill 令牌未消费时
永久注册，guard_output 每次全量匹配，SSE 延迟与内存线性恶化）。
"""
from __future__ import annotations

import re
import time

from app.core.config import get_settings

_settings = get_settings()

_MASK = "[*** 已屏蔽：检测到疑似密钥泄露]"

_secrets: set[str] | None = None


def _collect_secrets() -> set[str]:
    """收集全部敏感值：密钥本身 + 前 8 字符前缀（防截断泄露）。"""
    values: set[str] = set()

    def add(v: str | None) -> None:
        if not v or len(v) < 8:
            return
        values.add(v)
        values.add(v[:8])

    add(_settings.secret_key)
    add(_settings.deepseek_api_key)
    add(_settings.zhipu_api_key)
    # F4（红队二次）：漏遮罩的密钥字段——红队链 2 组合 F1 穿越可 grep .env 读到 AGNES_API_KEY 全值
    add(_settings.agnes_api_key)
    add(_settings.alert_sign_secret)
    add(_settings.redis_password)
    # 数据库 URL 中的密码段（postgresql+asyncpg://user:pass@host/db）
    for url in (_settings.global_db_url, _settings.dept_db_url_template, _settings.ceo_db_url):
        if url and "://" in url:
            m = re.search(r"://([^:/@]+):([^@]+)@", url)
            if m:
                add(m.group(2))
    return values


# 通用密钥形态（sk- 开头长串），防模型重新拼装；不做裸 "sk-"（会误伤 task- 等）
_KEY_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{10,}"),
]

# SEC-26（2026-08-17）：LLM 输出侧恶意载荷屏蔽（OWASP LLM05）——命令注入/SQL RCE/eval 等
# 攻击载荷形态文本替换为占位符。平台自身无执行面（输出仅文本），此为防"用户复制执行"与
# 与 SEC-04（html_report XSS）联动放大；命中替换而非阻断（保持正常教学代码示例的可读性权衡）。
_MALICIOUS_MASK = "[*** 已拦截：疑似恶意代码载荷]"
_MALICIOUS_PATTERNS = [
    # 命令注入（分隔符 + 危险命令共现；收窄避免误伤普通文本；curl|bash 单管道执行形态覆盖）
    # 2026-08-26：bash/sh 后必须跟空格/-c/引号/行尾才命中（原 (?:\s+-c)? 可选——
    # ";shiduan" 表名被误拦成 "iduan"；改 \s+-c 必填又漏 "curl | bash" 单管道形态，加前瞻收口）
    re.compile(r"(?i)(;|\||&&)\s*(rm\s+-rf|curl|wget|nc\s|ncat|python3?\s+-c|(?:bash|sh)(?:\s+-c)?(?=\s|[\"'`]|$))"),
    # eval/exec/system/popen/subprocess 调用形态
    re.compile(r"(?i)\b(eval|exec|system|popen|os\.system|subprocess\.(?:call|run|Popen))\s*\("),
    # Python 内省/反序列化（代码执行原语）
    re.compile(r"(?i)\b(__import__|__builtins__|pickle\.loads)\b"),
    # SQL 破坏性 DML + PostgreSQL COPY TO PROGRAM RCE（教学示例可能误伤——已列入能力限制清单）
    re.compile(r"(?i)\b(drop|truncate|delete\s+from|insert\s+into)\b"),
    re.compile(r"(?i)\bcopy\s+[a-z0-9_.\"]+\s+to\s+program\b"),
    # 编码载荷解码/反序列化入口
    re.compile(r"(?i)\b(base64\.(?:b64decode|decodebytes)|yaml\.load)\s*\("),
]


# 动态敏感值（运行时登记，如 skill 内部令牌）——E-05：记录过期时间，guard_output 惰性清理
_dynamic_secrets: dict[str, float] = {}


def register_secret(value: str, ttl_seconds: float = 900) -> None:
    """登记运行时动态敏感值（M20：skill 内部令牌进敏感集合；TTL 后自动注销防集合膨胀）。"""
    if value and len(value) >= 8:
        _dynamic_secrets[value] = time.time() + ttl_seconds


def unregister_secret(value: str) -> None:
    """立即注销动态敏感值（显式消费/吊销场景）。"""
    _dynamic_secrets.pop(value, None)


def _prune_dynamic() -> None:
    """E-05：惰性清理过期动态项（每次匹配前调用，成本 O(n) 且只清已过期）。"""
    now = time.time()
    expired = [v for v, exp in _dynamic_secrets.items() if exp < now]
    for v in expired:
        _dynamic_secrets.pop(v, None)


def sensitive_values() -> set[str]:
    global _secrets
    _prune_dynamic()
    if _secrets is None:
        _secrets = _collect_secrets()
    return _secrets | set(_dynamic_secrets.keys())


def guard_output(text: str) -> tuple[str, list[str]]:
    """过滤输出文本，返回 (过滤后文本, 命中的敏感值前缀列表)。

    精确值按最长优先替换（避免短前缀先替换导致长值无法命中）；
    再做通用密钥形态正则替换。
    """
    if not text:
        return text, []
    hits: list[str] = []
    filtered = text
    for secret in sorted(sensitive_values(), key=len, reverse=True):
        if secret in filtered:
            filtered = filtered.replace(secret, _MASK)
            hits.append(secret[:12] + "…")
    for pat in _KEY_PATTERNS:
        m = pat.search(filtered)
        if m:
            filtered = pat.sub(_MASK, filtered)
            hits.append("sk-…（密钥形态）")
    # SEC-26：恶意载荷屏蔽层（命中替换为占位符；config 开关可关）
    if _settings.output_guard_malicious:
        for pat in _MALICIOUS_PATTERNS:
            m = pat.search(filtered)
            if m:
                filtered = pat.sub(_MALICIOUS_MASK, filtered)
                hits.append(f"malicious: {pat.pattern[:40]}")
    return filtered, hits
