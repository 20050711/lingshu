"""安全模块：bcrypt 密码、JWT 签发校验、算术验证码、IP 登录防护。

- JWT HS256，含 jti，7 天有效，Redis 黑名单支持登出
- 验证码：4 位算术题（200x60 PNG base64），5 分钟一次性
- 防暴力破解：同 IP 失败 >=3 次要求验证码；>=10 次封 24h
"""
from __future__ import annotations

import base64
import io
import math
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt as _bcrypt
import jwt

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.redis import redis_delete, redis_get, redis_incr, redis_set

_settings = get_settings()
logger = get_logger("core.security")


# ===== 密码 =====

def validate_password(pwd: str) -> str | None:
    """密码规则统一校验（2026-09-01 方案：8-16 位）；返回错误文案或 None（合法）。
    应用点：用户自助改密（PUT /auth/account）、admin 建用户、admin 重置密码。"""
    if not pwd or not (8 <= len(pwd) <= 16):
        return "密码需 8-16 位"
    return None


def hash_password(raw: str) -> str:
    return _bcrypt.hashpw(raw.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")


def verify_password(raw: str, hashed: str) -> bool:
    try:
        return _bcrypt.checkpw(raw.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


# ===== JWT =====

# SEC-17（2026-08-17）：固定 iss/aud（本项目仅一个签发方/受众），防跨应用 token 复用
_JWT_ISSUER = "ai-platform"
_JWT_AUDIENCE = "ai-platform"


def create_token(user_id: int, username: str, role: str, dept_id: str, token_version: int = 0) -> tuple[str, str]:
    """返回 (token, jti)。ver = users.token_version（L21：登出/吊销 +1 后旧 token 全部失效）。"""
    jti = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "dept_id": dept_id,
        "jti": jti,
        "ver": token_version,
        "iss": _JWT_ISSUER,
        "aud": _JWT_AUDIENCE,
        "iat": now,
        "exp": now + timedelta(days=_settings.jwt_expire_days),
    }
    token = jwt.encode(payload, _settings.secret_key, algorithm="HS256")
    return token, jti


async def decode_token(token: str) -> dict | None:
    """SEC-17：强制校验 iss/aud——旧格式（无 iss/aud）与跨应用 token 一律拒绝（存量 token 失效，需重登）。

    R1（红队三修复）：双密钥窗口——先试 active（secret_key），失败再试 prev（上一枚，
    轮换后 8 天窗口内旧 token 仍有效）；签发始终用 active。
    """
    keys = [k for k in (_settings.secret_key, _settings.secret_key_prev) if k]
    for key in keys:
        try:
            payload = jwt.decode(
                token, key, algorithms=["HS256"],
                audience=_JWT_AUDIENCE, issuer=_JWT_ISSUER,
                options={"verify_aud": True, "verify_iss": True},
            )
            return payload  # L21：吊销校验在 middleware（对比 users.token_version + status），不再依赖 jti 黑名单
        except jwt.PyJWTError:
            continue
    return None


# ===== 验证码 =====

# F17（红队二次，2026-08-18）：验证码强化——移植用户提供的强验证码方案
# （F:\脚本导出\test_yanzheng.py）：两位数(10-99)加减乘 + 波浪扭曲 + 字符级旋转 + 微断点 +
# 贝塞尔细干扰线 + 灰度/背景双层噪点 + 高斯模糊（原 4 位个位数算术无扭曲可 OCR 批量解）。
# 调整点：random→secrets（密钥无关但保持项目风格）、字体用 DejaVu（WSL 无 arial.ttf）、
# 删除调试写文件、答案保证非负（'-' 时 a≥b）。答案仍为纯数字——前端输入框零改动
# （登录页 img height:40 固定、宽度自适应，260x100 等比缩放不变形）。
_CAPTCHA_W = 260
_CAPTCHA_H = 100
# 2026-09-01：字体路径进 config（CAPTCHA_FONT_PATH env 可覆盖；原硬编码 DejaVu 绝对路径，精简系统缺字体时验证码直接失败）
_CAPTCHA_FONT = _settings.captcha_font_path


def _captcha_expression() -> tuple[str, str]:
    """两位数加减乘算式，返回 (算式文本, 数字答案)。答案恒非负。"""
    a, b = secrets.randbelow(90) + 10, secrets.randbelow(90) + 10
    op = secrets.choice(["+", "-", "×"])
    if op == "-" and a < b:
        a, b = b, a
    expr = f"{a} {op} {b} = ?"
    answer = str(a + b if op == "+" else a - b if op == "-" else a * b)
    return expr, answer


def _captcha_wave_distortion(image, amp_x=4, period_x=40, amp_y=2, period_y=35):
    """波浪扭曲（逐像素偏移）。"""
    from PIL import Image

    w, h = image.size
    result = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    for y in range(h):
        offset_x = int(amp_x * math.sin(2 * math.pi * y / period_x))
        for x in range(w):
            offset_y = int(amp_y * math.sin(2 * math.pi * x / period_y))
            src_x, src_y = x - offset_x, y - offset_y
            if 0 <= src_x < w and 0 <= src_y < h:
                result.putpixel((x, y), image.getpixel((src_x, src_y)))
    return result


def _captcha_draw_text_layer(font, text, color):
    """绘制文字层：字符级随机旋转 ±10° + 轻微重叠/缝隙。"""
    from PIL import Image as _PILImage, ImageDraw

    dummy = _PILImage.new("RGBA", (_CAPTCHA_W * 2, _CAPTCHA_H * 2), (0, 0, 0, 0))
    d_draw = ImageDraw.Draw(dummy)
    d_draw.text((0, 0), text, font=font, fill=(0, 0, 0, 255))
    bbox = dummy.getbbox()
    if not bbox:
        return None
    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    layer = _PILImage.new("RGBA", (text_w + 50, text_h + 50), (0, 0, 0, 0))
    x_cursor = 10
    for ch in text:
        ch_bbox = d_draw.textbbox((0, 0), ch, font=font)
        ch_w = ch_bbox[2] - ch_bbox[0]
        char_img = _PILImage.new("RGBA", (ch_w + 10, ch_w + 10), (0, 0, 0, 0))
        char_draw = ImageDraw.Draw(char_img)
        angle = secrets.randbelow(21) - 10
        char_draw.text((5, 5), ch, font=font, fill=color)
        char_img = char_img.rotate(angle, expand=1, resample=_PILImage.BICUBIC)
        overlap = secrets.randbelow(3) - 1  # [-1, 1]
        paste_y = secrets.randbelow(7) - 3 + 5
        layer.paste(char_img, (x_cursor, paste_y), char_img)
        x_cursor += ch_w + overlap
    layer = layer.crop(layer.getbbox())
    return layer


def _captcha_add_micro_gaps(canvas, text_bbox, bg_color):
    """笔画随机微断点（背景色小圆）。"""
    from PIL import Image as _PILImage, ImageDraw

    gap_layer = _PILImage.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(gap_layer)
    tx_min, ty_min, tx_max, ty_max = text_bbox
    for _ in range(secrets.randbelow(11) + 10):
        x = secrets.randbelow(max(tx_max - tx_min, 1)) + tx_min
        y = secrets.randbelow(max(ty_max - ty_min, 1)) + ty_min
        r = secrets.randbelow(2) + 1
        draw.ellipse([x - r, y - r, x + r, y + r], fill=(*bg_color, 255))
    return _PILImage.alpha_composite(canvas, gap_layer)


def _captcha_draw_thin_curves(canvas, text_bbox, color_alpha):
    """细干扰曲线（贝塞尔，部分进入文字区域）+ 字符间隙小圆圈。"""
    from PIL import Image as _PILImage, ImageDraw

    draw = ImageDraw.Draw(canvas)
    tx_min, ty_min, tx_max, ty_max = text_bbox
    mid_y = (ty_min + ty_max) // 2

    for zone in [(ty_min - 8, ty_min + 15), (ty_max - 15, ty_max + 8)]:
        y_low, y_high = zone
        start_x = secrets.randbelow(max(tx_max - tx_min + 25, 1)) + tx_min - 15
        start_y = secrets.randbelow(max(y_high - y_low, 1)) + y_low
        end_x = secrets.randbelow(max(tx_max - tx_min + 25, 1)) + tx_min - 10
        end_y = secrets.randbelow(max(y_high - y_low, 1)) + y_low
        cp1 = (start_x + secrets.randbelow(41) - 20, start_y + secrets.randbelow(31) - 15)
        cp2 = (end_x + secrets.randbelow(41) - 20, end_y + secrets.randbelow(31) - 15)
        points = []
        for t in range(0, 101, 5):
            tt = t / 100.0
            x = (1 - tt) ** 3 * start_x + 3 * (1 - tt) ** 2 * tt * cp1[0] + 3 * (1 - tt) * tt ** 2 * cp2[0] + tt ** 3 * end_x
            y = (1 - tt) ** 3 * start_y + 3 * (1 - tt) ** 2 * tt * cp1[1] + 3 * (1 - tt) * tt ** 2 * cp2[1] + tt ** 3 * end_y
            points.append((int(x), int(y)))
        if len(points) >= 2:
            draw.line(points, fill=color_alpha, width=secrets.randbelow(2) + 1, joint="curve")

    for _ in range(secrets.randbelow(2) + 1):
        x1 = secrets.randbelow(max(tx_max - tx_min, 1)) + tx_min
        y1 = secrets.randbelow(max(ty_max - ty_min, 1)) + ty_min
        if abs(y1 - mid_y) < 10:
            y1 = mid_y + secrets.choice([-15, 15])
        x2 = x1 + secrets.randbelow(41) - 20
        y2 = y1 + secrets.randbelow(21) - 10
        points = []
        for t in range(0, 101, 10):
            tt = t / 100.0
            points.append((int(x1 + (x2 - x1) * tt + secrets.randbelow(7) - 3),
                           int(y1 + (y2 - y1) * tt + secrets.randbelow(5) - 2)))
        if len(points) >= 2:
            draw.line(points, fill=color_alpha, width=1, joint="curve")

    for _ in range(secrets.randbelow(5) + 4):
        cx = secrets.randbelow(max(tx_max - tx_min, 1)) + tx_min
        cy = secrets.randbelow(max(ty_max - ty_min, 1)) + ty_min
        if abs(cy - mid_y) < 8:
            continue
        r = secrets.randbelow(4) + 3
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=color_alpha, width=1)


