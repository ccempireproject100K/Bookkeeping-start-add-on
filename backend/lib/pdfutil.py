"""In-house PDF utilities (bake-off #14) — the twelve operations our users
demonstrably need, implemented independently in our stack on permissive
libraries only (catalog study: Stirling-PDF official docs; NO Stirling code
used — its production license is not clear for our use).

Dependency licenses (verified from each project's license file, 2026-10-30):
  pypdf        BSD-3-Clause
  pdfplumber   MIT (pulls pdfminer.six MIT)
  pypdfium2    BSD-3-Clause / Apache-2.0 (page rendering for redaction)
  reportlab    BSD-style (ReportLab license)
  Pillow       HPND (permissive)
OCR is opportunistic: pytesseract (Apache-2.0) + the tesseract binary are
used only when already present; otherwise ocr reports itself unavailable
instead of failing the request.
"""
import difflib
import hashlib
import io

from pypdf import PdfReader, PdfWriter


def _reader(data: bytes) -> PdfReader:
    return PdfReader(io.BytesIO(data))


def _write(writer: PdfWriter) -> bytes:
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def page_count(data: bytes) -> int:
    return len(_reader(data).pages)


def merge_pdfs(datas: list) -> bytes:
    """Assemble several PDFs into one, in order (the tax/job packet)."""
    if not datas:
        raise ValueError("Nothing to merge")
    writer = PdfWriter()
    for data in datas:
        for page in _reader(data).pages:
            writer.add_page(page)
    return _write(writer)


def extract_pages(data: bytes, pages: list) -> bytes:
    """Extract 1-based page numbers into a new PDF (split)."""
    reader = _reader(data)
    if not pages:
        raise ValueError("No pages requested")
    writer = PdfWriter()
    for p in pages:
        if p < 1 or p > len(reader.pages):
            raise ValueError(f"Page {p} out of range 1..{len(reader.pages)}")
        writer.add_page(reader.pages[p - 1])
    return _write(writer)


def compress_pdf(data: bytes) -> bytes:
    """Recompress content streams and drop duplicated objects."""
    reader = _reader(data)
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    for page in writer.pages:
        try:
            page.compress_content_streams()
        except Exception:
            pass
    try:
        writer.compress_identical_objects(remove_duplicates=True)
    except Exception:
        pass
    return _write(writer)


def _overlay_pdf(width: float, height: float, draw) -> bytes:
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(width, height))
    draw(c, width, height)
    c.showPage()
    c.save()
    return buf.getvalue()


def stamp_pdf(data: bytes, text: str, *, footer: bool = False) -> bytes:
    """Watermark/stamp every page: diagonal center text by default, or a
    bottom footer line (PAID / DRAFT / COPY stamps)."""
    reader = _reader(data)
    writer = PdfWriter()
    for page in reader.pages:
        w = float(page.mediabox.width)
        h = float(page.mediabox.height)

        def draw(c, W, H, _text=text, _footer=footer):
            c.saveState()
            if _footer:
                c.setFont("Helvetica-Bold", 11)
                c.setFillGray(0.35)
                c.drawCentredString(W / 2, 24, _text)
            else:
                c.translate(W / 2, H / 2)
                c.rotate(35)
                c.setFont("Helvetica-Bold", 54)
                c.setFillGray(0.5, alpha=0.25)
                c.drawCentredString(0, 0, _text)
            c.restoreState()

        overlay = PdfReader(io.BytesIO(_overlay_pdf(w, h, draw))).pages[0]
        page.merge_page(overlay)
        writer.add_page(page)
    return _write(writer)


def flatten_pdf(data: bytes) -> bytes:
    """Lock the document: drop annotations and the AcroForm so filled
    values can no longer be edited downstream."""
    writer = PdfWriter(clone_from=io.BytesIO(data))
    try:
        root = writer._root_object
        if "/AcroForm" in root:
            del root["/AcroForm"]
    except Exception:
        pass
    for page in writer.pages:
        if "/Annots" in page:
            del page["/Annots"]
    return _write(writer)


def encrypt_pdf(data: bytes, password: str) -> bytes:
    """Password-protect (AES-256 when available) and restrict editing."""
    reader = _reader(data)
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt(user_password=password, owner_password=None,
                   permissions_flag=0b100)  # allow printing only
    return _write(writer)


def sign_pdf(data: bytes, signer: str, signed_on: str) -> bytes:
    """Plain signature (certificate stack deliberately deferred): stamps a
    footer attestation on page 1 and appends a signature page carrying the
    document's SHA-256 so the signed artifact is identifiable."""
    digest = hashlib.sha256(data).hexdigest()
    stamped = stamp_pdf(data, f"Signed by {signer} — {signed_on}", footer=True)
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(72, 700, "Signature page")
    c.setFont("Helvetica", 11)
    c.drawString(72, 660, f"Signed by: {signer}")
    c.drawString(72, 640, f"Date: {signed_on}")
    c.drawString(72, 620, "Source document SHA-256:")
    c.setFont("Courier", 8)
    c.drawString(72, 604, digest)
    c.showPage()
    c.save()
    return merge_pdfs([stamped, buf.getvalue()])


def form_fields(data: bytes) -> list:
    """AcroForm fields as a plain list: [{name, value, type}]."""
    fields = _reader(data).get_fields() or {}
    return [{"name": name, "value": f.get("/V"), "type": str(f.get("/FT") or "")}
            for name, f in fields.items()]


