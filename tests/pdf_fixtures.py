"""Small PDFs for the form-library tests.

pypdf has no public helper that adds an AcroForm field, so these builders
attach a widget and point the catalog AcroForm at it.
"""

from io import BytesIO

from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    NumberObject,
    TextStringObject,
)


def blank_pdf_bytes() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    return _bytes(writer)


def text_field_pdf_bytes(name: str = "FullName") -> bytes:
    return _widget_pdf(
        {
            NameObject("/FT"): NameObject("/Tx"),
            NameObject("/T"): TextStringObject(name),
        }
    )


def checkbox_field_pdf_bytes(name: str = "Agree") -> bytes:
    return _widget_pdf(
        {
            NameObject("/FT"): NameObject("/Btn"),
            NameObject("/T"): TextStringObject(name),
            NameObject("/V"): NameObject("/Off"),
            NameObject("/AS"): NameObject("/Off"),
        }
    )


def signature_field_pdf_bytes(name: str = "Sign") -> bytes:
    return _widget_pdf(
        {
            NameObject("/FT"): NameObject("/Sig"),
            NameObject("/T"): TextStringObject(name),
        }
    )


def choice_field_pdf_bytes() -> bytes:
    return _widget_pdf(
        {
            NameObject("/FT"): NameObject("/Ch"),
            NameObject("/T"): TextStringObject("Color"),
            NameObject("/Opt"): ArrayObject(
                [
                    ArrayObject([TextStringObject("R"), TextStringObject("Red")]),
                    TextStringObject("Blue"),
                ]
            ),
        }
    )


def text_fields_pdf_bytes(names: list[str], values: dict[str, str] | None = None) -> bytes:
    """One AcroForm whose text fields are `names`. `values` presets /V."""
    stored = values or {}
    widgets = []
    for name in names:
        extra = {
            NameObject("/FT"): NameObject("/Tx"),
            NameObject("/T"): TextStringObject(name),
        }
        if name in stored:
            extra[NameObject("/V")] = TextStringObject(stored[name])
        widgets.append(extra)
    return _fields_pdf(widgets)


def text_and_choice_pdf_bytes() -> bytes:
    return _fields_pdf(
        [
            {
                NameObject("/FT"): NameObject("/Tx"),
                NameObject("/T"): TextStringObject("Name"),
            },
            {
                NameObject("/FT"): NameObject("/Ch"),
                NameObject("/T"): TextStringObject("Color"),
                NameObject("/Opt"): ArrayObject(
                    [TextStringObject("Red"), TextStringObject("Blue")]
                ),
            },
        ]
    )


def _fields_pdf(widgets: list[dict]) -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    refs = []
    for extra in widgets:
        widget = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Annot"),
                NameObject("/Subtype"): NameObject("/Widget"),
                NameObject("/Rect"): ArrayObject(
                    [FloatObject(10), FloatObject(10), FloatObject(100), FloatObject(30)]
                ),
                NameObject("/F"): NumberObject(4),
            }
        )
        widget.update(extra)
        refs.append(writer.add_annotation(0, widget).indirect_reference)
    writer._root_object[NameObject("/AcroForm")] = DictionaryObject(
        {NameObject("/Fields"): ArrayObject(refs)}
    )
    return _bytes(writer)


def _widget_pdf(extra: dict) -> bytes:
    return _fields_pdf([extra])


def _bytes(writer: PdfWriter) -> bytes:
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()