def _captcha_gradient_noise(canvas, text_bbox):
    """灰度噪点（椭圆/线/点，半透明）。"""
    from PIL import Image as _PILImage, ImageDraw

    draw = ImageDraw.Draw(canvas)
    w, h = canvas.size
    tx_min, ty_min, tx_max, ty_max = text_bbox
    for _ in range(secrets.randbelow(16) + 20):
        x, y = secrets.randbelow(w), secrets.randbelow(h)
        gray = secrets.randbelow(103) + 85
        alpha = secrets.randbelow(121) + 60
        color = (gray, gray, gray, alpha)
        shape = secrets.choice(["ellipse", "line", "dot"])
        if shape == "ellipse":
            rx, ry = secrets.randbelow(8) + 1, secrets.randbelow(4) + 1
            draw.ellipse([x - rx, y - ry, x + rx, y + ry], fill=color)
        elif shape == "line":
            angle = secrets.randbelow(360)
            length = secrets.randbelow(10) + 3
            draw.line([(x, y), (x + length * math.cos(math.radians(angle)),
                                 y + length * math.sin(math.radians(angle)))], fill=color, width=secrets.randbelow(2) + 1)
        else:
            r = secrets.randbelow(6) + 2
            draw.ellipse([x - r, y - r, x + r, y + r], fill=color)


