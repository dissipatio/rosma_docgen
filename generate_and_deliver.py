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
import re
import sys
import time
import argparse
import traceback
from datetime import datetime, timezone, timedelta

import requests

import doc_resolver as resolver
import doc_render
import yandex_disk_upload as yd

INQUIRIES_TABLE = resolver.INQUIRIES_TABLE
CLIENTS_TABLE = "tblRRW1btCVX9Yp8F"
DOC_TEMPLATES_TABLE = resolver.DOC_TEMPLATES_TABLE
UPDATES_TABLE = "tblAGP1eIx6Oyomq1"

# Updates fields used when logging a КП generation (see
# _log_update_for_kp_generation below). Only fired for Inquiries-scoped КП
# templates -- Clients-scoped ones (Договор поставки) have no "Inquiries
# linked to update" to hang the record off of, and non-КП Inquiries
# templates (Спецификация, Счёт) weren't asked for.
FLD_UPD_INQUIRY_LINK = "fldodg80PN8iAOOkg"  # Inquiries linked to update
FLD_UPD_TYPE = "fld419nHW9PB6KsFX"          # Types update (singleSelect, "КП" is an existing option)
FLD_UPD_PDF_LINK = "fldI7K46hOWKZCWyE"      # PDF link (url)
FLD_UPD_DOCX_LINK = "fldKL25uKySSXPluE"     # DOCX link (url)
FLD_UPD_DATE_GMAIL = "fldrEqou0hQGy4zIT"    # Date_gmail (dateTime) -- reused here as "when this
                                             # was created", same field the Gmail sync writes to,
                                             # so both sources sort/display consistently.

# Clients: Inquiries link -- Договор поставки is Clients-scoped and has no
# Inquiry of its own, but Updates only has an "Inquiries linked to update"
# field (no Clients link), so for that one template we fall back to the
# client's own first linked Inquiry. Same fallback already used elsewhere
# for that template's "Our company" resolution (see its Doc Templates
# notes) -- reasonable since one client's inquiries share one seller
# entity, and here it's just "something to hang the log row on", not data
# the document itself depends on.
FLD_CLIENT_INQUIRIES_LINK = "fldLcWAcez8ztMN4H"

# Doc Templates name -> Updates "Types update" value, by prefix (covers
# every variant of each category, e.g. all the "Спец — ..." templates).
# "Документ" is the fallback for anything that doesn't match, rather than
# skipping the log entirely, in case a template is added later that isn't
# any of these four. "Спецификация"/"Счёт"/"Договор" aren't pre-existing
# options on the Types update select -- created via typecast the first
# time one of these logs, same as "КП" already was.
# Prefixes are compared after normalizing ё -> е and lowercasing, so
# «Счет гибкая оплата» (е) and «Счёт ...» (ё) both map to "Счёт".
# BUG FIX: the old exact-prefix "Счёт" never matched the real Doc Templates
# names «Счет гибкая оплата» / «Счет предоплата 100%» (spelled with е), so
# every invoice was logged as the fallback type "Документ".
_UPDATE_TYPE_BY_PREFIX = [
    ("кп", "КП"),
    ("спец", "Спецификация"),
    ("счет", "Счёт"),
    ("договор", "Договор"),
]


def _normalize_name(name):
    return str(name or "").strip().lower().replace("ё", "е")


def _update_type_for_template(template_name):
    normalized = _normalize_name(template_name)
    for prefix, type_value in _UPDATE_TYPE_BY_PREFIX:
        if normalized.startswith(prefix):
            return type_value
    return "Документ"


def _inquiry_id_for_log(table_id, record, record_id):
    """The Inquiry record ID to link the Updates row to. Direct for
    Inquiries-scoped generations; for Clients-scoped ones (Договор
    поставки) falls back to the client's own first linked Inquiry, per
    FLD_CLIENT_INQUIRIES_LINK above. Returns None if neither is available
    -- the caller skips logging in that case rather than creating an
    Updates row with nothing to link it to."""
    if table_id == INQUIRIES_TABLE:
        return record_id
    if table_id == CLIENTS_TABLE:
        links = resolver._field(record, FLD_CLIENT_INQUIRIES_LINK, [])
        return links[0] if links else None
    return None

