"""
Generic resolver for ROSMA document generation.

Reads the Doc Templates / Doc Field Map tables (appiwf9u0xL0knirk) and,
given a template name + a root record ID, builds the plain dict that
docxtpl needs to render that template. The root record's table is read
from the template's Scope table field (FLD_TPL_ROOT_TABLE_ID) -- most
templates are scoped to Inquiries, but a template can be scoped to any
table (e.g. Договор поставки is scoped to Clients, since a contract
covers the whole client relationship rather than one deal). One script
serves every template -- adding a new document type means adding rows to
Doc Field Map, not writing new Python.

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
from datetime import date
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

# Optional per-template ROW FILTER (both fields empty = no filtering, i.e. the
# template behaves exactly as before). Lets a template render only the items
# whose status is in an allowed list -- e.g. КП templates show only items
# with «Статус КП для товара» = «Отправлено клиенту», so managers never have
# to delete unsent items from the inquiry.
#   FLD_TPL_ROW_FILTER_FIELD  -- text: the Field ID to check on each item
#                                (a field on Inquired Items, e.g. fld37V1onwDwcUnyV)
#   FLD_TPL_ROW_FILTER_VALUES -- long text: allowed values, ONE PER LINE
# (Doc Templates fields «Row filter field ID» / «Row filter values». If either
# constant is set to "" the filter is switched off for every template.)
FLD_TPL_ROW_FILTER_FIELD = "fldzMkZhPAXjlAecT"
FLD_TPL_ROW_FILTER_VALUES = "fld8gUUCxIH48rjDb"

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

# Inquiries checkbox: "Без печати и подписи" -- when checked, forces stamp
# and signature blank in the rendered document regardless of whether the
# Our company attachment fields actually have images uploaded. Only
# meaningful when the template's root table is Inquiries (true for both
# Spec templates); on other scope tables (e.g. Договор поставки -> Clients)
# the root record simply won't have this field, so _field()'s default
# leaves the checkbox treated as unchecked -- safe no-op there.
FLD_INQ_NO_STAMP_SIGN = "fldDDtAJvvo1uk94A"

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


@lru_cache(maxsize=None)
def _cached_record(table_id, record_id):
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

def _unwrap_ai(value):
    """AI text fields (type aiText) come back from the API as
    {"state": "generated", "value": "...", "isStale": false} instead of a
    plain string. Without this, a chain ending on an AI field reached the
    Header loop as a dict, got treated like a single-select option, and
    _select_name() returned "" -- the document silently rendered blank
    (seen on contract_date -> Contracts.Дата договора прописью, A-test).
    Only a "generated" value is usable; "empty"/"error"/"pending" become
    None (-> "" via _blank_none). A lookup of an AI field arrives as a list
    of these dicts, so lists are unwrapped element by element."""
    if isinstance(value, dict) and "state" in value and "value" in value:
        return value.get("value") if value.get("state") == "generated" else None
    if isinstance(value, list) and value and isinstance(value[0], dict) and "state" in value[0]:
        return [_unwrap_ai(v) for v in value]
    return value


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
            return _unwrap_ai(_field(current_record, field_id))

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


def _line_discount(product_row):
    # Mirrors _line_sum above, but for the per-line discount amount
    # (product.discount_amount -> "discount_amount" after the "product."
    # strip in the row loop) so discount_total can sum it the same way
    # total_raw sums product.sum.
    for k, v in product_row.items():
        if k.endswith(".discount_amount") or k == "discount_amount":
            try:
                return float(v or 0)
            except (TypeError, ValueError):
                return 0
    return 0


def _row_label(product_row):
    """Short human label for error messages."""
    return str(product_row.get("name") or f"строка {product_row.get('index', '?')}")


def _row_vat_rate(product_row):
    """The item's effective VAT rate as a number (22, 20, 0) from Inquired
    Items «Ставка НДС» (item VAT, falling back to the inquiry's Vat), mapped
    in Doc Field Map as product.vat_rate. None if missing."""
    raw = product_row.get("vat_rate")
    if raw is None or raw == "":
        return None
    try:
        return float(str(raw).replace("%", "").replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


def _fmt_rate(rate):
    return f"{int(rate)}%" if float(rate).is_integer() else f"{rate:g}%"


def _check_vat_rates(ctx):
    missing = [_row_label(p) for p in ctx["products"] if _row_vat_rate(p) is None]
    if missing:
        raise ValueError(
            "НДС не определён (пустая «Ставка НДС» -- заполните VAT у товара "
            "или Vat у заявки): " + "; ".join(missing)
        )


def _rule_vat_inclusive_tax_value(ctx):
    """Total VAT of the document = sum of Inquired Items «НДС в строке»
    (product.vat) over the rows actually rendered. Each line's VAT is
    computed in Airtable on the line's final amount (Sum КП со скидкой, so
    discounts are included) at that item's own rate -- mixed rates, discounts
    and the row filter are therefore all handled correctly.
    Replaces the old single-rate formula (total - total/(1+rate)) that took
    the rate from the first item only and printed 0 when it was missing."""
    _check_vat_rates(ctx)
    total = 0.0
    for p in ctx["products"]:
        v = p.get("vat")
        try:
            total += float(v or 0)
        except (TypeError, ValueError):
            raise ValueError(f"Некорректное значение «НДС в строке» у {_row_label(p)}: {v!r}")
    return round(total, 2)


def _rule_vat_rate_label(ctx):
    """Header label for the rate: '22%' normally, '20% / 22%' if the rendered
    items have different rates -- so the label can never contradict the
    VAT amount."""
    _check_vat_rates(ctx)
    rates = sorted({_row_vat_rate(p) for p in ctx["products"]})
    return " / ".join(_fmt_rate(r) for r in rates)


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


_RU_MONTHS_GENITIVE = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


def _rule_today_date_ru(ctx):
    """DD.MM.YYYY -- matches the '~d.m.y' format hint on {DocumentCreateTime~d.m.y}."""
    return date.today().strftime("%d.%m.%Y")


def _rule_today_date_ru_long(ctx):
    """'10 августа 2026' -- day, genitive month name, year."""
    today = date.today()
    return f"{today.day} {_RU_MONTHS_GENITIVE[today.month - 1]} {today.year}"


def _rule_today_date_full(ctx):
    """Same as today_date_ru_long with a trailing 'г.'"""
    return f"{_rule_today_date_ru_long(ctx)} г."


COMPUTED_REGISTRY = {
    "sum_line_items": lambda ctx: round(sum(_line_sum(p) for p in ctx["products"]), 2),
    # New: sums product.discount_amount (Сумма скидки) across items for the
    # KP's total-discount summary row. Same shape as sum_line_items -- kept
    # separate rather than parameterizing one function, since the two keys
    # ("sum" vs "discount_amount") are genuinely different fields with
    # nothing to gain from sharing an implementation.
    "sum_discount_items": lambda ctx: round(sum(_line_discount(p) for p in ctx["products"]), 2),
    "vat_inclusive_tax_value": _rule_vat_inclusive_tax_value,
    "vat_rate_label": _rule_vat_rate_label,
    "number_to_words_ru": _rule_number_to_words_ru,
    # BUG FIX: these three were selectable in Doc Field Map's Computed Rule
    # field but had no implementation here. COMPUTED_REGISTRY.get(rule_name)
    # returned None for them, which build_context() then wrote straight into
    # context[jinja_var] as a literal None -- and Jinja/docxtpl prints an
    # explicit None as the four-letter word "None", not blank. This is what
    # produced "Коммерческое предложение № B-1299 от None" in the КП матрицы
    # и ролики template (document_date -> today_date_ru).
    "today_date_ru": _rule_today_date_ru,
    "today_date_ru_long": _rule_today_date_ru_long,
    "today_date_full": _rule_today_date_full,
    "row_index": None,  # handled inline in the row loop, not called generically
    "currency_symbol": None,  # in practice resolved as a Header lookup, not computed -- kept for schema completeness
}


# --------------------------------------------------------------------------
# Money formatting (display only)
# --------------------------------------------------------------------------
# BUG FIX: computed totals were written into the document as raw Python
# floats -- "20000.0" instead of "20 000,00" (seen on the A-test Спецификация).
# Formatting runs as the LAST step of build_context(), after every computed
# rule has finished: sum_line_items / vat_inclusive_tax_value /
# number_to_words_ru all read these values as numbers, so formatting any
# earlier would break them.
#
# Style: Russian convention -- non-breaking space between thousands (so a
# number never wraps across lines), comma decimal, always 2 decimals.
#
# String values like "€ 3606.56" (the Inquiries «VAT from SUM €» formula,
# which puts the symbol first) are parsed and rebuilt as "3 606,56 €" so the
# VAT line matches the totals. Any other string is left untouched.
#
# Kill switch: set DOCGEN_FORMAT_MONEY=0 on Railway to restore the old raw
# output without a redeploy (e.g. if a template turns out to do arithmetic
# on one of these variables in Jinja).

FORMAT_MONEY = os.environ.get("DOCGEN_FORMAT_MONEY", "1").strip() != "0"

# Header-level variables that hold money amounts
MONEY_HEADER_VARS = {"total_raw", "total_sum", "tax_value", "discount_total"}
# Per-item keys (after the "product." prefix is stripped in the row loop)
MONEY_ROW_KEYS = {"price", "sum", "discounted_price", "discount_amount", "vat"}

_CURRENCY_SYMBOLS = "€$₽¥£"


def _fmt_number(num):
    """20000 -> '20 000,00' (NBSP as thousands separator)."""
    s = f"{float(num):,.2f}"                     # '20,000.00'
    return s.replace(",", "\u00a0").replace(".", ",")


def _format_money(value):
    if isinstance(value, bool) or value is None or value == "":
        return value
    if isinstance(value, (int, float)):
        return _fmt_number(value)
    if isinstance(value, str):
        text = value.strip()
        symbol = ""
        if text and text[0] in _CURRENCY_SYMBOLS:
            symbol, text = text[0], text[1:].strip()
        elif text and text[-1] in _CURRENCY_SYMBOLS:
            symbol, text = text[-1], text[:-1].strip()
        try:
            num = float(text.replace("\u00a0", "").replace(" ", "").replace(",", "."))
        except ValueError:
            return value                           # not a plain amount -- leave as is
        formatted = _fmt_number(num)
        return f"{formatted}\u00a0{symbol}" if symbol else formatted
    return value


def _apply_money_format(context):
    for var in MONEY_HEADER_VARS:
        if var in context:
            context[var] = _format_money(context[var])
    for product in context.get("products", []):
        for key in MONEY_ROW_KEYS:
            if key in product:
                product[key] = _format_money(product[key])


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
# Root record resolution -- table comes from the template's Scope table
# field (FLD_TPL_ROOT_TABLE_ID), not a hardcoded Inquiries assumption.
# By record ID this is a plain fetch; the Inquiry-number fallback only
# makes sense when the scope table actually is Inquiries.
# --------------------------------------------------------------------------

def _resolve_root_record(root_ref, root_table_id):
    if root_ref.startswith("rec") and len(root_ref) == 17:
        return _get_record(root_table_id, root_ref)
    if root_table_id != INQUIRIES_TABLE:
        raise ValueError(
            f"'{root_ref}' is not a record ID and non-Inquiries scope tables "
            f"don't support a name-based lookup yet"
        )
    formula = f"{{Inquiry}} = '{root_ref}'"  # field name fallback -- adjust if this 422s
    records = _list_records(root_table_id, filter_formula=formula)
    if not records:
        raise ValueError(f"No record found for '{root_ref}' in table {root_table_id}")
    return records[0]


# --------------------------------------------------------------------------
# Row filter -- show only items whose status is in a template-defined list
# --------------------------------------------------------------------------

def _load_row_filter(template):
    """Returns (field_id, {allowed values}) or None when the template has no
    filter configured (either config field empty, or field IDs not set up)."""
    if not FLD_TPL_ROW_FILTER_FIELD or not FLD_TPL_ROW_FILTER_VALUES:
        return None
    field_id = str(_field(template, FLD_TPL_ROW_FILTER_FIELD) or "").strip()
    raw = str(_field(template, FLD_TPL_ROW_FILTER_VALUES) or "")
    allowed = {line.strip() for line in raw.splitlines() if line.strip()}
    if not field_id or not allowed:
        return None
    return field_id, allowed


def _item_passes_filter(item_record, row_filter):
    """True if the item should be rendered. An item whose filter field is
    empty is NOT rendered when a filter is active (no status = not sent).
    Comparison is whitespace-trimmed on both sides, because some option names
    are stored with stray leading spaces (e.g. ' Спец (согласование)')."""
    if row_filter is None:
        return True
    field_id, allowed = row_filter
    value = _unwrap_ai(_field(item_record, field_id))
    if isinstance(value, list):
        names = [_select_name(v) if isinstance(v, dict) else str(v) for v in value]
    elif value is None:
        names = []
    else:
        names = [_select_name(value) if isinstance(value, dict) else str(value)]
    return any(n.strip() in allowed for n in names)


# --------------------------------------------------------------------------
# Main resolver
# --------------------------------------------------------------------------

def _blank_none(obj):
    """Recursively replace Python None with '' throughout a context tree.

    Jinja/docxtpl only prints an empty string for a genuinely *undefined*
    variable -- an explicit None is str()'d, which prints the literal word
    "None". This codebase uses None as "no data yet" all over the place
    (skipped/Not-built placeholders, empty lookups, unmapped computed
    rules, blank freeform text fields), so without this every one of those
    cases showed up as visible "None" text in generated documents (the
    date header, the general comment, and the payment/delivery terms
    concatenation were the ones caught in practice). Leaves everything
    else -- including 0, False, and InlineImage objects added later by
    doc_render.py -- untouched.
    """
    if obj is None:
        return ""
    if isinstance(obj, dict):
        return {k: _blank_none(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_blank_none(v) for v in obj]
    return obj


def build_context(template_name, root_ref):
    # BUG FIX: _cached_record uses @lru_cache(maxsize=None) -- unbounded and
    # never expires for the life of the process. On a long-running Railway
    # service that means every record fetch is cached FOREVER: once any
    # record involved in a render is fetched once, every later render of a
    # document touching that same record reuses that stale snapshot
    # indefinitely, even after the underlying Airtable data changes. This is
    # what kept track_width resolving to nothing on A-1347 long after the
    # Goods lookup itself was confirmed populated -- the Goods record had
    # been cached before that data existed. Clearing at the start of every
    # build_context() call keeps the (real) benefit of not re-fetching the
    # same record twice within one document's render, while guaranteeing
    # every new generation request starts from live Airtable data.
    _cached_record.cache_clear()

    template = _load_template_record(template_name)
    rows = _load_field_map_rows(template["id"])
    root_table_id = _field(template, FLD_TPL_ROOT_TABLE_ID) or INQUIRIES_TABLE
    root_record = _resolve_root_record(root_ref, root_table_id)

    header_rows = [r for r in rows if _select_name(_field(r, FLD_MAP_SCOPE)) == "Header"]
    image_rows = [r for r in rows if _select_name(_field(r, FLD_MAP_SCOPE)) == "Image"]
    row_rows = [r for r in rows if _select_name(_field(r, FLD_MAP_SCOPE)) == "Row"]
    # BUG FIX: the alternate-field ("fldA|fldB") convention in _resolve_chain
    # picks Die vs. Shell based on row_context["name"], which is only set
    # once the product.name row has run for this item. Doc Field Map rows
    # come back from Airtable in no particular order, so without this sort
    # a track-width row (or any other alt-field row) processed before
    # product.name always sees an empty name and silently falls back to the
    # Die option -- observed as track width resolving to None for both a
    # die AND a roller-shell line on the same inquiry.
    row_rows.sort(key=lambda r: 0 if _field(r, FLD_MAP_JINJA_VAR) == "product.name" else 1)
    computed_rows = [r for r in rows if _select_name(_field(r, FLD_MAP_SCOPE)) == "Computed"]
    static_rows = [r for r in rows if _select_name(_field(r, FLD_MAP_SCOPE)) == "Static constant"]
    skipped = [r for r in rows if _select_name(_field(r, FLD_MAP_SCOPE)) in ("Not built", "")]

    context = {}

    # --- Header fields ---
    for row in header_rows:
        jinja_var = _field(row, FLD_MAP_JINJA_VAR)
        chain = _field(row, FLD_MAP_FIELD_ID_CHAIN)
        if not jinja_var or not chain:
            continue
        value = _resolve_chain(root_record, chain)
        if isinstance(value, dict):
            value = _select_name(value)
        elif isinstance(value, list):
            # Same flattening the row loop already does: a lookup (or an
            # unwrapped AI lookup) ending a Header chain arrives as a list and
            # would otherwise print as "['X']" in the document. Attachment
            # lists never get here -- they're Image scope, handled below.
            value = ", ".join(
                _select_name(v) if isinstance(v, dict) else str(v)
                for v in value if v not in (None, "")
            )
        context[jinja_var] = value

    # --- Image fields ---
    # The chain resolves to an Airtable attachment field, i.e. a list of
    # attachment dicts (or [] if nothing's uploaded yet for this entity).
    # Context gets just the first attachment's URL (or None); doc_render.py
    # downloads that URL and swaps it for a real docxtpl InlineImage right
    # before rendering, using the jinja_var names listed in image_fields.
    image_fields = []
    for row in image_rows:
        jinja_var = _field(row, FLD_MAP_JINJA_VAR)
        chain = _field(row, FLD_MAP_FIELD_ID_CHAIN)
        if not jinja_var or not chain:
            continue
        attachments = _resolve_chain(root_record, chain)
        context[jinja_var] = attachments[0]["url"] if attachments else None
        image_fields.append(jinja_var)

    # BUG FIX / FEATURE: "Без печати и подписи" on Inquiries lets a user
    # force a document out without stamp/signature (e.g. a draft sent for
    # review before it's actually signed) even when Our company's
    # Печать/Подпись attachments are populated. Overriding the URL to None
    # here -- rather than touching doc_render.py -- reuses the existing
    # "no attachment yet" path: _resolve_image_fields() already turns a
    # None image value into "" so the {{ signature }}{{ stamp }} tags
    # render blank instead of erroring or printing "None".
    no_stamp_sign = bool(_field(root_record, FLD_INQ_NO_STAMP_SIGN, False))
    if no_stamp_sign:
        for jinja_var in image_fields:
            context[jinja_var] = None

    # --- Row fields ---
    item_ids = _field(root_record, FLD_INQ_ITEMS_LINK, [])
    # Optional per-template filter (see FLD_TPL_ROW_FILTER_*). Hidden items are
    # skipped BEFORE anything is computed, so every total / VAT / discount sum /
    # amount-in-words (all derived from context["products"]) automatically
    # covers only the rendered items.
    row_filter = _load_row_filter(template)
    hidden_by_filter = 0
    products = []
    for item_id in item_ids:
        item_record = _cached_record(INQUIERED_ITEMS_TABLE, item_id)
        if not _item_passes_filter(item_record, row_filter):
            hidden_by_filter += 1
            continue
        row_ctx = {}
        for row in row_rows:
            jinja_var = _field(row, FLD_MAP_JINJA_VAR)
            chain = _field(row, FLD_MAP_FIELD_ID_CHAIN)
            if not jinja_var or not chain:
                continue
            value = _resolve_chain(item_record, chain, row_context=row_ctx)
            if isinstance(value, dict):
                value = _select_name(value)
            elif isinstance(value, list) and value:
                if isinstance(value[0], dict):
                    value = ", ".join(_multiselect_names(value))
                else:
                    # BUG FIX: a multi-hop lookup through a linked-record
                    # field (e.g. Inquired Items -> Goods -> material) comes
                    # back as a list even when it resolves to a single
                    # value. str()'ing that list literally printed
                    # "['X46Cr13']" in rendered documents -- flatten it here
                    # instead, same as the list-of-dicts case above.
                    value = ", ".join(str(v) for v in value)
            row_ctx[jinja_var.replace("product.", "")] = value
        # Per-row rate label ("22%") for templates that print the rate in a
        # table column. Always derived from the same effective rate used for
        # the VAT amount, so column, header label and VAT never disagree.
        _rate = _row_vat_rate(row_ctx)
        if _rate is not None:
            row_ctx["tax_rate"] = _fmt_rate(_rate)
        # Numbered by position among the items actually shown (1, 2, 3...),
        # not by position in the inquiry, so hidden items leave no gaps.
        row_ctx["index"] = len(products) + 1
        products.append(row_ctx)
    if row_filter is not None and item_ids and not products:
        # Every item was filtered out. Fail loudly rather than producing an
        # empty КП that could be sent to a client by mistake.
        raise ValueError(
            "No items to show in this document: none of the "
            f"{len(item_ids)} item(s) has a status in the template's row "
            f"filter ({', '.join(sorted(row_filter[1]))})"
        )
    context["products"] = products
    # One log line per render (visible in Railway logs) so it is obvious whether
    # the row filter was active for this template and what it did.
    print(
        f"[docgen] row filter for '{template_name}': "
        f"{'ON' if row_filter is not None else 'OFF'} -- "
        f"showing {len(products)} of {len(item_ids)} item(s), hidden {hidden_by_filter}",
        flush=True,
    )

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

    # --- Computed fields (may depend on header/products, AND on each other:
    # e.g. vat_inclusive_tax_value reads ctx["total_raw"], which is itself
    # only present once sum_line_items has run). Doc Field Map rows come
    # back from the Airtable API in no particular order, so a single pass
    # can silently compute a dependent value before its dependency exists --
    # ctx.get("total_raw", 0) then defaults to 0 with no error, which is
    # exactly what produced "НДС 22%%: 0.0" on an order whose real VAT was
    # $868.46. BUG FIX: run two passes; the first lets same-scope
    # dependencies land in context, the second recomputes everything now
    # that they're available. Cheap, and correct for any dependency depth
    # the current registry actually has. ---
    for _pass in range(2):
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

    meta = {
        "template": template_name,
        "skipped_placeholders": [_field(r, FLD_MAP_PLACEHOLDER) for r in skipped],
        "image_fields": image_fields,
        "stamp_signature_suppressed": no_stamp_sign,
        "rows_hidden_by_filter": hidden_by_filter,
    }
    # Display formatting runs last -- every computed rule above needs these
    # values as numbers. See "Money formatting" section.
    if FORMAT_MONEY:
        _apply_money_format(context)

    context = _blank_none(context)
    context["_meta"] = meta
    return context


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Resolve a document render context from Doc Field Map")
    parser.add_argument("template", help='Doc Templates name, e.g. "КП матрицы и ролики"')
    parser.add_argument("root_ref", help="Root record: Inquiry number (e.g. A-936) or any record ID matching the template's Scope table")
    parser.add_argument("--dry-run", action="store_true", help="Print context as JSON")
    args = parser.parse_args()

    ctx = build_context(args.template, args.root_ref)
    if args.dry_run:
        print(json.dumps(ctx, indent=2, ensure_ascii=False))
        if ctx["_meta"]["skipped_placeholders"]:
            print(f"\nSkipped (Not built): {ctx['_meta']['skipped_placeholders']}", file=sys.stderr)
