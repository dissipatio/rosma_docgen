"""
Render step for ROSMA document generation.

Takes the context dict produced by doc_resolver.build_context() and a
Doc Templates record, and produces a rendered .docx + .pdf. This is the
piece that turns the resolver's output into an actual file.

Pipeline so far:
    Airtable (Doc Field Map + Inquiry data)
        -> doc_resolver.build_context()          [reads Airtable, builds dict]
        -> doc_render.render_document()          [THIS FILE: docxtpl + LibreOffice]
        -> (next: upload result back to the Inquiry's attachment field)

CLI test (once the template is attached to the Doc Templates record):
    python doc_render.py "КП матрицы и ролики" A-936 --pdf
"""

import os
import sys
import argparse
import tempfile
from pathlib import Path

import requests

from docxtpl import DocxTemplate

from pdf_convert import docx_to_pdf  # shared with main.py -- same LibreOffice call, same profile-isolation fix
import doc_resolver as resolver  # reuses BASE_ID, API_BASE, HEADERS, etc.

# Doc Templates field IDs (see doc_resolver.py for the full list)
FLD_TPL_TEMPLATE_FILE = "flduRSPDOi4D8KHgR"  # "Template file" attachment field, confirmed via get_table_schema


def _get_template_file_url(template_record):
    """Doc Templates.Template file is an attachment field -- Airtable returns
    a list of attachment objects, each with a temporary 'url'. That URL
    expires, so this must be fetched fresh right before rendering, not cached."""
    attachments = resolver._field(template_record, FLD_TPL_TEMPLATE_FILE, [])
    if not attachments:
        raise ValueError(
            "Doc Templates record has no Template file attached yet. "
            "Attach the docxtpl-converted .docx to this record in Airtable first."
        )
    return attachments[0]["url"], attachments[0].get("filename", "template.docx")


def _download_template(url, dest_path):
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    with open(dest_path, "wb") as f:
        f.write(r.content)


def render_document(template_name, inquiry_ref, output_dir=None, make_pdf=True):
    """
    Full render: resolves context from Airtable, downloads the current
    template attachment, renders with docxtpl, optionally converts to PDF.

    Returns (docx_path, pdf_path_or_None).
    """
    output_dir = output_dir or tempfile.mkdtemp(prefix="rosma_docgen_")
    os.makedirs(output_dir, exist_ok=True)

    template_record = resolver._load_template_record(template_name)
    template_url, template_filename = _get_template_file_url(template_record)

    template_local_path = os.path.join(output_dir, "_template_" + template_filename)
    _download_template(template_url, template_local_path)

    context = resolver.build_context(template_name, inquiry_ref)
    skipped = context.get("_meta", {}).get("skipped_placeholders", [])
    if skipped:
        print(f"[doc_render] Warning: {len(skipped)} placeholder(s) marked "
              f"'Not built' in Doc Field Map were skipped.", file=sys.stderr)

    doc = DocxTemplate(template_local_path)
    doc.render(context)

    safe_inquiry = str(inquiry_ref).replace("/", "-")
    docx_filename = f"{safe_inquiry} — {template_name}.docx"
    docx_path = os.path.join(output_dir, docx_filename)
    doc.save(docx_path)

    pdf_path = None
    if make_pdf:
        pdf_path = str(docx_to_pdf(Path(docx_path), Path(output_dir)))

    return docx_path, pdf_path


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Render a ROSMA document end-to-end from Airtable data")
    parser.add_argument("template", help='Doc Templates name, e.g. "КП матрицы и ролики"')
    parser.add_argument("inquiry", help="Inquiry number (e.g. A-936) or record ID")
    parser.add_argument("--pdf", action="store_true", help="Also convert to PDF")
    parser.add_argument("--outdir", default=None, help="Output directory (default: temp dir)")
    args = parser.parse_args()

    docx_path, pdf_path = render_document(args.template, args.inquiry, output_dir=args.outdir, make_pdf=args.pdf)
    print(f"Rendered: {docx_path}")
    if pdf_path:
        print(f"PDF:      {pdf_path}")