FLD_TPL_NAME = resolver.FLD_TPL_NAME  # "fld0w9V0nvHNMDros" -- primary field, Template name

# Field IDs used only to build a human-readable filename (inquiry number /
# client display name) -- not part of the render context.
FLD_INQ_DISPLAY_NUMBER = "fldF1GTqeq8BiArqe"  # Inquiries: Inquiry (e.g. "B-1299")
FLD_CLIENT_DISPLAY_NAME = "fldrMb8nojLXI7daC"  # Clients: Сокращённое наименование

OUR_COMPANY_NAME = "РОСМА"  # used in generated filenames, e.g. "B-1299_РОСМА.pdf"


def _display_id_for_filename(table_id, record, record_id):
    """Best-effort human-readable identifier for the generated filename --
    the Inquiry's own display number (e.g. "B-1299") for Inquiries-scoped
    templates, the client's short name for Clients-scoped ones (Договор
    поставки), falling back to the raw record_id if the expected field
    isn't populated."""
    if table_id == INQUIRIES_TABLE:
        value = resolver._field(record, FLD_INQ_DISPLAY_NUMBER)
    elif table_id == CLIENTS_TABLE:
        value = resolver._field(record, FLD_CLIENT_DISPLAY_NAME)
    else:
        value = None
    return str(value).strip() if value else record_id

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


def _airtable_request(method, url, **kwargs):
    """Shared by _update_record/_create_record. Airtable rate-limits at 5
    req/sec per base; build_context() alone does many reads in a tight
    burst, and this module's own calls land right after that burst, so a
    429 here is plausible under load even though each individual call is
    normally fine. One short retry covers the ordinary transient case
    (Airtable's own guidance is to back off ~30s, but that's unreasonable
    to block a webhook response on, so this is a courtesy retry, not a
    complete rate-limit strategy)."""
    resp = requests.request(method, url, **kwargs)
    if resp.status_code == 429:
        time.sleep(3)
        resp = requests.request(method, url, **kwargs)
    return resp


def _update_record(table_id, record_id, fields):
    url = f"{resolver.API_BASE}/{resolver.BASE_ID}/{table_id}/{record_id}"
    r = _airtable_request("PATCH", url, headers=resolver.HEADERS, json={"fields": fields}, timeout=30)
    r.raise_for_status()
    return r.json()


def _create_record(table_id, fields):
    url = f"{resolver.API_BASE}/{resolver.BASE_ID}/{table_id}"
    r = _airtable_request(
        "POST", url, headers=resolver.HEADERS, json={"fields": fields, "typecast": True}, timeout=30
    )
    r.raise_for_status()
    return r.json()


