"""Phase 9A: known values fill an AcroForm the owner can download."""

import asyncio
import logging
from io import BytesIO
from uuid import UUID, uuid4

from pypdf import PdfReader
from sqlmodel import Session

from app.db import engine
from app.models.form_fill_job import FormFillJob
from app.services.forms.filler import PdfFormFiller
from app.worker.tasks import run_form_fill
from tests.pdf_fixtures import (
    blank_pdf_bytes,
    checkbox_field_pdf_bytes,
    signature_field_pdf_bytes,
    text_and_choice_pdf_bytes,
    text_field_pdf_bytes,
    text_fields_pdf_bytes,
)

_JOB_KEYS = {
    "id",
    "template_id",
    "message_id",
    "status",
    "confidence",
    "extracted_values",
    "dropped_fields",
    "error",
    "file_ready",
}


def _values(data: bytes) -> dict[str, str]:
    fields = PdfReader(BytesIO(data)).get_fields() or {}
    return {
        name: str(field["/V"])
        for name, field in fields.items()
        if isinstance(field, dict) and "/V" in field
    }


def _upload(client, headers, data, filename="form.pdf"):
    return client.post(
        "/forms/templates",
        headers=headers,
        files={"file": (filename, data, "application/pdf")},
    )


def _run(job_id: str) -> None:
    asyncio.run(run_form_fill({}, job_id))


def test_text_checkbox_and_choice_rules():
    filler = PdfFormFiller()
    text = filler.fill(text_field_pdf_bytes("Name"), {"Name": "Ada"})
    assert text.dropped == []
    assert _values(text.pdf)["Name"] == "Ada"

    preset = filler.fill(
        text_fields_pdf_bytes(["Name", "City"], {"City": "Boston"}),
        {"Name": "Ada", "Extra": "nope"},
    )
    assert preset.dropped == ["Extra"]
    assert _values(preset.pdf)["Name"] == "Ada"
    assert _values(preset.pdf)["City"] == "Boston"

    box = checkbox_field_pdf_bytes()
    assert _values(filler.fill(box, {"Agree": True}).pdf)["Agree"] == "/Yes"
    assert _values(filler.fill(box, {"Agree": "YES"}).pdf)["Agree"] == "/Yes"
    assert _values(filler.fill(box, {"Agree": 1}).pdf)["Agree"] == "/Yes"
    assert _values(filler.fill(box, {"Agree": "on"}).pdf)["Agree"] == "/Yes"
    assert _values(filler.fill(box, {"Agree": False}).pdf)["Agree"] == "/Off"
    assert _values(filler.fill(box, {"Agree": "no"}).pdf)["Agree"] == "/Off"
    assert _values(filler.fill(box, {"Agree": 0}).pdf)["Agree"] == "/Off"
    assert _values(filler.fill(box, {"Agree": "off"}).pdf)["Agree"] == "/Off"
    assert _values(filler.fill(box, {"Agree": ""}).pdf)["Agree"] == "/Off"
    assert filler.fill(box, {"Agree": "maybe"}).dropped == ["Agree"]

    mixed = filler.fill(
        text_and_choice_pdf_bytes(),
        {"Name": "Ada", "Color": "Blue", "ColorExtra": "nope"},
    )
    assert mixed.dropped == ["ColorExtra"]
    assert _values(mixed.pdf)["Name"] == "Ada"
    assert _values(mixed.pdf)["Color"] == "Blue"
    rejected = filler.fill(text_and_choice_pdf_bytes(), {"Color": "Green", "Name": "Ada"})
    assert rejected.dropped == ["Color"]
    assert _values(rejected.pdf)["Name"] == "Ada"
    assert "Color" not in _values(rejected.pdf)

    signed = filler.fill(signature_field_pdf_bytes(), {"Sign": "Ada"})
    assert signed.dropped == ["Sign"]


