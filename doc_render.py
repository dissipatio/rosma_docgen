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

from docxtpl import DocxTemplate, InlineImage
from docx.shared import Mm
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from jinja2 import Environment
from lxml import etree

from pdf_convert import docx_to_pdf  # shared with main.py -- same LibreOffice call, same profile-isolation fix
import doc_resolver as resolver  # reuses BASE_ID, API_BASE, HEADERS, etc.

# Doc Templates field IDs (see doc_resolver.py for the full list)
FLD_TPL_TEMPLATE_FILE = "flduRSPDOi4D8KHgR"  # "Template file" attachment field, confirmed via get_table_schema

# Default rendered width for stamp/signature images. Both source images in
# the first template using this (~23mm and ~38mm) fell in this range: one
# width for both keeps this simple, and docxtpl preserves aspect ratio when
# only width is given. Revisit if a template needs the two sized differently.
IMAGE_WIDTH_MM = 30


# Magic-byte signatures for the image formats Word can embed.
_IMAGE_SIGNATURES = [
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
    (b"II*\x00", "tiff"),
    (b"MM\x00*", "tiff"),
]
_CONTENT_TYPE_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/gif": "gif",
    "image/bmp": "bmp",
    "image/tiff": "tiff",
}


def _image_extension(content, content_type):
    """Works out a real file extension for a downloaded image: magic bytes
    first (they can't lie), then the HTTP Content-Type, else "png"."""
    for signature, ext in _IMAGE_SIGNATURES:
        if content.startswith(signature):
            return ext
    ctype = (content_type or "").split(";")[0].strip().lower()
    return _CONTENT_TYPE_EXT.get(ctype, "png")


def _download_to_temp(url, output_dir, suffix):
    """Downloads an arbitrary URL (e.g. a stamp/signature attachment) to a
    temp file in output_dir. Returns the local path, or None if url is
    falsy (e.g. no attachment uploaded yet for this field).

    BUG FIX: the temp file used to be saved as "_image_<var>" with NO
    extension. python-docx takes the embedded part's extension from the
    file name, so every stamp/signature went into the DOCX as
    "word/media/image1." with no matching [Content_Types].xml entry.
    LibreOffice (and therefore the PDFs) silently tolerated it, but
    Microsoft Word reports "unreadable content" and, on repair, drops all
    the images. Affected every template with Image-scope fields
    (Спецификация, Счёт, Договор). The file now gets a real extension
    (.png / .jpg / ...) detected from the bytes."""
    if not url:
        return None
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    ext = _image_extension(r.content, r.headers.get("Content-Type"))
    path = os.path.join(output_dir, f"_image_{suffix}.{ext}")
    with open(path, "wb") as f:
        f.write(r.content)
    return path


def _resolve_image_fields(context, doc, output_dir):
    """Mutates context in place: for every jinja_var listed in
    context["_meta"]["image_fields"] (populated by doc_resolver for Image
    scope rows), downloads the attachment URL and replaces the context
    value with a real docxtpl InlineImage. A field with no attachment
    uploaded yet (context value is None) is left as None -- docxtpl then
    renders that {{ tag }} as blank rather than erroring, so a document can
    still be generated before every stamp/signature is uploaded."""
    image_fields = context.get("_meta", {}).get("image_fields", [])
    for jinja_var in image_fields:
        url = context.get(jinja_var)
        if not url:
            # BUG FIX: was `= None`. A bare {{ tag }} left as None prints the
            # literal word "None" (Jinja only prints blank for a genuinely
            # undefined variable, not an explicit None) -- "" is what
            # actually renders as nothing. Same bug as the one fixed via
            # _blank_none() in doc_resolver.py, just not yet hit here
            # because no template has shipped without its stamp/signature
            # attached.
            context[jinja_var] = ""
            continue
        local_path = _download_to_temp(url, output_dir, jinja_var)
        context[jinja_var] = InlineImage(doc, local_path, width=Mm(IMAGE_WIDTH_MM))


# ---------------------------------------------------------------------------
# Image layout filters for templates
# ---------------------------------------------------------------------------
# Image-scope fields arrive in the context as InlineImage objects (30 mm wide,
# inline -- see _resolve_image_fields). A plain {{ stamp }} keeps rendering
# exactly like that, so existing templates are unaffected. A template can
# instead choose the layout itself with one of two filters:
#
#   {{ signature|img(h=15) }}
#       Inline image with an explicit size in mm: h= (height) or w= (width);
#       the other dimension keeps the aspect ratio. Use h= for signatures:
#       their proportions vary a lot, and a fixed width made a square-ish
#       signature ~31 mm tall.
#
#   {{ stamp|float_img(h=38, x=22, y=-18, rotate=-4, opacity=90) }}
#       Floating ("in front of text") image anchored to the paragraph the tag
#       sits in. x / y = offset in mm from that paragraph's column/cell left
#       edge and top edge (negative y = upwards). Takes no layout space, so a
#       stamp can overlap the signature and the name like on paper. Optional:
#       rotate= degrees (clockwise), opacity= 0-100, behind=True to put the
#       image behind the text instead.
#
# Both return "" when the field is empty (no attachment, or «Без печати и
# подписи» ticked), so the tag simply disappears. Type the tag in Word
# with plain ASCII quotes/minus: Word's autocorrect may turn "-18" into
# an en dash, which Jinja will reject.