def _log_update_for_generation(inquiry_record_id, type_value, pdf_url, docx_url):
    """Creates one Updates row per generation run, so a document's history
    shows up there the same way a manually-logged comment or an incoming
    email would -- link to both the PDF and the editable DOCX, and a
    timestamp in Date_gmail (the same field the Gmail sync writes to, not a
    new one) so generated-doc rows sort/display consistently alongside
    emails rather than needing their own separate "when" field."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    _create_record(
        UPDATES_TABLE,
        {
            FLD_UPD_INQUIRY_LINK: [inquiry_record_id],
            FLD_UPD_TYPE: type_value,
            FLD_UPD_PDF_LINK: pdf_url,
            FLD_UPD_DOCX_LINK: docx_url,
            FLD_UPD_DATE_GMAIL: now_iso,
        },
    )


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
        # Filename now carries the inquiry number (or client name for
        # Clients-scoped templates) plus the company name, e.g.
        # "B-1299_РОСМА.pdf", instead of the bare record ID -- readable to
        # whoever downloads it from the Yandex Disk link or the Airtable
        # attachment copy. Kept space-free: Yandex Disk's upload API
        # mishandles spaces in the `path` query param (requests encodes
        # them as '+', form-style, which is only valid in form bodies, not
        # URL paths, and Yandex's server returns a 500 rather than a clean
        # error) -- any spaces already present in the source field (there
        # shouldn't be any in an inquiry number, but a client name could
        # have them) are collapsed to '-' rather than risk that.
        display_id = _display_id_for_filename(table_id, record, record_id)
        safe_display_id = re.sub(r"\s+", "-", display_id)
        # BUG FIX: the filename used to be only "<inquiry>_РОСМА.pdf", and
        # uploads run with overwrite=true -- so КП, Спецификация and Счёт for
        # the same inquiry all overwrote ONE file, and every older Updates
        # row's PDF/DOCX link silently started opening the newest document.
        # Now the name also carries the template and a Moscow-time stamp,
        # e.g. "A-test_Счет-гибкая-оплата_РОСМА_20260921-1557.pdf", so each
        # generation is its own file and each Updates row keeps pointing at
        # the version it logged. Still space-free (see note above); anything
        # that isn't a letter/digit/_/- (spaces, %, quotes) becomes "-".
        safe_template = re.sub(r"[^\w-]+", "-", str(template_name)).strip("-")
        stamp = datetime.now(timezone(timedelta(hours=3))).strftime("%Y%m%d-%H%M")
        base_filename = f"{safe_display_id}_{safe_template}_{OUR_COMPANY_NAME}_{stamp}"
        remote_filename = f"{base_filename}.pdf"
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

        # Log an Updates row for every document type (КП, Спецификация,
        # Счёт, Договор поставки). Also uploads the DOCX itself (not just
        # the PDF) so the Updates row can link to the editable version too.
        # Skipped only if there's genuinely no Inquiry to hang the row off
        # of (see _inquiry_id_for_log). Best-effort: a failure here
        # shouldn't undo the successful generation above, so it's caught
        # and swallowed rather than turning the whole run into an "Ошибка".
        log_inquiry_id = _inquiry_id_for_log(table_id, record, record_id)
        if not log_inquiry_id:
            # Most likely a Договор поставки generation for a Client with
            # no linked Inquiry (see _inquiry_id_for_log) -- nothing to
            # hang an Updates row off of. Printed for the same reason as
            # the except block below: silent skips are indistinguishable
            # from bugs otherwise.
            print(
                f"[generate_and_deliver] No Inquiry to log an Updates row against for "
                f"{record_id} (table {table_id}) -- skipped.",
                file=sys.stderr,
            )
        else:
            try:
                # A short deliberate pause before starting the second
                # upload to the same Yandex Disk folder -- confirmed in
                # practice that starting it immediately after the PDF
                # upload/publish just completed reliably 423-locks (see
                # _RETRY_DELAYS_SECONDS in yandex_disk_upload.py for the
                # full story). That retry budget alone covers this too,
                # eventually, but avoiding the lock is cheaper than
                # retrying through it every single time.
                time.sleep(3)
                docx_remote_filename = f"{base_filename}.docx"
                docx_url = yd.upload_and_publish(docx_path, docx_remote_filename)
                type_value = _update_type_for_template(template_name)
                _log_update_for_generation(log_inquiry_id, type_value, public_url, docx_url)
            except Exception:
                # Best-effort: a failure here shouldn't undo the successful
                # generation above (status stays "Готово", the PDF link is
                # already written). But it must NOT disappear silently --
                # printed here (Railway captures stdout/stderr in its logs)
                # so a missing Updates row is debuggable instead of a
                # mystery. An earlier version of this block used a bare
                # `except: pass`, which is exactly how a real failure here
                # (e.g. the DOCX upload, or the create_record call itself)
                # went completely unreported.
                print(
                    f"[generate_and_deliver] Updates-row logging failed for {record_id} "
                    f"(document itself still generated successfully):",
                    file=sys.stderr,
                )
                traceback.print_exc()

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
