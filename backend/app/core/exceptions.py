"""统一错误码与异常。

错误响应格式：{"error": {"code": "E001", "message": "..."}}
"""
from fastapi import HTTPException


class ErrorCodes:
    E001 = ("E001", "数据库连接失败")
    E002 = ("E002", "SQL 查询超时")
    E003 = ("E003", "文件解析失败")
    E004 = ("E004", "模板占位符缺失")
    E005 = ("E005", "API 限流")
    E006 = ("E006", "未授权访问")
    E007 = ("E007", "账号已禁用")
    E008 = ("E008", "数据已更新，当前会话变为只读")
    E009 = ("E009", "操作确认超时")
    E010 = ("E010", "工具调用被拒绝")
    E011 = ("E011", "参数校验失败")
    E012 = ("E012", "skill 令牌无效")
    E013 = ("E013", "skill 权限不足")
    E014 = ("E014", "工具不存在")
    E015 = ("E015", "skill 调用失败")
    E016 = ("E016", "Agent 执行异常")  # 2026-08-10：graph 内部异常（原误标 E005 限流；兼作全局 500 兜底码）


class AppError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400, extra: dict | None = None):
        self.code = code
        self.message = message
        self.status_code = status_code
        self.extra = extra  # 错误响应附加字段（如登录失败时携带 captcha_id/captcha_image）
        super().__init__(message)


def app_error(code: str, message: str, status_code: int = 400, extra: dict | None = None) -> AppError:
    return AppError(code, message, status_code, extra)


def http_error(e: AppError) -> HTTPException:
    return HTTPException(status_code=e.status_code, detail={"code": e.code, "message": e.message})
