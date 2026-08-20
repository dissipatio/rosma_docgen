"""
LibreOffice DOCX->PDF conversion, extracted from main.py's Stage 1 code
unchanged, so it can be shared between the FastAPI app and
generate_and_deliver.py without main.py importing generate_and_deliver.py
(which imports doc_render.py, which would otherwise need to import main.py
right back -- a circular import).
"""

import logging
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

log = logging.getLogger("docgen")

SOFFICE_TIMEOUT = int(os.getenv("SOFFICE_TIMEOUT", "120"))


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
