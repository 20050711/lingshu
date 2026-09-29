"""R1（红队三修复）：每日轮换 JWT 签名密钥（双密钥窗口）。

逻辑（幂等，可手动/ cron 重跑）：
1. 读 backend/.env（逐行保序，其余行不动）
2. 现 SECRET_KEY 移到 SECRET_KEY_PREV；SECRET_KEY_PREV_SINCE=今天
   （prev 已存在且距今 >8 天 → 清空 prev——旧 token 已自然过期，窗口关闭）
3. 新 SECRET_KEY = secrets.token_urlsafe(64)（>=32 字符，过 fail-fast）
4. 写回 .env（缺失行追加，含注释头）

重启 uvicorn 由调用方（rotate_secret.sh）负责——本脚本只改配置。
"""
from __future__ import annotations

import secrets
from datetime import date, datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"

WINDOW_DAYS = 8  # prev 保留窗口（token 有效期 7 天，留 1 天余量）


def _load_lines() -> list[str]:
    return ENV_FILE.read_text(encoding="utf-8").splitlines()


def _find(lines: list[str], key: str) -> int | None:
    for i, line in enumerate(lines):
        if line.startswith(f"{key}="):
            return i
    return None


def main() -> None:
    lines = _load_lines()

    old_active = ""
    idx_active = _find(lines, "SECRET_KEY")
    if idx_active is not None:
        old_active = lines[idx_active].split("=", 1)[1].strip()

    # prev 清理：超窗口则丢弃
    prev_since = ""
    idx_since = _find(lines, "SECRET_KEY_PREV_SINCE")
    if idx_since is not None:
        prev_since = lines[idx_since].split("=", 1)[1].strip()
    keep_prev = True
    if prev_since:
        try:
            since = datetime.fromisoformat(prev_since).date()
            if (date.today() - since).days > WINDOW_DAYS:
                keep_prev = False
        except ValueError:
            keep_prev = False  # 非法日期 → 不保留

    new_active = secrets.token_urlsafe(64)
    today = date.today().isoformat()

    def upsert(lines: list[str], key: str, value: str) -> None:
        idx = _find(lines, key)
        if idx is not None:
            lines[idx] = f"{key}={value}"
        else:
            lines.append(f"{key}={value}")

    upsert(lines, "SECRET_KEY", new_active)
    upsert(lines, "SECRET_KEY_PREV", old_active if keep_prev else "")
    upsert(lines, "SECRET_KEY_PREV_SINCE", today if keep_prev else "")

    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[rotate_secret] SECRET_KEY 已轮换: 新 active 86 字符; prev={'保留(' + prev_since + ')' if keep_prev and old_active else '清空'}")


if __name__ == "__main__":
    main()
