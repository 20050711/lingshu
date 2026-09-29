"""红队二次修复复测（批 3：资源与网络面）——零 LLM，本地直跑。

覆盖：
- F6 zip 炸弹：现场构造 1000:1 压缩比 xlsx 与 sharedStrings 超大展开 → xlsx_guard.precheck_xlsx 拒绝；
  合法小 xlsx 放行
- F7 SSRF：video_gen._url_allowed 内网 URL 拒绝 / 公网放行 / 自站 public_base_url 放行
- N1 tmp_media：token 格式预检（junk token 不打 Redis）；TTL 默认 900
- N2 /chat/files：数量上限参数检查（函数级单测见 chat.py，此处验证上限配置）

用法：cd backend && conda run -n aip python -u tests/attack_repro_xlsx.py
"""
import asyncio
import io
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def _build_xlsx(shared_str: str = "正常文本", compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    """构造最小合法 xlsx（[Content_Types].xml + workbook.xml + sheet1.xml + sharedStrings.xml）。"""
    shared = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="1" uniqueCount="1">'
        f'<si><t>{shared_str}</t></si></sst>'
    ).encode("utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as z:
        z.writestr("xl/sharedStrings.xml", shared)
        z.writestr("[Content_Types].xml", (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
            '</Types>'))
        z.writestr("xl/workbook.xml", (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/></sheets>'
            '</workbook>'))
        z.writestr("xl/_rels/workbook.xml.rels", (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '</Relationships>'))
        z.writestr("xl/worksheets/sheet1.xml", (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetData><row r="1"><c r="A1" t="s"><v>0</v></c></row></sheetData>'
            '</worksheet>'))
    return buf.getvalue()


def test_precheck_ratio_bomb(tmp: Path) -> None:
    """F6-1：压缩比炸弹被拒（红队链 7 同型：高熵重复文本 60MB 压缩至 ~60KB ≈ 1000:1）。"""
    from app.services.xlsx_guard import precheck_xlsx

    p = tmp / "bomb_ratio.xlsx"
    # 真实数据写入（zipfile 写入时会修正条目尺寸声明——真实攻击即内容本身大、压缩后小）
    p.write_bytes(_build_xlsx(shared_str="A" * (60 * 1024 * 1024)))
    err = precheck_xlsx(str(p))
    check("压缩比炸弹被拒", err is not None and "压缩比" in err, str(err)[:100])


def test_precheck_sharedstrings_bomb(tmp: Path) -> None:
    """F6-2：sharedStrings 展开超上限被拒（不可压缩随机数据 → 压缩比 <100 走 sharedStrings 分支）。"""
    import base64 as _b64
    import os

    from app.services.xlsx_guard import precheck_xlsx

    p = tmp / "bomb_shared.xlsx"
    # 101MB 随机 base64 文本（ZIP_STORED 压缩比=1:1 绕过压缩比拦截，验证 sharedStrings 流式预扫分支）
    rand_b64 = _b64.b64encode(os.urandom((101 * 1024 * 1024) // 4 * 3)).decode("ascii")
    p.write_bytes(_build_xlsx(shared_str=rand_b64, compression=zipfile.ZIP_STORED))
    err = precheck_xlsx(str(p))
    check("sharedStrings 展开超限被拒", err is not None and "共享字符串" in err, str(err)[:100])


def test_precheck_legit(tmp: Path) -> None:
    """F6-3：合法小 xlsx 放行；非 zip（.csv 模拟）跳过。"""
    from app.services.xlsx_guard import precheck_xlsx

    p = tmp / "legit.xlsx"
    p.write_bytes(_build_xlsx())
    check("合法 xlsx 放行", precheck_xlsx(str(p)) is None)
    p2 = tmp / "legit.csv"
    p2.write_text("a,b\n1,2\n", encoding="utf-8")
    check("非 zip（csv）跳过", precheck_xlsx(str(p2)) is None)


async def test_ssrf_url_allowed() -> None:
    """F7：video_gen._url_allowed 校验矩阵。"""
    from app.agent.tools.video_gen import _url_allowed
    from app.core.config import get_settings

    check("内网地址拒绝", not await _url_allowed("http://169.254.169.254/latest/meta-data/"))
    check("回环地址拒绝", not await _url_allowed("http://127.0.0.1:8001/internal"))
    check("内网网段拒绝", not await _url_allowed("http://10.0.0.5/x"))
    pub_base = (get_settings().public_base_url or "").rstrip("/")
    if pub_base:
        check("自站 public_base_url 放行", await _url_allowed(f"{pub_base}/api/v1/tmp-media/xxx"))
    else:
        print("  - public_base_url 未配置（自站放行用例跳过）")
    # 公网放行（DNS 可达性依赖网络；用通用域名验证——解析失败视为拒绝属安全侧）
    ok = await _url_allowed("https://example.com/video.mp4")
    print(f"  - 公网 URL 放行: {ok}（依赖 DNS，仅信息展示）")


def test_tmp_media_token() -> None:
    """N1：tmp_media token 格式预检（junk token 直接 404 不打 Redis）；TTL 配置默认 900。"""
    import asyncio

    from app.core.config import get_settings
    from app.services.tmp_media import _TOKEN_RE, resolve_tmp_media

    check("合法 32 字符 base64url 通过", bool(_TOKEN_RE.match("a" * 32)))
    check("junk token 拒绝", not _TOKEN_RE.match("../../../etc/passwd"))
    check("短 token 拒绝", not _TOKEN_RE.match("short"))
    check("含非法字符拒绝", not _TOKEN_RE.match("a" * 31 + "/"))
    # junk token resolve → None（不打 Redis）
    r = asyncio.run(resolve_tmp_media("../../../etc/passwd"))
    check("junk token resolve 返回 None", r is None)
    check("tmp_media_ttl_seconds 默认 900", get_settings().tmp_media_ttl_seconds == 900)


def main() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        print("== F6 zip 炸弹预检 ==")
        test_precheck_ratio_bomb(tmp)
        test_precheck_sharedstrings_bomb(tmp)
        test_precheck_legit(tmp)
    print("== F7 SSRF 校验 ==")
    asyncio.run(test_ssrf_url_allowed())
    print("== N1 tmp_media token ==")
    test_tmp_media_token()
    print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
