"""
End-to-end document generation, triggered by a generation checkbox on
either Inquiries or (since Договор поставки) Clients.

Flow:
    Airtable Automation (checkbox ticked)
        -> webhook POST {"record_id": "recXXXXXXXXXXXXXXX", ...}
        -> generate_document_for_record()            [THIS FILE]
             -> doc_resolver.build_context()          [Airtable -> dict]
             -> doc_render.render_document()          [dict -> docx -> pdf]
             -> yandex_disk_upload.upload_and_publish()[pdf -> public URL]
             -> write link back, reset checkbox, set status

TABLE RESOLUTION: the original design assumed record_id always meant an
Inquiries record, and read which template to use from the Inquiry's own
"Шаблон для генерации" link field. That still works unchanged when the
payload is just {"record_id": ...} (Inquiries automation, as before).
Clients-scoped templates don't have a generic "Шаблон для генерации"
selector yet -- there's a dedicated trigger checkbox per template instead
(currently just Договор поставки) -- so the Clients automation's webhook
call must also pass table_id and template_name explicitly. If both are
present in the payload, they're used as-is and the Inquiries-specific
lookup is skipped entirely.

Field IDs (Inquiries, tbl0F4KKFXXaObAHm):
    fldaqLZzI6yaF7bhm  Сгенерировать документ       (checkbox, the trigger)
    fldsM9XQtYPuz2SlP  Шаблон для генерации          (link -> Doc Templates)
    fldQr7PgIq1IxOe0L  Ссылка на сгенерированный документ (url)
    fldxI9z9NHTjpwgDk  Статус генерации              (singleSelect)
    fldejocySzUZ7SVMa  Ошибка генерации              (multilineText)

Field IDs (Clients, tblRRW1btCVX9Yp8F) -- same shape, Договор поставки only:
    fld6EZ04Ym74AKY2x  Сгенерировать Договор         (checkbox, the trigger)
    fldNs6MbeHCSjGTO2  Ссылка на сгенерированный документ (url)
    fldUZfvt8hEAaGuhY  Статус генерации              (singleSelect)
    fld7bbN3bPaEDsaPO  Ошибка генерации              (multilineText)
    fldD8D8H3gN7scBJx  Договор (файл)                (attachment, convenience
                                                       copy -- populated by
                                                       pointing Airtable at
                                                       the same Yandex Disk
                                                       URL, so Airtable
                                                       fetches its own copy)

Status single-select option names (typecast handles conversion from these
plain strings -- see create_records_for_table learnings):
    "Ожидает" / "В процессе" / "Готово" / "Ошибка"

Adding a third generation source later (another table, another dedicated
checkbox): add its four fields here in TABLE_FIELD_MAP, no other changes.

CLI test (once AIRTABLE_API_KEY / YANDEX_DISK_TOKEN / YANDEX_DISK_FOLDER
are set, e.g. via Railway shared vars):
    python generate_and_deliver.py recXXXXXXXXXXXXXXX
    python generate_and_deliver.py recXXXXXXXXXXXXXXX --table tblRRW1btCVX9Yp8F --template "Договор поставки"
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
CLIENTS_TABLE = "tblRRW1btCVX9Yp8F"
DOC_TEMPLATES_TABLE = resolver.DOC_TEMPLATES_TABLE

FLD_TPL_NAME = resolver.FLD_TPL_NAME  # "fld0w9V0nvHNMDros" -- primary field, Template name

# Per-table trigger/result/status/error fields, and (Inquiries only) the
# generic "which template" link field. Clients has no such link yet since
# it currently only drives one template -- template_name must come from
# the webhook payload instead (see TABLE_FIELD_MAP["template_name"]).
TABLE_FIELD_MAP = {
    INQUIRIES_TABLE: {
        "trigger": "fldaqLZzI6yaF7bhm",
        "template_link": "fldsM9XQtYPuz2SlP",
        "result_link": "fldQr7PgIq1IxOe0L",
        "status": "fldxI9z9NHTjpwgDk",
        "error": "fldejocySzUZ7SVMa",
        "result_attachment": None,
    },
    CLIENTS_TABLE: {
        "trigger": "fld6EZ04Ym74AKY2x",
        "template_link": None,
        "template_name": "Договор поставки",  # only template Clients drives today
        "result_link": "fldNs6MbeHCSjGTO2",
        "status": "fldUZfvt8hEAaGuhY",
        "error": "fld7bbN3bPaEDsaPO",
        "result_attachment": "fldD8D8H3gN7scBJx",
    },
}


def _update_record(table_id, record_id, fields):
    url = f"{resolver.API_BASE}/{resolver.BASE_ID}/{table_id}/{record_id}"
    r = requests.patch(url, headers=resolver.HEADERS, json={"fields": fields}, timeout=30)
    r.raise_for_status()
    return r.json()


def _set_status(table_id, record_id, status, error_text=None, reset_trigger=False):
    fmap = TABLE_FIELD_MAP[table_id]
    fields = {fmap["status"]: status}
    if error_text is not None:
        fields[fmap["error"]] = error_text
    if reset_trigger:
        fields[fmap["trigger"]] = False
    _update_record(table_id, record_id, fields)


def _get_template_name(table_id, record):
    fmap = TABLE_FIELD_MAP[table_id]
    if fmap.get("template_link"):
        links = resolver._field(record, fmap["template_link"], [])
        if not links:
            raise ValueError(
                'Record has no template selected in "Шаблон для генерации" -- '
                "link a Doc Templates record before ticking the trigger checkbox."
            )
        template_record = resolver._get_record(DOC_TEMPLATES_TABLE, links[0])
        return resolver._field(template_record, FLD_TPL_NAME)
    if fmap.get("template_name"):
        return fmap["template_name"]
    raise ValueError(f"No template resolution configured for table {table_id}")


def _detect_table(record_id, table_id_hint):
    """Table comes from the webhook payload when the caller knows it
    (Clients automation always sends table_id explicitly). Falls back to
    Inquiries for backward compatibility with the original automation,
    which only ever sends {"record_id": ...}."""
    if table_id_hint:
        if table_id_hint not in TABLE_FIELD_MAP:
            raise ValueError(f"Unrecognized table_id: {table_id_hint}")
        return table_id_hint
    return INQUIRIES_TABLE


def generate_document_for_record(record_id, table_id=None, template_name=None):
    """
    Full pipeline for one record (Inquiries or Clients). Always leaves the
    record in a terminal, visible state (Готово+link, or Ошибка+message)
    and always resets the trigger checkbox, so a failed run doesn't get
    stuck unable to retry.
    """
    table_id = _detect_table(record_id, table_id)
    try:
        _set_status(table_id, record_id, "В процессе", error_text="")

        record = resolver._get_record(table_id, record_id)
        if not template_name:
            template_name = _get_template_name(table_id, record)

        docx_path, pdf_path = doc_render.render_document(template_name, record_id, make_pdf=True)

        yd.ensure_folder_exists()
        # NOTE: local docx/pdf filenames keep the readable "B-522 — КП Китай.pdf"
        # form (see doc_render.py) -- that's fine locally. But Yandex Disk's
        # API mishandled that same filename in a path parameter: spaces got
        # encoded as '+', which is only valid in form bodies, not URL paths,
        # and Yandex's server returned a 500 rather than a clean error. Keep
        # the remote name plain ASCII to sidestep this entirely.
        remote_filename = f"{record_id}.pdf"
        public_url = yd.upload_and_publish(pdf_path, remote_filename)

        fmap = TABLE_FIELD_MAP[table_id]
        result_fields = {
            fmap["result_link"]: public_url,
            fmap["status"]: "Готово",
            fmap["error"]: "",
            fmap["trigger"]: False,
        }
        if fmap.get("result_attachment"):
            # Airtable fetches and stores its own copy given a URL -- gives
            # a one-click download right on the record, on top of the plain
            # link field every table already gets.
            result_fields[fmap["result_attachment"]] = [{"url": public_url}]
        _update_record(table_id, record_id, result_fields)
        return {"ok": True, "record_id": record_id, "url": public_url}

    except Exception as exc:
        error_text = f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}"
        try:
            _set_status(table_id, record_id, "Ошибка", error_text=error_text[:9000], reset_trigger=True)
        except Exception:
            pass  # don't let a failed status write mask the original error
        return {"ok": False, "record_id": record_id, "error": str(exc)}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate a document for one Inquiries or Clients record")
    parser.add_argument("record_id", help="Record ID, e.g. recXXXXXXXXXXXXXXX")
    parser.add_argument("--table", default=None, help="Table ID (default: Inquiries, for backward compatibility)")
    parser.add_argument("--template", default=None, help='Doc Templates name, e.g. "Договор поставки" (required if --table has no "Шаблон для генерации" link field)')
    args = parser.parse_args()

    result = generate_document_for_record(args.record_id, table_id=args.table, template_name=args.template)
    if result["ok"]:
        print(f"Done: {result['url']}")
    else:
        print(f"Failed: {result['error']}", file=sys.stderr)
        sys.exit(1)
