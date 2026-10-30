"""Map extracted field values onto a form template."""

from typing import Protocol

class FormFiller(Protocol):
    def fill(self, template: bytes, values: dict) -> bytes:
        """Return the filled template bytes."""


class PdfFormFiller:
    """TODO: write `values` into AcroForm fields and return the filled PDF."""

    def fill(self, template: bytes, values: dict) -> bytes:
        raise NotImplementedError("PDF filling is a later phase")

def get_filler(mime_type: str) -> FormFiller:
    if mime_type == "application/pdf":
        return PdfFormFiller()
    raise NotImplementedError(f"No form filler for mime type {mime_type!r}")