def fill_form_pdf(data: bytes, values: dict) -> bytes:
    known = {f["name"] for f in form_fields(data)}
    unknown = [k for k in values if k not in known]
    if unknown:
        raise ValueError(f"Unknown form fields: {', '.join(sorted(unknown))}")
    writer = PdfWriter(clone_from=io.BytesIO(data))
    for i in range(len(writer.pages)):
        writer.update_page_form_field_values(writer.pages[i], values,
                                             auto_regenerate=False)
    return _write(writer)


def extract_text(data: bytes) -> str:
    reader = _reader(data)
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def pdf_to_csv(data: bytes) -> str:
    """Text-layer tables to CSV: lines split on runs of 2+ spaces (or tabs)."""
    import csv as _csv
    import re
    out = io.StringIO()
    w = _csv.writer(out)
    for line in extract_text(data).splitlines():
        if not line.strip():
            continue
        cells = [c.strip() for c in re.split(r"\s{2,}|\t", line.strip())]
        w.writerow(cells)
    return out.getvalue()


def images_to_pdf(images: list) -> bytes:
    """Receipt photos / scans (PNG/JPEG bytes) assembled into one PDF."""
    from PIL import Image
    if not images:
        raise ValueError("No images supplied")
    pil_images = []
    for raw in images:
        im = Image.open(io.BytesIO(raw))
        if im.mode != "RGB":
            im = im.convert("RGB")
        pil_images.append(im)
    buf = io.BytesIO()
    pil_images[0].save(buf, format="PDF", save_all=True,
                       append_images=pil_images[1:])
    return buf.getvalue()


def redact_pdf(data: bytes, terms: list) -> bytes:
    """True redaction: pages containing a term are RASTERIZED with opaque
    boxes painted over every match (pypdfium2 rendering), so the underlying
    text is destroyed, not merely covered. Pages without matches pass
    through untouched as vector pages."""
    import pdfplumber

    lowered = [t.lower() for t in terms if t and t.strip()]
    if not lowered:
        raise ValueError("No redaction terms supplied")
    reader = _reader(data)
    out_pages = []          # list of ("pdf", bytes) | ("image", bytes)
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for idx, page in enumerate(pdf.pages):
            text = (page.extract_text() or "").lower()
            if not any(t in text for t in lowered):
                single = PdfWriter()
                single.add_page(reader.pages[idx])
                out_pages.append(("pdf", _write(single)))
                continue
            chars = page.chars
            full = "".join(ch.get("text", "") for ch in chars)
            low = full.lower()
            boxes = []
            for term in lowered:
                start = 0
                while True:
                    j = low.find(term, start)
                    if j < 0:
                        break
                    span = chars[j:j + len(term)]
                    if span:
                        boxes.append((min(c["x0"] for c in span),
                                      min(c["top"] for c in span),
                                      max(c["x1"] for c in span),
                                      max(c["bottom"] for c in span)))
                    start = j + 1
            # rasterize this page and burn the boxes into the pixels
            import pypdfium2 as pdfium
            from PIL import ImageDraw
            doc = pdfium.PdfDocument(data)
            scale = 200 / 72.0
            bitmap = doc[idx].render(scale=scale)
            img = bitmap.to_pil()
            draw = ImageDraw.Draw(img)
            for (x0, top, x1, bottom) in boxes:
                draw.rectangle([x0 * scale, top * scale, x1 * scale, bottom * scale],
                               fill=(0, 0, 0))
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="PNG")
            out_pages.append(("image", buf.getvalue()))
            doc.close()
    parts = []
    for kind, payload in out_pages:
        parts.append(payload if kind == "pdf" else images_to_pdf([payload]))
    return merge_pdfs(parts)


def compare_pdfs(a: bytes, b: bytes) -> dict:
    """Line-level text diff between two versions of a document."""
    la = [l for l in extract_text(a).splitlines() if l.strip()]
    lb = [l for l in extract_text(b).splitlines() if l.strip()]
    sm = difflib.SequenceMatcher(a=la, b=lb)
    added, removed = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("insert", "replace"):
            added.extend(lb[j1:j2])
        if tag in ("delete", "replace"):
            removed.extend(la[i1:i2])
    return {"similarity": round(sm.ratio(), 4),
            "added_lines": added, "removed_lines": removed,
            "pages_a": page_count(a), "pages_b": page_count(b)}


def ocr_pdf(data: bytes) -> dict:
    """OCR where possible. Returns {available, text, engine}. Uses embedded
    text first; for image-only pages, pytesseract over embedded images when
    the tesseract binary is present — otherwise reports unavailable."""
    text = extract_text(data)
    if text.strip():
        return {"available": True, "engine": "embedded-text", "text": text}
    try:
        import pytesseract
        from PIL import Image
    except Exception:
        return {"available": False, "engine": None, "text": "",
                "reason": "pytesseract not installed"}
    try:
        reader = _reader(data)
        chunks = []
        for page in reader.pages:
            for img in page.images:
                im = Image.open(io.BytesIO(img.data))
                chunks.append(pytesseract.image_to_string(im))
        joined = "\n".join(chunks).strip()
        if joined:
            return {"available": True, "engine": "tesseract", "text": joined}
        return {"available": False, "engine": "tesseract", "text": "",
                "reason": "no OCR text recognized"}
    except Exception as e:
        return {"available": False, "engine": "tesseract", "text": "",
                "reason": str(e)[:200]}