def _captcha_background_noise(image, intensity=12):
    """背景像素噪点（18% 概率 ±intensity 灰度扰动）。"""
    pixels = image.load()
    w, h = image.size
    for y in range(h):
        for x in range(w):
            r, g, b = pixels[x, y][:3]
            if secrets.randbelow(100) < 18:
                noise = secrets.randbelow(intensity * 2 + 1) - intensity
                pixels[x, y] = (max(0, min(255, r + noise)),
                                max(0, min(255, g + noise)),
                                max(0, min(255, b + noise)))


def generate_captcha() -> tuple[str, str]:
    """生成强算术验证码（两位数加减乘），返回 (answer, base64_png)。"""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    expr, answer = _captcha_expression()
    try:
        font = ImageFont.truetype(_CAPTCHA_FONT, 48)
    except OSError:
        font = ImageFont.load_default()

    text_color = (51, 51, 51)
    semi_transparent = (51, 51, 51, 150)

    # 1. 文字层（字符旋转 + 轻微重叠）→ 波浪扭曲
    text_layer = _captcha_draw_text_layer(font, expr, text_color)
    if text_layer is None:
        return answer, ""
    text_layer = _captcha_wave_distortion(
        text_layer,
        amp_x=secrets.randbelow(4) + 3,
        period_x=secrets.randbelow(16) + 35,
        amp_y=secrets.randbelow(3) + 1,
        period_y=secrets.randbelow(16) + 30,
    )

    # 2. 背景 + 放置文字
    bg_color = (secrets.randbelow(16) + 240, secrets.randbelow(16) + 240, secrets.randbelow(16) + 240)
    canvas = Image.new("RGBA", (_CAPTCHA_W, _CAPTCHA_H), (0, 0, 0, 0))
    canvas.paste(bg_color, [0, 0, _CAPTCHA_W, _CAPTCHA_H])
    tw, th = text_layer.size
    offset_x = (_CAPTCHA_W - tw) // 2 + secrets.randbelow(11) - 5
    offset_y = (_CAPTCHA_H - th) // 2 + secrets.randbelow(11) - 5
    canvas.paste(text_layer, (offset_x, offset_y), text_layer)
    text_bbox = (offset_x, offset_y, offset_x + tw, offset_y + th)

    # 3. 微断点 + 细干扰线 + 灰度噪点 + 背景噪点 + 高斯模糊
    canvas = _captcha_add_micro_gaps(canvas, text_bbox, bg_color)
    line_layer = Image.new("RGBA", (_CAPTCHA_W, _CAPTCHA_H), (0, 0, 0, 0))
    _captcha_draw_thin_curves(line_layer, text_bbox, semi_transparent)
    canvas = Image.alpha_composite(canvas, line_layer)
    noise_layer = Image.new("RGBA", (_CAPTCHA_W, _CAPTCHA_H), (0, 0, 0, 0))
    _captcha_gradient_noise(noise_layer, text_bbox)
    canvas = Image.alpha_composite(canvas, noise_layer)
    final_rgb = canvas.convert("RGB")
    _captcha_background_noise(final_rgb, intensity=10)
    final_rgb = final_rgb.filter(ImageFilter.GaussianBlur(radius=0.6))

    buf = io.BytesIO()
    final_rgb.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    return answer, b64


