"""M5b 补充验证：file_parse（docx/pptx/pdf，原 doc_parse 四期已合并）+ image_recognition（GLM 视觉）。"""
import asyncio
import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.tools import ToolContext, get_all_tools, get_tool

CTX = ToolContext("smoke2", 1, "demo", "employee", "smoke2", "/data/outputs/smoke2/1")


async def main() -> int:
    results = []

    # 0. 工具注册清单（期望 ≥10；一期 10 → 二期 13 → 三期 14，避免枚举过期断言）
    names = [t.name for t in get_all_tools()]
    print("已注册工具:", names)
    results.append(("工具注册齐全(>=10)", len(names) >= 10, len(names)))

    # 1. file_parse（原 doc_parse，四期已合并）：用 PyMuPDF 生成测试 docx/pdf → markitdown 解析
    t = get_tool("file_parse")
    docx_path = "/data/outputs/smoke2/1/test.docx"
    Path(docx_path).parent.mkdir(parents=True, exist_ok=True)
    from docx import Document

    d = Document()
    d.add_heading("周报测试", 0)
    d.add_paragraph("消费 17132 元")
    d.save(docx_path)
    r = await t.handler({"file_path": docx_path}, CTX)
    ok = isinstance(r, dict) and "markdown" in r and "周报测试" in r.get("markdown", "")
    results.append(("file_parse docx markitdown", ok, r.get("markdown", "")[:40] if isinstance(r, dict) else str(r)))

    # 2. file_parse: pdf（用 PyMuPDF 生成一个测试 pdf）→ markitdown
    pdf_path = "/data/outputs/smoke2/1/test.pdf"
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "AI Platform PDF Test Content")
    doc.save(pdf_path)
    doc.close()
    r = await t.handler({"file_path": pdf_path}, CTX)
    ok = isinstance(r, dict) and "markdown" in r and "PDF Test" in r.get("markdown", "")
    results.append(("file_parse pdf markitdown", ok, r.get("markdown", "")[:40] if isinstance(r, dict) else str(r)))

    # 3. image_recognition: 生成一张带文字的简单 PNG（Pillow）→ GLM 识别
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (400, 150), "#ffffff")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 28)
    except Exception:
        font = ImageFont.load_default()
    draw.text((30, 50), "Hello AI Platform 123", fill="#1a56db", font=font)
    img_path = "/data/outputs/smoke2/1/test.png"
    img.save(img_path)
    b64 = base64.b64encode(Path(img_path).read_bytes()).decode()

    t = get_tool("image_recognition")
    r = await t.handler({"image_data": b64, "recognition_type": "text"}, CTX)
    content = r.get("content", "") if isinstance(r, dict) else ""
    ok = isinstance(r, dict) and "error" not in r and ("Platform" in content or "123" in content)
    results.append(("image_recognition GLM", ok, content[:60]))

    for name, ok, info in results:
        print(f"  {'✓' if ok else '✗'} {name}: {info}")
    all_ok = all(ok for _, ok, _ in results)
    print("=== PASS ===" if all_ok else "=== FAIL ===")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
