"""
扫描版 PDF OCR 兜底：pypdfium2 渲染 → 火山引擎通用 OCR（OCRNormal）→ 拼接全文。

教学说明：
ocr_pdf(path)               ← 唯一对外公开的函数（入口）
    │
    ├── 1. _render_pdf_to_pngs()   PDF 每页渲染为 PNG bytes
    ├── 2. _ocr_image_bytes()      逐页调用火山 OCRNormal
    └── 3. 按页拼接为整篇文本
"""

import base64
from io import BytesIO
from pathlib import Path

import pypdfium2 as pdfium
from volcengine.visual.VisualService import VisualService

from app.core.settings import settings

# 业务固定常量
# ============================================================

# 150 DPI 是 OCR 效果与速度的折中点；pypdfium2 的 scale 基准是 72 DPI。
RENDER_DPI = 150
RENDER_SCALE = RENDER_DPI / 72  # ≈ 2.083

# 火山通用 OCR 接口名。
_OCR_ACTION = "OCRNormal"


# ============================================================
# 1) 火山 VisualService 客户端（懒加载 + 凭证校验）
# ============================================================

def _get_visual_service() -> VisualService:
    """构造一个带凭证与地域的 VisualService；缺凭证立即报错，避免网络层超时混淆问题。"""
    if not settings.volc_access_key or not settings.volc_secret_key:
        raise RuntimeError("VOLC_ACCESS_KEY / VOLC_SECRET_KEY 未配置，无法调用火山 OCR")

    service = VisualService()
    service.set_ak(settings.volc_access_key)
    service.set_sk(settings.volc_secret_key)
    # 注：visual 服务多数地域统一走 cn-north-1，。
    if hasattr(service, "set_region"):
        service.set_region(settings.volc_region)
    return service


# ============================================================
# 2) PDF 渲染：每页一张 PNG bytes
# ============================================================

def _render_pdf_to_pngs(path: Path) -> list[bytes]:
    """用 pypdfium2 把 PDF 每一页渲染成 PNG 字节流。"""
    pdf = pdfium.PdfDocument(str(path))
    images: list[bytes] = []
    try:
        for page_index in range(len(pdf)):
            page = pdf[page_index]
            # pypdfium2 的 render 返回位图对象，再 .to_pil() 转成 PIL.Image（需装 Pillow）。
            pil_image = page.render(scale=RENDER_SCALE).to_pil()
            buf = BytesIO()
            pil_image.save(buf, format="PNG")
            images.append(buf.getvalue())
            page.close()
    finally:
        pdf.close()
    return images


# ============================================================
# 3) 火山 OCRNormal 单图调用
# ============================================================

def _ocr_image_bytes(service: VisualService, image_bytes: bytes) -> str:
    """
    单张图片调用火山通用 OCR；按 image_base64 上送，拼接所有 line text。

    返回：识别出的纯文本（无版面坐标）。
    """
    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    form = {"image_base64": image_b64}

    resp = service.ocr_api(_OCR_ACTION, form)

    # 火山 SDK 正常路径返回 dict，且 code 在 ResponseMetadata 之外的 data 里。
    data = resp.get("data") if isinstance(resp, dict) else None
    if not data:
        raise RuntimeError(f"火山 OCR 返回异常：{resp}")

    # OCRNormal 字段：line_texts（按行的纯文本数组）。
    line_texts = data.get("line_texts") or []
    return "\n".join(t for t in line_texts if t)


# ============================================================
# 4) 对外入口：扫描版 PDF → 全文文本
# ============================================================

def ocr_pdf(path: Path) -> str:
    """
    扫描版 PDF OCR 兜底入口：渲染每页为 PNG，逐页调火山 OCR，按页拼接。
    """
    print(f"[RAG][OCR] === 进入扫描版兜底，file={path.name} ===")
    images = _render_pdf_to_pngs(path)
    print(f"[RAG][OCR] 渲染完成，共 {len(images)} 页")
    if not images:
        return ""

    service = _get_visual_service()
    page_texts: list[str] = []
    for idx, img in enumerate(images, start=1):
        text = _ocr_image_bytes(service, img)
        page_texts.append(text)
        print(f"[RAG][OCR] 第 {idx}/{len(images)} 页识别完成，文本长度={len(text)}")

    full_text = "\n".join(t for t in page_texts if t)
    print(f"[RAG][OCR] === 兜底完成，总文本长度={len(full_text)} ===")
    return full_text