# ===== IP 提取（SEC-07，2026-08-17 修正） =====

def get_client_ip(request) -> str:
    """E-01(API)：反代场景取真实客户端 IP。

    SEC-07 修正（原 auth._client_ip 逻辑反了）：**仅当请求带内部反代标记头
    （X-Internal-Request: 1，由 deploy/aip.nginx 注入）且直连 IP ∈ TRUSTED_PROXIES 白名单时
    才信任 X-Forwarded-For**（取从右往左第一个不在白名单的 IP = 真实客户端）。
    双重条件堵死两类绕过：① 直连 :8000 伪造 XFF（无内部头）；② 直连 IP 不在白名单。
    注：同机直连与 nginx 反代 TCP 源同为 127.0.0.1，仅靠 IP 无法区分——内部头是部署侧约定
    （攻击者需先知道内部头约定才可伪造，超出"随手绕过"范畴；username 双因子仍为主防线）。
    供 auth 登录防爆破与限流（SEC-05 per-IP 维度）共用，避免 auth→rate_limit 循环导入。
    """
    direct = request.client.host if request.client else "unknown"
    internal = request.headers.get("x-internal-request") == "1"
    xff = request.headers.get("x-forwarded-for", "")
    if internal and _settings.trusted_proxies.strip():
        trusted = {p.strip() for p in _settings.trusted_proxies.split(",") if p.strip()}
        if xff and direct in trusted:
            hops = [h.strip() for h in xff.split(",") if h.strip()]
            for h in reversed(hops):
                if h not in trusted:
                    return h
        if direct in trusted:
            return direct  # 正常 nginx 路径（直连 IP 在白名单；xff 缺失时直连 IP 即真实客户端）
    # F18（红队二次）：X-Internal-Request 头伪造留痕——命中内部头但直连 IP 不在白名单
    #（或白名单未配置）即伪造/异常尝试，记 warning（正常路径不打日志不刷屏）。
    # 头名为纯部署约定（deploy/aip.nginx 注入），公网部署前需改随机头名或强制白名单校验。
    #
    # 2026-09-11（走查：运维日志被刷屏）：本机/回环来源且带内部头 = **nginx 反代但
    # TRUSTED_PROXIES 没配**（部署机 start.sh 会 export，开发机手动起后端常漏）——这是
    # 配置缺口不是伪造尝试，降为 debug；只有**非回环**来源带内部头才是真伪造信号，保留 warning。
    # （回环来源本身就是同机进程，谈不上"伪造边界"；真攻击者从外部来，direct 必不是 127.0.0.1/::1）
    if internal:
        if direct in ("127.0.0.1", "::1"):
            logger.debug("X-Internal-Request 头来自回环但 TRUSTED_PROXIES 未配置"
                         "（部署侧应 export TRUSTED_PROXIES=127.0.0.1）：direct=%s", direct)
        else:
            logger.warning("X-Internal-Request 伪造/异常尝试：direct=%s xff=%r trusted_proxies=%r",
                           direct, xff, _settings.trusted_proxies)
    return direct


