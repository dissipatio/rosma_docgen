"""
End-to-end document generation triggered from the "Сгенерировать документ"
checkbox on Inquiries.

Flow:
    Airtable Automation (checkbox ticked)
        -> webhook POST {"record_id": "recXXXXXXXXXXXXXXX"}
        -> generate_document_for_record()            [THIS FILE]
             -> doc_resolver.build_context()          [Airtable -> dict]
             -> doc_render.render_document()          [dict -> docx -> pdf]
             -> yandex_disk_upload.upload_and_publish()[pdf -> public URL]
             -> write link back, reset checkbox, set status

Field IDs (Inquiries, tbl0F4KKFXXaObAHm):
    fldaqLZzI6yaF7bhm  Сгенерировать документ       (checkbox, the trigger)
    fldsM9XQtYPuz2SlP  Шаблон для генерации          (link -> Doc Templates)
    fldQr7PgIq1IxOe0L  Ссылка на сгенерированный документ (url)
    fldxI9z9NHTjpwgDk  Статус генерации              (singleSelect)
    fldejocySzUZ7SVMa  Ошибка генерации              (multilineText)

Status single-select option names (typecast handles conversion from these
plain strings -- see create_records_for_table learnings):
    "Ожидает" / "В процессе" / "Готово" / "Ошибка"

CLI test (once AIRTABLE_API_KEY / YANDEX_DISK_TOKEN / YANDEX_DISK_FOLDER
are set, e.g. via Railway shared vars):
    python generate_and_deliver.py recXXXXXXXXXXXXXXX
"""

import os
import sys
import argparse
import traceback

import requests

import doc_resolver as resolver
import doc_render
import yandex_disk_upload as yd

INQUIRIES_TABLE = resolver.INQUIRIES_TABLE
DOC_TEMPLATES_TABLE = resolver.DOC_TEMPLATES_TABLE

FLD_INQ_TRIGGER = "fldaqLZzI6yaF7bhm"
FLD_INQ_TEMPLATE_LINK = "fldsM9XQtYPuz2SlP"
FLD_INQ_RESULT_LINK = "fldQr7PgIq1IxOe0L"
FLD_INQ_STATUS = "fldxI9z9NHTjpwgDk"
FLD_INQ_ERROR = "fldejocySzUZ7SVMa"

FLD_TPL_NAME = resolver.FLD_TPL_NAME  # "fld0w9V0nvHNMDros" -- primary field, Template name


def _update_inquiry(record_id, fields):
    url = f"{resolver.API_BASE}/{resolver.BASE_ID}/{INQUIRIES_TABLE}/{record_id}"
    r = requests.patch(url, headers=resolver.HEADERS, json={"fields": fields}, timeout=30)
    r.raise_for_status()
    return r.json()


def _set_status(record_id, status, error_text=None, reset_trigger=False):
    fields = {FLD_INQ_STATUS: status}
    if error_text is not None:
        fields[FLD_INQ_ERROR] = error_text
    if reset_trigger:
        fields[FLD_INQ_TRIGGER] = False
    _update_inquiry(record_id, fields)


def _get_template_name_for_inquiry(inquiry_record):
    links = resolver._field(inquiry_record, FLD_INQ_TEMPLATE_LINK, [])
    if not links:
        raise ValueError(
            'Inquiry has no template selected in "Шаблон для генерации" -- '
            "link a Doc Templates record before ticking the trigger checkbox."
        )
    template_record_id = links[0]
    template_record = resolver._get_record(DOC_TEMPLATES_TABLE, template_record_id)
    return resolver._field(template_record, FLD_TPL_NAME)


def generate_document_for_record(record_id):
    """
    Full pipeline for one Inquiry record. Always leaves the record in a
    terminal, visible state (Готово+link, or Ошибка+message) and always
    resets the trigger checkbox, so a failed run doesn't get stuck unable
    to retry.
    """
    try:
        _set_status(record_id, "В процессе", error_text="")

        inquiry_record = resolver._get_record(INQUIRIES_TABLE, record_id)
        template_name = _get_template_name_for_inquiry(inquiry_record)

        docx_path, pdf_path = doc_render.render_document(template_name, record_id, make_pdf=True)

        yd.ensure_folder_exists()
        # NOTE: local docx/pdf filenames keep the readable "B-522 — КП Китай.pdf"
        # form (see doc_render.py) -- that's fine locally. But Yandex Disk's API
        # mishandled that same filename in a path parameter: spaces got encoded
        # as '+', which is only valid in form bodies, not URL paths, and Yandex's
        # server returned a 500 rather than a clean error. Keep the remote name
        # plain ASCII to sidestep this entirely.
        remote_filename = f"{record_id}.pdf"
        public_url = yd.upload_and_publish(pdf_path, remote_filename)

        _update_inquiry(record_id, {
            FLD_INQ_RESULT_LINK: public_url,
            FLD_INQ_STATUS: "Готово",
            FLD_INQ_ERROR: "",
            FLD_INQ_TRIGGER: False,
        })
        return {"ok": True, "record_id": record_id, "url": public_url}

    except Exception as exc:
        error_text = f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}"
        try:
            _set_status(record_id, "Ошибка", error_text=error_text[:9000], reset_trigger=True)
        except Exception:
            pass  # don't let a failed status write mask the original error
        return {"ok": False, "record_id": record_id, "error": str(exc)}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate a document for one Inquiry record")
    parser.add_argument("record_id", help="Inquiries record ID, e.g. recXXXXXXXXXXXXXXX")
    args = parser.parse_args()

    result = generate_document_for_record(args.record_id)
    if result["ok"]:
        print(f"Done: {result['url']}")
    else:
        print(f"Failed: {result['error']}", file=sys.stderr)
        sys.exit(1)
