"""Read AcroForm fields into the schema_json shape.

Extraction is synchronous and does not call a model. A file with no AcroForm
fields, including an XFA-only PDF, is kind none. label is not set here.
"""

from io import BytesIO

from pypdf import PdfReader
from pypdf.errors import PyPdfError
from pypdf.generic import NameObject

# PDF button flags. Pushbutton wins over radio, matching the field-flag table.
_PUSHBUTTON = 1 << 16
_RADIO = 1 << 15


class UnreadablePdf(Exception):
    """The bytes are not a PDF pypdf can parse."""


class TemplateEditError(Exception):
    """A template PATCH breaks a field or text rule. detail is the HTTP body."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def extract_schema(data: bytes) -> dict:
    """Return {kind, fields} for a PDF. Raise UnreadablePdf if it cannot be parsed."""
    try:
        reader = PdfReader(BytesIO(data))
        raw_fields = reader.get_fields()
        return _schema_from_fields(raw_fields)
    except PyPdfError as exc:
        raise UnreadablePdf from exc


def public_form_schema(stored: object) -> dict:
    """The schema a client reads.

    A row written before field extraction, stored as {}, has no kind. That
    reads as kind none and an empty field list. The same is true of any value
    that is not the acroform or none shape.
    """
    if not isinstance(stored, dict) or stored.get("kind") not in {"acroform", "none"}:
        return {"kind": "none", "fields": []}
    raw_fields = stored.get("fields")
    if not isinstance(raw_fields, list):
        return {"kind": "none", "fields": []}
    fields = []
    for field in raw_fields:
        if isinstance(field, dict) and isinstance(field.get("name"), str):
            fields.append(dict(field))
    if stored["kind"] == "acroform" and not fields:
        return {"kind": "none", "fields": []}
    return {"kind": stored["kind"], "fields": fields}


def schema_summary(stored: object) -> tuple[str, int, dict]:
    """kind, field_count, and the public schema for one stored value."""
    public = public_form_schema(stored)
    return public["kind"], len(public["fields"]), public


def apply_field_labels(stored: object, labels: dict[str, str]) -> dict:
    """Set label on existing fields. An unknown name raises TemplateEditError.

    Names, types, and options stay as they were. This cannot add a field.
    """
    public = public_form_schema(stored)
    known = {field["name"] for field in public["fields"]}
    if any(name not in known for name in labels):
        raise TemplateEditError("Unknown field name.")
    fields = []
    for field in public["fields"]:
        updated = dict(field)
        if updated["name"] in labels:
            updated["label"] = labels[updated["name"]]
        fields.append(updated)
    return {"kind": public["kind"], "fields": fields}


def _schema_from_fields(raw_fields: object) -> dict:
    if not isinstance(raw_fields, dict) or not raw_fields:
        return {"kind": "none", "fields": []}
    fields = []
    for name, field in raw_fields.items():
        text = str(name).strip()
        if not text or not isinstance(field, dict):
            continue
        fields.append(_field_record(text, field))
    if not fields:
        return {"kind": "none", "fields": []}
    return {"kind": "acroform", "fields": fields}


def _field_record(name: str, field: dict) -> dict:
    field_type = _field_type(field)
    return {
        "name": name,
        "type": field_type,
        "options": _options(field, field_type),
    }


def _field_type(field: dict) -> str:
    name = _type_name(field)
    if name == "/Tx":
        return "text"
    if name == "/Ch":
        return "choice"
    if name == "/Btn":
        flags = _flags(field)
        if flags & _PUSHBUTTON:
            return "other"
        if flags & _RADIO:
            return "choice"
        return "checkbox"
    return "other"


def _type_name(field: dict) -> str:
    raw = _inherited(field, "/FT")
    if raw is None:
        return ""
    text = str(raw)
    return text if text.startswith("/") else f"/{text}"


def _flags(field: dict) -> int:
    raw = _inherited(field, "/Ff")
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def _inherited(field: dict, key: str) -> object:
    if key in field:
        return field.get(key)
    get_inherited = getattr(field, "get_inherited", None)
    if get_inherited is None:
        return None
    try:
        return get_inherited(key, None)
    except PyPdfError:
        return None


def _options(field: dict, field_type: str) -> list[str]:
    if field_type != "choice":
        return []
    if _type_name(field) == "/Btn":
        states = field.get("/_States_")
        if states is not None:
            return _option_strings(states, drop_off=True)
        return _radio_from_kids(field)
    raw = field.get("/Opt")
    if raw is None:
        raw = field.get("/_States_")
    return _option_strings(raw, drop_off=False)


def _option_strings(raw: object, *, drop_off: bool) -> list[str]:
    raw = _unwrap(raw)
    if not isinstance(raw, list):
        return []
    options: list[str] = []
    for item in raw:
        text = _export_text(item)
        if not text or (drop_off and text == "Off"):
            continue
        if text not in options:
            options.append(text)
    return options


def _radio_from_kids(field: dict) -> list[str]:
    kids = _unwrap(field.get("/Kids"))
    if not isinstance(kids, list):
        return []
    names: list[str] = []
    for kid in kids:
        kid = _unwrap(kid)
        if not isinstance(kid, dict):
            continue
        appearance = _unwrap(kid.get("/AP"))
        if not isinstance(appearance, dict):
            continue
        normal = _unwrap(appearance.get("/N"))
        if not isinstance(normal, dict):
            continue
        for key in normal.keys():
            text = _export_text(key)
            if text and text != "Off" and text not in names:
                names.append(text)
    return names


def _export_text(value: object) -> str:
    """Export value of a choice entry, or the name of a radio appearance state."""
    value = _unwrap(value)
    if isinstance(value, NameObject):
        text = str(value)
        return text[1:] if text.startswith("/") else text
    if isinstance(value, list):
        return _export_text(value[0]) if value else ""
    if isinstance(value, str):
        return value
    return str(value)


def _unwrap(value: object) -> object:
    for _ in range(5):
        get_object = getattr(value, "get_object", None)
        if get_object is None or isinstance(value, (str, bytes, dict, list)):
            return value
        value = get_object()
    return value