def test_name_filled_with_ada_is_read_back(client, register_user, tmp_path):
    headers = register_user("ada@example.com")
    template_id = _upload(client, headers, text_field_pdf_bytes("Name")).json()["id"]
    created = client.post(
        "/forms/jobs",
        headers=headers,
        json={"template_id": template_id, "values": {"Name": "Ada"}},
    )
    assert created.status_code == 200, created.text
    assert set(created.json()) == {"id", "status"}
    assert created.json()["status"] == "pending"
    client.app.state.arq.enqueue_job.assert_awaited_once_with(
        "run_form_fill", created.json()["id"]
    )

    pending = client.get(f"/forms/jobs/{created.json()['id']}", headers=headers).json()
    assert set(pending) == _JOB_KEYS
    assert pending["confidence"] == 1.0
    assert pending["extracted_values"] == {"Name": "Ada"}
    assert pending["file_ready"] is False

    _run(created.json()["id"])
    done = client.get(f"/forms/jobs/{created.json()['id']}", headers=headers)
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["status"] == "completed"
    assert body["confidence"] == 1.0
    assert body["dropped_fields"] == []
    assert body["error"] is None
    assert body["file_ready"] is True

    downloaded = client.get(f"/forms/jobs/{created.json()['id']}/file", headers=headers)
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"] == "application/pdf"
    assert _values(downloaded.content)["Name"] == "Ada"
    assert len(list(tmp_path.rglob("filled.pdf"))) == 1


def test_unknown_key_needs_review_and_keeps_known_fields(client, register_user):
    headers = register_user("drop@example.com")
    template_id = _upload(
        client,
        headers,
        text_fields_pdf_bytes(["Name", "City"], {"City": "Boston"}),
    ).json()["id"]
    created = client.post(
        "/forms/jobs",
        headers=headers,
        json={"template_id": template_id, "values": {"Name": "Ada", "Extra": "nope"}},
    )
    _run(created.json()["id"])
    body = client.get(f"/forms/jobs/{created.json()['id']}", headers=headers).json()
    assert body["status"] == "needs_review"
    assert body["dropped_fields"] == ["Extra"]
    assert body["file_ready"] is True
    assert body["confidence"] == 1.0
    downloaded = client.get(f"/forms/jobs/{created.json()['id']}/file", headers=headers)
    assert _values(downloaded.content)["Name"] == "Ada"
    assert _values(downloaded.content)["City"] == "Boston"


def test_kind_none_fails_without_a_file(client, register_user, tmp_path):
    headers = register_user("none@example.com")
    template_id = _upload(client, headers, blank_pdf_bytes()).json()["id"]
    created = client.post(
        "/forms/jobs",
        headers=headers,
        json={"template_id": template_id, "values": {"Name": "Ada"}},
    )
    _run(created.json()["id"])
    body = client.get(f"/forms/jobs/{created.json()['id']}", headers=headers).json()
    assert body["status"] == "failed"
    assert body["error"] == "This template has no fillable fields."
    assert body["file_ready"] is False
    assert list(tmp_path.rglob("filled.pdf")) == []
    missing = client.get(f"/forms/jobs/{created.json()['id']}/file", headers=headers)
    assert missing.status_code == 404
    assert missing.json() == {"detail": "Fill job not found"}


def test_empty_values_need_review_and_no_file(client, register_user, tmp_path):
    headers = register_user("empty@example.com")
    template_id = _upload(client, headers, text_field_pdf_bytes("Name")).json()["id"]
    created = client.post(
        "/forms/jobs",
        headers=headers,
        json={"template_id": template_id, "values": {}},
    )
    assert created.status_code == 200, created.text
    client.app.state.arq.enqueue_job.assert_awaited_once_with(
        "run_form_fill", created.json()["id"]
    )
    _run(created.json()["id"])
    body = client.get(f"/forms/jobs/{created.json()['id']}", headers=headers).json()
    assert body["status"] == "needs_review"
    assert body["file_ready"] is False
    assert body["extracted_values"] == {}
    assert list(tmp_path.rglob("filled.pdf")) == []


def test_other_user_cannot_read_the_job_or_the_file(client, register_user):
    ana = register_user("ana-fill@example.com")
    bob = register_user("bob-fill@example.com")
    template_id = _upload(client, ana, text_field_pdf_bytes("Name")).json()["id"]
    created = client.post(
        "/forms/jobs",
        headers=ana,
        json={"template_id": template_id, "values": {"Name": "Ada"}},
    )
    job_id = created.json()["id"]
    _run(job_id)

    for path in (f"/forms/jobs/{job_id}", f"/forms/jobs/{job_id}/file"):
        foreign = client.get(path, headers=bob)
        assert foreign.status_code == 404
        assert foreign.json() == {"detail": "Fill job not found"}
    refill = client.post(f"/forms/jobs/{job_id}/refill", headers=bob, json={"values": {"Name": "Grace"}})
    assert refill.status_code == 404
    assert refill.json() == {"detail": "Fill job not found"}
    own = client.get(f"/forms/jobs/{job_id}/file", headers=ana)
    assert own.status_code == 200
    assert _values(own.content)["Name"] == "Ada"


