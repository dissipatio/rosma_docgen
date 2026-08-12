"""
rosma_docgen — Stage 1 (infrastructure only)

Purpose of this stage: prove that LibreOffice + Cyrillic fonts + DOCX->PDF
conversion actually work on Railway, BEFORE any real template exists.

What works now:
  GET  /health        - liveness
  GET  /fonts         - which fonts LibreOffice can actually see
  POST /smoke-test    - builds a Cyrillic DOCX, converts it, returns the PDF
  POST /render-test   - upload your own .docx template + JSON context, get PDF back
  POST /generate      - stub; wired up in Stage 2 once field mapping is done

Stage 2 adds: Airtable fetch, template lookup, context builder, upload-back.
"""

import json
import logging
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("docgen")

app = FastAPI(title="ROSMA docgen")

SOFFICE_TIMEOUT = int(os.getenv("SOFFICE_TIMEOUT", "120"))


# --------------------------------------------------------------------------
# LibreOffice conversion
# --------------------------------------------------------------------------

def docx_to_pdf(docx_path: Path, out_dir: Path) -> Path:
    """
    Convert a .docx to .pdf using headless LibreOffice.

    Each call gets its own UserInstallation profile directory. Without this,
    two concurrent conversions fight over the same profile and one of them
    silently produces nothing.
    """
    profile = Path(tempfile.gettempdir()) / f"lo_{uuid.uuid4().hex}"
    profile.mkdir(parents=True, exist_ok=True)

    cmd = [
        "soffice",
        "--headless",
        "--norestore",
        "--invisible",
        f"-env:UserInstallation=file://{profile}",
        "--convert-to", "pdf:writer_pdf_Export",
        "--outdir", str(out_dir),
        str(docx_path),
    ]

    log.info("Converting %s -> pdf", docx_path.name)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=SOFFICE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"LibreOffice timed out after {SOFFICE_TIMEOUT}s")
    finally:
        shutil.rmtree(profile, ignore_errors=True)

    pdf_path = out_dir / (docx_path.stem + ".pdf")

    # soffice frequently exits 0 even when it produced nothing, so check the file.
    if not pdf_path.exists():
        raise RuntimeError(
            f"LibreOffice produced no PDF.\n"
            f"returncode={proc.returncode}\n"
            f"stdout={proc.stdout}\n"
            f"stderr={proc.stderr}"
        )

    log.info("Converted OK: %s (%d bytes)", pdf_path.name, pdf_path.stat().st_size)
    return pdf_path


# --------------------------------------------------------------------------
# Diagnostics
# --------------------------------------------------------------------------

@app.get("/health")
def health():
    soffice = shutil.which("soffice")
    return {
        "status": "ok",
        "soffice": soffice,
        "soffice_found": bool(soffice),
    }


@app.get("/fonts")
def fonts():
    """
    Lists the font families fontconfig exposes to LibreOffice.

    If 'Liberation Serif' is missing here, Cyrillic text and Times New Roman
    substitution will both be wrong in the output PDF.
    """
    try:
        out = subprocess.run(
            ["fc-list", ":", "family"],
            capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception as e:
        raise HTTPException(500, f"fc-list failed: {e}")

    families = sorted({
        part.strip()
        for line in out.splitlines()
        for part in line.split(",")
        if part.strip()
    })

    expected = ["Liberation Serif", "Liberation Sans", "DejaVu Sans"]
    return {
        "total": len(families),
        "expected_present": {name: name in families for name in expected},
        "families": families,
    }


# --------------------------------------------------------------------------
# Smoke test — no template needed
# --------------------------------------------------------------------------

@app.post("/smoke-test")
def smoke_test():
    """
    Builds a small DOCX containing Cyrillic text in Times New Roman,
    converts it to PDF, and returns the PDF.

    Open the result and check: Cyrillic renders as letters (not boxes),
    and the serif font looks like Times, not a fallback.
    """
    from docx import Document
    from docx.shared import Pt
    from num2words import num2words

    tmp = Path(tempfile.mkdtemp())
    docx_path = tmp / "smoke.docx"

    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Times New Roman"
    style.font.size = Pt(12)

    doc.add_heading("Проверка кириллицы", level=1)
    doc.add_paragraph("Счёт на оплату № 123 от 12 августа 2026 г.")
    doc.add_paragraph("Поставщик: ООО «РОСМА» — штампы, бандажи, реставрация.")
    doc.add_paragraph("ЁёЙйЩщЪъЫыЬьЭэЮюЯя — проверка редких букв.")
    doc.add_paragraph(
        "Сумма прописью: " + num2words(1234.56, to="currency", lang="ru")
    )

    table = doc.add_table(rows=2, cols=3)
    table.style = "Table Grid"
    for i, h in enumerate(["Наименование", "Кол-во", "Сумма"]):
        table.rows[0].cells[i].text = h
    for i, v in enumerate(["Штамп ГРАФ-200", "2", "45 000,00"]):
        table.rows[1].cells[i].text = v

    doc.save(docx_path)

    try:
        pdf_path = docx_to_pdf(docx_path, tmp)
    except RuntimeError as e:
        raise HTTPException(500, str(e))

    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        filename="smoke_test.pdf",
    )


@app.post("/render-test")
async def render_test(
    template: UploadFile = File(...),
    context: str = Form("{}"),
):
    """
    Upload a real .docx template plus a JSON context, get the rendered PDF back.

    This is how we test each Bitrix template as it gets converted, without
    touching Airtable at all.

    curl -X POST $URL/render-test \
      -F "template=@schet.docx" \
      -F 'context={"client_name":"ООО Тест","total":45000}' \
      -o out.pdf
    """
    from docxtpl import DocxTemplate

    try:
        ctx = json.loads(context)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"context is not valid JSON: {e}")

    if not template.filename.lower().endswith(".docx"):
        raise HTTPException(400, "template must be a .docx file")

    tmp = Path(tempfile.mkdtemp())
    tpl_path = tmp / "template.docx"
    tpl_path.write_bytes(await template.read())

    out_docx = tmp / "rendered.docx"
    try:
        tpl = DocxTemplate(tpl_path)
        tpl.render(ctx)
        tpl.save(out_docx)
    except Exception as e:
        raise HTTPException(400, f"docxtpl render failed: {e}")

    try:
        pdf_path = docx_to_pdf(out_docx, tmp)
    except RuntimeError as e:
        raise HTTPException(500, str(e))

    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        filename="rendered.pdf",
    )


# --------------------------------------------------------------------------
# Stage 2 placeholder
# --------------------------------------------------------------------------

@app.post("/generate")
async def generate(payload: dict, background_tasks: BackgroundTasks):
    """
    Airtable-triggered generation. Returns 202 immediately because
    LibreOffice conversion takes longer than Airtable's automation timeout.

    Wired up in Stage 2, once the placeholder -> field mapping exists.
    """
    record_id = payload.get("record_id")
    doc_type = payload.get("doc_type")

    if not record_id or not doc_type:
        raise HTTPException(400, "record_id and doc_type are required")

    log.info("Generate requested: %s / %s (stub)", doc_type, record_id)

    return JSONResponse(
        status_code=202,
        content={
            "accepted": True,
            "record_id": record_id,
            "doc_type": doc_type,
            "note": "Stage 1 stub — Airtable wiring lands in Stage 2.",
        },
    )
