"""用户输入安全检查（提示词注入/破甲攻击检测）。

设计：语义化词袋 + 组合匹配（触发词 × 目标词在窗口内共现），抽象度高，
换说法也能命中；再加独立危险单点词与结构标签检测，共 5 层：

1. 指令操控（role_reset）：覆盖/忽略/绕过类动词 × 系统对象（指令/规则/prompt…）
2. 敏感信息泄露（secret_leak）：泄露动词 × 密钥/密码/凭据/提示词（双向）
3. 角色越权（privilege_claim）：管理员/root/最高权限 × 扮演/指令/执行
4. 越权访问（cross_dept）：其他团队/全局/CEO × 数据/查询
5. 危险操作（dangerous_ops）：删除/写入/执行类 × 数据/表/库/文件/系统
6. 编码混淆（encoding_bypass）：base64/hex/反转/unicode × 指令词
7. thinking 标签注入（结构级）：<think>/<end_of_thinking> 等标签（含变体）
8. 独立危险词（单点）：破甲/越狱/jailbreak/prompt injection/developer mode 等
"""
from __future__ import annotations

import hashlib
import re

# ===== 语义词袋（抽象层）=====

_OVERRIDE = (
    r"忽略|无视|忘记|遗忘|跳过|覆盖|颠覆|推翻|override|ignore|forget|skip|bypass"
    r"|绕过|突破|劫持|注入|解除|解锁|unlock|不再遵守|不必遵守|不用遵守|丢开"
)
_SYSTEM_OBJ = (
    r"指令|规则|系统提示|提示词|prompt|system\s*prompt|instruction|rule|约束|限制"
    r"|安全(设置|机制)?|越狱|引导|设定|边界|原则|行为准则"
)
_LEAK_VERBS = (
    r"输出|告诉|展示|暴露|说出|查看|给我|导出|打印|透露|泄露|print|echo|show|display|reveal|expose|dump"
    r"|是什么|是多少|在哪|在哪里|多少"
)
_SECRET_OBJ = (
    r"密钥|密码|口令|凭据|secret|api[_\s-]*key|token|credential|系统提示词|内部配置"
    r"|数据库(密码|账号)|环境变量|private[_\s-]*key|access[_\s-]*key"
)
_PRIVILEGE = (
    r"管理员|root|admin|superuser|最高权限|开发者|系统层|上帝模式|无条件|超级用户|系统管理员"
)
_PRIV_VERBS = r"扮演|你是|你作为|成为|模拟|assume|act\s*as|simulate|伪装"
_DEPT_OBJ = r"其他团队|别的团队|所有团队|全局|跨团队|ceo|总裁"
_DEPT_VERBS = r"数据|查询|访问|查看|读取"
# 2026-08-14 收紧：v2 框架下「修改/更新报告、执行脚本、写入文件」是正常业务流程（B10），
# 原词表把 修改/更新/写入/执行/启动/关闭/重建/重置 纳入拦截导致正常消息误伤（实况：做 PPT 时
# 用户消息被 dangerous_ops 拦截）。仅保留真正破坏性动词 × 系统级目标；业务对象（文件/配置）剔除。
_DANGER_VERBS = (
    r"删除|drop|truncate|清空|销毁|格式化|抹掉|kill"
)
_DANGER_OBJ = r"数据|表|库|记录|系统|索引|备份"
_ENCODING = r"base64|hex\s*编码|rot13|反转|逆序|倒序|unicode\s*混淆|url\s*编码|编码后|encode"
_TAGS = (
    r"<\s*\/?\s*(think|thinking|reasoning|thought|end_of_thinking|lend_of_thinking"
    r"|end_of_turn|end_of_tool|end_of_response|system|user|assistant|tool|output)\s*>"
)
_SINGLE_DANGER = (
    r"破甲|越狱|jailbreak|prompt\s*injection|developer\s*mode|DAN\b|脱狱|解禁|绕过安全"
    r"|无视伦理|no\s*restrictions|uncensored"
)

# ===== 组合规则（触发词 × 目标词，窗口内共现）=====
INJECTION_PATTERNS: list[tuple[str, str]] = [
    ("role_reset", rf"(?i)({_OVERRIDE})[^。；\n]{{0,30}}({_SYSTEM_OBJ})"),
    ("secret_leak", rf"(?i)(({_LEAK_VERBS})[^。；\n]{{0,20}}({_SECRET_OBJ}))|(({_SECRET_OBJ})[^。；\n]{{0,20}}({_LEAK_VERBS}))"),
    ("privilege_claim", rf"(?i)({_PRIVILEGE})[^。；\n]{{0,25}}({_PRIV_VERBS}|{_SYSTEM_OBJ}|执行|输出)"),
    ("cross_dept", rf"(?i)({_DEPT_OBJ})[^。；\n]{{0,20}}({_DEPT_VERBS})"),
    ("dangerous_ops", rf"(?i)({_DANGER_VERBS})[^。；\n]{{0,20}}({_DANGER_OBJ})"),
    ("encoding_bypass", rf"(?i)({_ENCODING})[^。；\n]{{0,20}}({_SYSTEM_OBJ}|{_SECRET_OBJ}|指令|内容)"),
    # 结构级：thinking 标签（独立命中，含变体）
    ("thinking_tag", rf"(?i)({_TAGS})"),
    # 单点危险词（独立命中）
    ("jailbreak_word", rf"(?i)({_SINGLE_DANGER})"),
]


class InputFilter:
    @staticmethod
    def check(text: str, dept_id: str, allowed_tools: list[str] | None = None,
              role: str | None = None) -> dict:
        """检查结果：{passed, reason, rule, matched_text}。

        E-10(API，2026-08-10)：role="ceo" 时放行 cross_dept 规则——看全局数据是 CEO 的职责；
        其余注入规则照常。
        """
        skip_rules = {"cross_dept"} if role == "ceo" else set()
        for rule, pattern in INJECTION_PATTERNS:
            if rule in skip_rules:
                continue
            m = re.search(pattern, text)
            if m:
                return {
                    "passed": False,
                    "reason": "检测到疑似提示词注入/越权请求，已拦截。请描述具体业务需求。",
                    "rule": rule,
                    "matched_text": m.group(0)[:80],
                }
        return {"passed": True, "reason": "", "rule": ""}

    @staticmethod
    def hash_text(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
