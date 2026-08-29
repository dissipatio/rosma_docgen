"""
Generic resolver for ROSMA document generation.

Reads the Doc Templates / Doc Field Map tables (appiwf9u0xL0knirk) and,
given a template name + a root record ID (an Inquiries record), builds the
plain dict that docxtpl needs to render that template. One script serves
every template -- adding a new document type means adding rows to Doc
Field Map, not writing new Python.

-----------------------------------------------------------------------
HOW CHAIN WALKING WORKS

"Field ID chain" in Doc Field Map is a comma-separated list of field IDs,
e.g. "fldEG6QSkbG8nr7Wg,fldvNFwjffW6frtlr,fldExiSrQjC4eAMNA". Every field
except the last must be a multipleRecordLinks field; the resolver follows
each link to the next record. To know which TABLE each link points to
(needed to fetch the next record), the resolver fetches the base schema
once at startup and looks up each field's `linkedTableId` -- this is why
Doc Field Map doesn't need to store an ID for every hop, just the root's
Source table ID and the final field's.

ALTERNATE-FIELD CONVENTION: if the last segment of a chain contains "|"
(e.g. "fldExiSrQjC4eAMNA|fldkbL0DDvEx08nCf"), the resolver picks between
them using is_roller_row() -- first field for matrices, second for
rollers/shells. This convention exists because a few fields (e.g. Die
Track vs Shell Track) genuinely differ by product type. Document any new
use of this convention in the Doc Field Map row's Notes.
-----------------------------------------------------------------------

Env vars:
    AIRTABLE_API_KEY   Personal access token with access to the base
    AIRTABLE_BASE_ID   Defaults to appiwf9u0xL0knirk if unset

CLI test:
    python doc_resolver.py "КП матрицы и ролики" A-936 --dry-run
"""

import os
import sys
import json
import argparse
from functools import lru_cache

import requests

try:
    from num2words import num2words
except ImportError:
    num2words = None  # number_to_words_ru will raise a clear error if actually called

BASE_ID = os.environ.get("AIRTABLE_BASE_ID", "appiwf9u0xL0knirk").strip()
DOC_TEMPLATES_TABLE = "tbl3tSLF4OaoeKibd"
DOC_FIELD_MAP_TABLE = "tblEzpFpoEFVBXTJm"
INQUIRIES_TABLE = "tbl0F4KKFXXaObAHm"
INQUIERED_ITEMS_TABLE = "tblchEJTeS55IoHNv"

# Doc Templates field IDs
FLD_TPL_NAME = "fld0w9V0nvHNMDros"
FLD_TPL_ROOT_TABLE_ID = "fldCYFwoErMgqGyIO"
FLD_TPL_HAS_ROW_LOOP = "fld34BUKorsCjkll0"
FLD_TPL_ACTIVE = "fldYBt2YpHUqvM7o3"

# Doc Field Map field IDs
FLD_MAP_TEMPLATE_LINK = "fldCVYFb71sDiq9qZ"
FLD_MAP_PLACEHOLDER = "fldD472VBzTAw6Ydz"
FLD_MAP_JINJA_VAR = "fldUDnGDoeWqPXF50"
FLD_MAP_SCOPE = "fldEKTS1XWxBap08L"
FLD_MAP_SOURCE_TABLE_ID = "fld7rXyTowyqdgsGg"
FLD_MAP_FIELD_ID_CHAIN = "fldU1bixPyY1Wql6D"
FLD_MAP_COMPUTED_RULE = "fldWrpUge8PQk8W0L"
FLD_MAP_STATUS = "fldDGSCvew021kVBo"

# Inquiries field IDs needed to find the row source
FLD_INQ_ITEMS_LINK = "fldYy6SrZebO9mrQZ"

AIRTABLE_API_KEY = os.environ.get("AIRTABLE_API_KEY", "").strip()
if not AIRTABLE_API_KEY:
    raise RuntimeError("AIRTABLE_API_KEY is not set")

API_BASE = "https://api.airtable.com/v0"
META_BASE = "https://api.airtable.com/v0/meta"
HEADERS = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}


# --------------------------------------------------------------------------
# Low-level Airtable helpers
# --------------------------------------------------------------------------