def test_refill_replaces_values_and_second_run_overwrites_the_same_key(client, register_user, tmp_path):
    headers = register_user("refill@example.com")
    template_id = _upload(client, headers, text_field_pdf_bytes("Name")).json()["id"]
    created = client.post(
        "/forms/jobs",
        headers=headers,
        json={"template_id": template_id, "values": {"Name": "Ada"}},
    )
    job_id = created.json()["id"]
    _run(job_id)
    client.app.state.arq.enqueue_job.reset_mock()

    refilled = client.post(
        f"/forms/jobs/{job_id}/refill",
        headers=headers,
        json={"values": {"Name": "Grace"}},
    )
    assert refilled.status_code == 200, refilled.text
    assert refilled.json()["status"] == "pending"
    client.app.state.arq.enqueue_job.assert_awaited_once_with("run_form_fill", job_id)
    waiting = client.get(f"/forms/jobs/{job_id}", headers=headers).json()
    assert waiting["extracted_values"] == {"Name": "Grace"}
    assert waiting["file_ready"] is False
    assert waiting["confidence"] == 1.0

    _run(job_id)
    body = client.get(f"/forms/jobs/{job_id}", headers=headers).json()
    assert body["status"] == "completed"
    assert body["confidence"] == 1.0
    assert _values(client.get(f"/forms/jobs/{job_id}/file", headers=headers).content)["Name"] == "Grace"
    stored = list(tmp_path.rglob("filled.pdf"))
    assert len(stored) == 1
    assert "filled.pdf" in stored[0].parts


def test_filler_does_not_overwrite_confidence(client, register_user):
    headers = register_user("confidence@example.com")
    template_id = _upload(client, headers, text_field_pdf_bytes("Name")).json()["id"]
    created = client.post(
        "/forms/jobs",
        headers=headers,
        json={"template_id": template_id, "values": {"Name": "Ada"}},
    )
    job_id = created.json()["id"]
    with Session(engine) as session:
        job = session.get(FormFillJob, UUID(job_id))
        job.confidence = 0.25
        session.add(job)
        session.commit()
    _run(job_id)
    body = client.get(f"/forms/jobs/{job_id}", headers=headers).json()
    assert body["status"] == "completed"
    assert body["confidence"] == 0.25


def test_unexpected_fill_error_is_a_short_message(client, register_user, monkeypatch):
    headers = register_user("boom@example.com")
    template_id = _upload(client, headers, text_field_pdf_bytes("Name")).json()["id"]
    created = client.post(
        "/forms/jobs",
        headers=headers,
        json={"template_id": template_id, "values": {"Name": "Ada"}},
    )

    class Boom:
        def fill(self, template, values):
            raise RuntimeError("secret Ada")

    monkeypatch.setattr("app.services.forms.fill_job.get_filler", lambda mime: Boom())
    _run(created.json()["id"])
    body = client.get(f"/forms/jobs/{created.json()['id']}", headers=headers).json()
    assert body["status"] == "failed"
    assert body["error"] == "The form could not be filled."
    assert "Ada" not in body["error"]
    assert "Traceback" not in body["error"]
    assert body["file_ready"] is False


def test_missing_job_is_logged_and_ignored(caplog):
    for name, obj in logging.root.manager.loggerDict.items():
        if isinstance(obj, logging.Logger) and (name == "app" or name.startswith("app.")):
            obj.disabled = False
    caplog.set_level(logging.WARNING)
    missing = str(uuid4())
    asyncio.run(run_form_fill({}, missing))
    asyncio.run(run_form_fill({}, "not-a-uuid"))
    assert missing in caplog.text
    assert "was not found" in caplog.text
    assert "skipped invalid id" in caplog.text