_EMU_PER_MM = 36000


class FloatingImage(InlineImage):
    """InlineImage variant that emits a <wp:anchor> (floating, no text wrap)
    instead of <wp:inline>. Built from python-docx's own inline picture, so
    the image part, relationship and sizing logic are identical."""

    _z_order = 251659264  # Word-style relativeHeight seed; incremented per image

    def __init__(self, tpl, image_descriptor, width=None, height=None,
                 x_mm=0, y_mm=0, rotate=0, opacity=100, behind=False):
        super().__init__(tpl, image_descriptor, width=width, height=height)
        self.x_mm, self.y_mm = float(x_mm), float(y_mm)
        self.rotate, self.opacity, self.behind = float(rotate), float(opacity), bool(behind)

    def _insert_image(self):
        inline = self.tpl.current_rendering_part.new_pic_inline(
            self.image_descriptor, self.width, self.height
        )
        FloatingImage._z_order += 1

        anchor = OxmlElement("wp:anchor")
        for k, v in (("distT", "0"), ("distB", "0"), ("distL", "0"), ("distR", "0"),
                     ("simplePos", "0"), ("relativeHeight", str(FloatingImage._z_order)),
                     ("behindDoc", "1" if self.behind else "0"), ("locked", "0"),
                     ("layoutInCell", "1"), ("allowOverlap", "1")):
            anchor.set(k, v)

        simple = OxmlElement("wp:simplePos")
        simple.set("x", "0")
        simple.set("y", "0")
        anchor.append(simple)
        for tag, rel, mm in (("wp:positionH", "column", self.x_mm),
                             ("wp:positionV", "paragraph", self.y_mm)):
            pos = OxmlElement(tag)
            pos.set("relativeFrom", rel)
            off = OxmlElement("wp:posOffset")
            off.text = str(int(round(mm * _EMU_PER_MM)))
            pos.append(off)
            anchor.append(pos)

        anchor.append(inline.find(qn("wp:extent")))
        eff = OxmlElement("wp:effectExtent")
        for side in ("l", "t", "r", "b"):
            eff.set(side, "0")
        anchor.append(eff)
        anchor.append(OxmlElement("wp:wrapNone"))
        anchor.append(inline.find(qn("wp:docPr")))
        anchor.append(inline.find(qn("wp:cNvGraphicFramePr")))
        graphic = inline.find(qn("a:graphic"))
        anchor.append(graphic)

        if self.rotate:
            xfrm = graphic.find(".//" + qn("a:xfrm"))
            if xfrm is not None:
                xfrm.set("rot", str(int(round(self.rotate * 60000))))
        if self.opacity < 100:
            blip = graphic.find(".//" + qn("a:blip"))
            if blip is not None:
                alpha = OxmlElement("a:alphaModFix")
                alpha.set("amt", str(int(max(0, self.opacity) * 1000)))
                blip.append(alpha)

        return ("</w:t></w:r><w:r><w:drawing>%s</w:drawing></w:r><w:r>"
                '<w:t xml:space="preserve">' % etree.tostring(anchor, encoding="unicode"))


def _size_kwargs(h, w):
    # Only one dimension is passed on, so python-docx keeps the aspect ratio.
    if w:
        return {"width": Mm(float(w)), "height": None}
    if h:
        return {"width": None, "height": Mm(float(h))}
    return {"width": Mm(IMAGE_WIDTH_MM), "height": None}


def _img_filter(value, h=None, w=None):
    if not isinstance(value, InlineImage):
        return value  # "" (no image) passes through as blank
    return InlineImage(value.tpl, value.image_descriptor, **_size_kwargs(h, w))


def _float_img_filter(value, h=None, w=None, x=0, y=0, rotate=0, opacity=100, behind=False):
    if not isinstance(value, InlineImage):
        return value
    return FloatingImage(value.tpl, value.image_descriptor, x_mm=x, y_mm=y,
                         rotate=rotate, opacity=opacity, behind=behind, **_size_kwargs(h, w))


def _jinja_env():
    env = Environment()  # same defaults docxtpl uses when no env is passed
    env.filters["img"] = _img_filter
    env.filters["float_img"] = _float_img_filter
    return env


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
    _resolve_image_fields(context, doc, output_dir)
    doc.render(context, _jinja_env())

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
