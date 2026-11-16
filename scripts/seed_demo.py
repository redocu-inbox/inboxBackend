"""Create one local inbox for development.

Run from the repo root, only when APP_ENV is development:

    python scripts/seed_demo.py

The password is documented in the README. This script does not connect Gmail
and does not print tokens.
"""

import sys
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pypdf import PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, FloatObject, NameObject, NumberObject, TextStringObject
from sqlmodel import Session, select

from app.config import get_settings
from app.db import engine
from app.models.connected_account import ConnectedAccount
from app.models.message import Message
from app.models.user import User
from app.security import hash_password
from app.services.forms.pipeline import save_template

DEMO_EMAIL = "demo@example.com"
DEMO_PASSWORD = "seed-demo-password"


def _acroform_pdf() -> bytes:
    """One text field named Name. Enough for the form library to read it."""
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
            NameObject("/FT"): NameObject("/Tx"),
            NameObject("/T"): TextStringObject("Name"),
        }
    )
    ref = writer.add_annotation(0, widget).indirect_reference
    writer._root_object[NameObject("/AcroForm")] = DictionaryObject(
        {NameObject("/Fields"): ArrayObject([ref])}
    )
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def main() -> None:
    """Insert the demo user, three messages, and one template. Exit 1 otherwise."""
    if get_settings().app_env != "development":
        print("seed_demo runs only when APP_ENV is development", file=sys.stderr)
        raise SystemExit(1)
    with Session(engine) as session:
        existing = session.exec(select(User).where(User.email == DEMO_EMAIL)).first()
        if existing is not None:
            print(f"{DEMO_EMAIL} is already seeded")
            return
        user = User(email=DEMO_EMAIL, hashed_password=hash_password(DEMO_PASSWORD))
        session.add(user)
        session.commit()
        session.refresh(user)
        account = ConnectedAccount(
            user_id=user.id,
            provider="microsoft",
            provider_account_id="seed-demo",
            email="demo@outlook.example",
            sync_status="idle",
        )
        session.add(account)
        session.commit()
        session.refresh(account)
        received = datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)
        rows = (
            ("Roof leak at the school", 90.0, "high", 0),
            ("Updated drawing set", 50.0, "medium", 1),
            ("Office newsletter", 10.0, "low", 2),
        )
        for subject, score, band, offset in rows:
            session.add(
                Message(
                    user_id=user.id,
                    connected_account_id=account.id,
                    graph_id=f"seed-{offset}",
                    subject=subject,
                    body_preview=subject,
                    received_at=received + timedelta(hours=offset),
                    priority_score=score,
                    priority_band=band,
                )
            )
        session.commit()
        save_template(
            session,
            user_id=user.id,
            filename="daily-report.pdf",
            data=_acroform_pdf(),
            mime_type="application/pdf",
        )
    print(f"Seeded {DEMO_EMAIL}")


if __name__ == "__main__":
    main()