# ===== IP 登录防护 =====

def _ip_key(ip: str, suffix: str, username: str = "") -> str:
    """E-01(API，2026-08-10)：失败计数/封禁键 = ip + username 双因子——
    反代部署下全站同源 IP，纯 IP 键会把任一攻击者（或连错密码的用户）的失败放大成全站
    共享（10 次封禁 = 全公司 24 小时无法登录）。双因子后只影响该 IP 尝试该账号的场景。"""
    return f"auth:{suffix}:{ip}" if not username else f"auth:{suffix}:{ip}:{username}"


async def record_login_failure(ip: str, username: str = "") -> int:
    """失败计数 +1，返回当前次数。"""
    return await redis_incr(_ip_key(ip, "fail", username), 3600)


async def get_login_failures(ip: str, username: str = "") -> int:
    """读取当前 IP+账号 登录失败计数（不递增）。S4：验证码强制状态机用。"""
    return int(await redis_get(_ip_key(ip, "fail", username)) or 0)


async def reset_login_failures(ip: str, username: str = "") -> None:
    await redis_delete(_ip_key(ip, "fail", username))


async def is_ip_blocked(ip: str, username: str = "") -> bool:
    return bool(await redis_get(_ip_key(ip, "block", username)))


async def block_ip(ip: str, username: str = "") -> None:
    await redis_set(_ip_key(ip, "block", username), "1", 86400)
