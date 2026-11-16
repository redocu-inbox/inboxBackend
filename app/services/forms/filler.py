"""Write known values into an AcroForm PDF.

Unknown keys are skipped and returned as dropped. Fields the caller did not
send stay as they were. Signature and pushbutton fields are type other and
are skipped. This does not flatten the form and does not change confidence.
"""

from dataclasses import dataclass
from io import BytesIO

from pypdf import PdfReader, PdfWriter
from pypdf.generic import DictionaryObject, NameObject, TextStringObject

from app.services.forms.schema import extract_schema

_CHECK = {"true", "yes", "1", "on"}
_CLEAR = {"false", "no", "0", "off", ""}


@dataclass(frozen=True)
class FillResult:
    pdf: bytes
    dropped: list[str]


class PdfFormFiller:
    """Write `values` into AcroForm fields and return the filled PDF."""

    def fill(self, template: bytes, values: dict) -> FillResult:
        if not isinstance(values, dict):
            raise TypeError("values must be an object")
        schema = extract_schema(template)
        fields = {
            field["name"]: field
            for field in schema.get("fields", [])
            if isinstance(field, dict) and isinstance(field.get("name"), str)
        }
        prepared: dict[str, tuple[str, object]] = {}
        for key, raw in values.items():
            name = key if isinstance(key, str) else str(key)
            field = fields.get(name)
            if field is None:
                continue
            value = _prepare(field, raw)
            if value is None:
                continue
            prepared[name] = value
        pdf, written = _write(template, prepared)
        dropped = [str(key) for key in values if str(key) not in written]
        return FillResult(pdf=pdf, dropped=dropped)


def get_filler(mime_type: str) -> PdfFormFiller:
    if mime_type == "application/pdf":
        return PdfFormFiller()
    raise NotImplementedError(f"No form filler for mime type {mime_type!r}")


def _prepare(field: dict, raw: object) -> tuple[str, object] | None:
    kind = field.get("type")
    if kind == "text":
        text = _text_value(raw)
        if text is None:
            return None
        return ("text", text)
    if kind == "checkbox":
        state = _checkbox_state(raw)
        if state is None:
            return None
        return ("checkbox", state)
    if kind == "choice":
        options = field.get("options") if isinstance(field.get("options"), list) else []
        if isinstance(raw, str) and raw in options:
            return ("choice", raw)
        return None
    return None


def _text_value(raw: object) -> str | None:
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, str):
        return raw
    if isinstance(raw, int):
        return str(raw)
    if isinstance(raw, float):
        if raw.is_integer():
            return str(int(raw))
        return str(raw)
    return None


def _checkbox_state(raw: object) -> bool | None:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int):
        if raw == 1:
            return True
        if raw == 0:
            return False
        return None
    if isinstance(raw, float) and raw in (0.0, 1.0):
        return raw == 1.0
    if isinstance(raw, str):
        text = raw.strip().casefold()
        if text in _CHECK:
            return True
        if text in _CLEAR:
            return False
    return None


def _write(template: bytes, prepared: dict[str, tuple[str, object]]) -> tuple[bytes, set[str]]:
    if not prepared:
        return template, set()
    reader = PdfReader(BytesIO(template))
    writer = PdfWriter()
    writer.append(reader)
    widgets = _widgets(writer)
    written: set[str] = set()
    text_values = {
        name: value
        for name, (kind, value) in prepared.items()
        if kind == "text" and isinstance(value, str) and name in widgets
    }
    if text_values:
        writer.update_page_form_field_values(None, text_values, auto_regenerate=False)
        written.update(text_values)
    for name, (kind, value) in prepared.items():
        if kind == "text" or name not in widgets:
            continue
        widget, parent = widgets[name]
        if kind == "checkbox" and isinstance(value, bool):
            state = _on_state(widget, parent) if value else "Off"
            _set_name(widget, "/AS", state)
            _set_name(widget, "/V", state)
            if parent is not widget:
                _set_name(parent, "/V", state)
            written.add(name)
        elif kind == "choice" and isinstance(value, str):
            _set_choice(widget, parent, value)
            written.add(name)
    buffer = BytesIO()
    writer.write(buffer)
    pdf = buffer.getvalue()
    saved = PdfReader(BytesIO(pdf)).get_fields() or {}
    written = {name for name in written if _stuck(saved.get(name), prepared[name])}
    return pdf, written


def _stuck(field: object, prepared: tuple[str, object]) -> bool:
    kind, value = prepared
    stored = None
    if isinstance(field, dict) and "/V" in field:
        stored = str(field["/V"])
    if kind == "text":
        return stored == value
    if kind == "checkbox":
        if value is True:
            return stored not in {None, "", "/Off", "Off"}
        return stored in {"/Off", "Off", ""}
    if kind == "choice":
        return stored in {value, f"/{value}"}
    return False


def _widgets(writer: PdfWriter) -> dict[str, tuple[DictionaryObject, DictionaryObject]]:
    found: dict[str, tuple[DictionaryObject, DictionaryObject]] = {}
    for page in writer.pages:
        annots = page.get("/Annots")
        if annots is None:
            continue
        for annot in annots:
            widget = annot.get_object()
            if str(widget.get("/Subtype")) != "/Widget":
                continue
            parent = widget
            if "/T" not in widget or "/FT" not in widget:
                raw_parent = widget.get("/Parent")
                if raw_parent is None:
                    continue
                parent = raw_parent.get_object()
            name = _qualified_name(parent)
            if name and name not in found:
                found[name] = (widget, parent)
    return found


def _qualified_name(field: DictionaryObject, seen: set[int] | None = None) -> str:
    if seen is None:
        seen = set()
    marker = id(field)
    if marker in seen:
        return str(field.get("/T", ""))
    seen.add(marker)
    if "/TM" in field:
        return str(field["/TM"])
    raw_parent = field.get("/Parent")
    if raw_parent is not None:
        parent = raw_parent.get_object()
        return _qualified_name(parent, seen) + "." + str(field.get("/T", ""))
    return str(field.get("/T", ""))


def _on_state(widget: DictionaryObject, parent: DictionaryObject) -> str:
    for source in (widget, parent):
        names = _appearance_names(source)
        if "Yes" in names:
            return "Yes"
        if names:
            return names[0]
    return "Yes"


def _appearance_names(field: DictionaryObject) -> list[str]:
    appearance = field.get("/AP")
    if appearance is None:
        return []
    appearance = appearance.get_object()
    if not isinstance(appearance, dict):
        return []
    normal = appearance.get("/N")
    if normal is None:
        return []
    normal = normal.get_object()
    if not isinstance(normal, dict):
        return []
    names: list[str] = []
    for key in normal.keys():
        text = str(key)
        if text.startswith("/"):
            text = text[1:]
        if text and text != "Off" and text not in names:
            names.append(text)
    return names


def _set_choice(widget: DictionaryObject, parent: DictionaryObject, value: str) -> None:
    if str(parent.get("/FT")) == "/Btn" or str(widget.get("/FT")) == "/Btn":
        _set_name(parent, "/V", value)
        _set_name(widget, "/AS", value)
        if parent is not widget:
            _set_name(widget, "/V", value)
        return
    parent[NameObject("/V")] = TextStringObject(value)


def _set_name(field: DictionaryObject, key: str, name: str) -> None:
    text = name if name.startswith("/") else f"/{name}"
    field[NameObject(key)] = NameObject(text)