def _get_record(table_id, record_id):
    r = requests.get(
        f"{API_BASE}/{BASE_ID}/{table_id}/{record_id}",
        headers=HEADERS,
        params={"returnFieldsByFieldId": "true"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def _list_records(table_id, filter_formula=None):
    records, params = [], {"returnFieldsByFieldId": "true"}
    if filter_formula:
        params["filterByFormula"] = filter_formula
    url = f"{API_BASE}/{BASE_ID}/{table_id}"
    while True:
        r = requests.get(url, headers=HEADERS, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        records.extend(data.get("records", []))
        if "offset" not in data:
            break
        params["offset"] = data["offset"]
    return records


def _field(record, field_id, default=None):
    return record.get("fields", {}).get(field_id, default)


def _select_name(value, default=""):
    if isinstance(value, dict):
        return value.get("name", default)
    return value if value is not None else default


def _multiselect_names(value):
    if not value:
        return []
    return [v.get("name", "") if isinstance(v, dict) else v for v in value]


def _cached_record(table_id, record_id):
    # NOTE: intentionally NOT cached across calls (no @lru_cache here).
    # This is called for records that get edited between renders in normal
    # operation -- Our company (stamps/signatures), Clients, Employees,
    # Goods, etc. main.py runs as a persistent process, not restarted per
    # request, so a process-lifetime cache would silently serve stale data
    # (e.g. a stamp uploaded after this record was first fetched) until the
    # next deploy. The extra API calls this costs are cheap relative to
    # correctness here.
    return _get_record(table_id, record_id)


# --------------------------------------------------------------------------
# Base schema cache -- lets the resolver know which table each link field
# points to, without Doc Field Map needing to store every intermediate hop.
# --------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _schema_index():
    """Returns {field_id: {"table_id": ..., "type": ..., "linked_table_id": ... or None}}"""
    r = requests.get(f"{META_BASE}/bases/{BASE_ID}/tables", headers=HEADERS, timeout=30)
    r.raise_for_status()
    index = {}
    for table in r.json().get("tables", []):
        for field in table.get("fields", []):
            linked = None
            if field.get("type") == "multipleRecordLinks":
                linked = field.get("options", {}).get("linkedTableId")
            index[field["id"]] = {
                "table_id": table["id"],
                "type": field.get("type"),
                "linked_table_id": linked,
            }
    return index


# --------------------------------------------------------------------------
# Chain walking
# --------------------------------------------------------------------------

def is_roller_row(product_name):
    name = str(product_name or "")
    return "Обечайка" in name or "Ролик" in name


def _resolve_chain(start_record, field_id_chain_str, row_context=None):
    """Walk a Field ID chain starting at an already-fetched record.
    Returns the raw field value at the end of the chain (caller normalizes
    select/multiselect shapes as needed)."""
    schema = _schema_index()
    chain = [f.strip() for f in field_id_chain_str.split(",") if f.strip()]
    if not chain:
        return None

    current_record = start_record

    for i, field_id in enumerate(chain):
        is_last = i == len(chain) - 1

        # Alternate-field convention: "fldA|fldB"
        if "|" in field_id:
            opt_a, opt_b = field_id.split("|", 1)
            product_name = (row_context or {}).get("name", "")
            field_id = opt_b if is_roller_row(product_name) else opt_a

        if is_last:
            return _field(current_record, field_id)

        # Not last -> must be a link field; follow it
        links = _field(current_record, field_id, [])
        if not links:
            return None
        next_record_id = links[0]
        meta = schema.get(field_id)
        if not meta or not meta.get("linked_table_id"):
            raise ValueError(f"Field {field_id} is not a recognized link field in base schema")
        next_table_id = meta["linked_table_id"]
        current_record = _cached_record(next_table_id, next_record_id)

    return None


# --------------------------------------------------------------------------
# Computed rule registry
# --------------------------------------------------------------------------

def _line_sum(product_row):
    # product_row is the resolved dict for one item; look for a value that
    # represents the line total. Convention: jinja var containing "sum".
    for k, v in product_row.items():
        if k.endswith(".sum") or k == "sum":
            try:
                return float(v or 0)
            except (TypeError, ValueError):
                return 0
    return 0


def _rule_vat_inclusive_tax_value(ctx):
    total = ctx.get("total_raw", 0) or 0
    rate = 0
    if ctx["products"]:
        rate_raw = ctx["products"][0].get("tax_rate") or ctx["products"][0].get("product.tax_rate")
        try:
            rate = float(str(rate_raw).replace("%", "")) if rate_raw else 0
        except (TypeError, ValueError):
            rate = 0
    if not rate:
        return 0.0
    return round(total - total / (1 + rate / 100), 2)


def _rule_number_to_words_ru(ctx):
    if num2words is None:
        raise RuntimeError("num2words is not installed -- pip install num2words --break-system-packages")
    amount = ctx.get("total_sum", 0) or 0
    currency = ctx.get("currency_symbol", "€")
    whole = int(amount)
    # NOTE: verify this against a real filled example before trusting it in
    # production -- num2words' Russian currency support is inconsistent
    # across versions. "евро" is hardcoded for the € case seen so far;
    # extend the map if $ / ₽ quotes need it too.
    currency_word = {"€": "евро", "$": "долларов", "₽": "рублей"}.get(currency, currency)
    words = num2words(whole, lang="ru")
    return f"{words} {currency_word}"


import datetime

RU_MONTHS_GENITIVE = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


def _rule_today_date_ru(ctx):
    return datetime.date.today().strftime("%d.%m.%Y")


def _rule_today_date_ru_long(ctx):
    today = datetime.date.today()
    return f"{today.day} {RU_MONTHS_GENITIVE[today.month - 1]} {today.year}"


COMPUTED_REGISTRY = {
    "sum_line_items": lambda ctx: round(sum(_line_sum(p) for p in ctx["products"]), 2),
    "vat_inclusive_tax_value": _rule_vat_inclusive_tax_value,
    "number_to_words_ru": _rule_number_to_words_ru,
    "today_date_ru": _rule_today_date_ru,
    "today_date_ru_long": _rule_today_date_ru_long,
    "row_index": None,  # handled inline in the row loop, not called generically
    "currency_symbol": None,  # in practice resolved as a Header lookup, not computed -- kept for schema completeness
}


# --------------------------------------------------------------------------
# Doc Field Map loading
# --------------------------------------------------------------------------

def _load_template_record(template_name):
    formula = f"{{{FLD_TPL_NAME}}} = '{template_name}'"
    records = _list_records(DOC_TEMPLATES_TABLE, filter_formula=formula)
    if not records:
        raise ValueError(f"No Doc Templates record found for '{template_name}'")
    return records[0]


def _load_field_map_rows(template_record_id):
    # NOTE: deliberately NOT using filterByFormula here. A formula like
    # FIND('{template_record_id}', ARRAYJOIN({Template})) looks correct but
    # silently matches zero rows -- ARRAYJOIN() on a linked-record field
    # returns the linked record's DISPLAY NAME, not its record ID. Doc
    # Field Map is small (~80 rows total), so fetching everything and
    # filtering in Python avoids the gotcha entirely.
    all_rows = _list_records(DOC_FIELD_MAP_TABLE)
    matched = []
    for row in all_rows:
        links = _field(row, FLD_MAP_TEMPLATE_LINK, [])
        linked_ids = [link.get("id") if isinstance(link, dict) else link for link in links]
        if template_record_id in linked_ids:
            matched.append(row)
    return matched


# --------------------------------------------------------------------------
# Root record resolution (Inquiries, by number or record ID)
# --------------------------------------------------------------------------

def _resolve_inquiry_record(inquiry_ref):
    if inquiry_ref.startswith("rec") and len(inquiry_ref) == 17:
        return _get_record(INQUIRIES_TABLE, inquiry_ref)
    formula = f"{{Inquiry}} = '{inquiry_ref}'"  # field name fallback -- adjust if this 422s
    records = _list_records(INQUIRIES_TABLE, filter_formula=formula)
    if not records:
        raise ValueError(f"No Inquiries record found for '{inquiry_ref}'")
    return records[0]


# --------------------------------------------------------------------------
# Main resolver
# --------------------------------------------------------------------------

def build_context(template_name, inquiry_ref):
    template = _load_template_record(template_name)
    rows = _load_field_map_rows(template["id"])
    root_record = _resolve_inquiry_record(inquiry_ref)

    header_rows = [r for r in rows if _select_name(_field(r, FLD_MAP_SCOPE)) == "Header"]
    row_rows = [r for r in rows if _select_name(_field(r, FLD_MAP_SCOPE)) == "Row"]
    computed_rows = [r for r in rows if _select_name(_field(r, FLD_MAP_SCOPE)) == "Computed"]
    static_rows = [r for r in rows if _select_name(_field(r, FLD_MAP_SCOPE)) == "Static constant"]
    image_rows = [r for r in rows if _select_name(_field(r, FLD_MAP_SCOPE)) == "Image"]
    skipped = [r for r in rows if _select_name(_field(r, FLD_MAP_SCOPE)) in ("Not built", "")]

    context = {}

    # --- Header fields ---
    for row in header_rows:
        jinja_var = _field(row, FLD_MAP_JINJA_VAR)
        chain = _field(row, FLD_MAP_FIELD_ID_CHAIN)
        if not jinja_var or not chain:
            continue
        value = _resolve_chain(root_record, chain)
        context[jinja_var] = _select_name(value) if isinstance(value, dict) else value

    # --- Image fields (stamp/signature, etc.) -- chain must end in an
    # Attachment field. Resolve to the first attachment's URL (a plain
    # string), not the normal select/scalar handling. doc_render.py
    # downloads this URL and swaps it for a real docxtpl InlineImage right
    # before rendering -- see context["_meta"]["image_fields"].
    image_field_vars = []
    for row in image_rows:
        jinja_var = _field(row, FLD_MAP_JINJA_VAR)
        chain = _field(row, FLD_MAP_FIELD_ID_CHAIN)
        if not jinja_var or not chain:
            continue
        value = _resolve_chain(root_record, chain)
        if isinstance(value, list) and value and isinstance(value[0], dict) and "url" in value[0]:
            context[jinja_var] = value[0]["url"]
        else:
            context[jinja_var] = None  # no attachment uploaded yet -- renders as a blank space, not an error
        image_field_vars.append(jinja_var)

    # --- Row fields ---
    item_ids = _field(root_record, FLD_INQ_ITEMS_LINK, [])
    products = []
    for idx, item_id in enumerate(item_ids):
        item_record = _cached_record(INQUIERED_ITEMS_TABLE, item_id)
        row_ctx = {}
        for row in row_rows:
            jinja_var = _field(row, FLD_MAP_JINJA_VAR)
            chain = _field(row, FLD_MAP_FIELD_ID_CHAIN)
            if not jinja_var or not chain:
                continue
            value = _resolve_chain(item_record, chain, row_context=row_ctx)
            if isinstance(value, dict):
                value = _select_name(value)
            elif isinstance(value, list) and value and isinstance(value[0], dict):
                value = ", ".join(_multiselect_names(value))
            row_ctx[jinja_var.replace("product.", "")] = value
        row_ctx["index"] = idx + 1
        products.append(row_ctx)
    context["products"] = products

    # --- Static constants ---
    STATIC_VALUES = {
        "tax_title": "НДС",
    }
    for row in static_rows:
        jinja_var = _field(row, FLD_MAP_JINJA_VAR)
        if jinja_var in STATIC_VALUES:
            context[jinja_var] = STATIC_VALUES[jinja_var]
        else:
            context[jinja_var] = None  # not yet defined -- add to STATIC_VALUES above

    # --- Computed fields (may depend on header/products already in context) ---
    for row in computed_rows:
        jinja_var = _field(row, FLD_MAP_JINJA_VAR)
        rule_name = _select_name(_field(row, FLD_MAP_COMPUTED_RULE))
        if jinja_var == "product.index":
            continue  # handled in the row loop above
        rule_fn = COMPUTED_REGISTRY.get(rule_name)
        if rule_fn is None:
            context[jinja_var] = None
            continue
        context[jinja_var] = rule_fn(context)

    context["_meta"] = {
        "template": template_name,
        "skipped_placeholders": [_field(r, FLD_MAP_PLACEHOLDER) for r in skipped],
        "image_fields": image_field_vars,
    }
    return context


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Resolve a document render context from Doc Field Map")
    parser.add_argument("template", help='Doc Templates name, e.g. "КП матрицы и ролики"')
    parser.add_argument("inquiry", help="Inquiry number (e.g. A-936) or record ID")
    parser.add_argument("--dry-run", action="store_true", help="Print context as JSON")
    args = parser.parse_args()

    ctx = build_context(args.template, args.inquiry)
    if args.dry_run:
        print(json.dumps(ctx, indent=2, ensure_ascii=False))
        if ctx["_meta"]["skipped_placeholders"]:
            print(f"\nSkipped (Not built): {ctx['_meta']['skipped_placeholders']}", file=sys.stderr)
