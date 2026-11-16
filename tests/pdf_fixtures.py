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


def _widget_pdf(extra: dict) -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
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
    added = writer.add_annotation(0, widget)
    writer._root_object[NameObject("/AcroForm")] = DictionaryObject(
        {NameObject("/Fields"): ArrayObject([added.indirect_reference])}
    )
    return _bytes(writer)


def _bytes(writer: PdfWriter) -> bytes:
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()
