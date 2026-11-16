"""Phase 11: fill-job retries and the development seed."""

import asyncio
import logging

import pytest
from sqlmodel import Session, select

from app.config import get_settings
from app.db import engine
from app.models.connected_account import ConnectedAccount
from app.models.form_fill_job import FormFillJob
from app.worker.form_tasks import run_form_fill
from scripts.seed_demo import DEMO_EMAIL, DEMO_PASSWORD, main
from tests.pdf_fixtures import text_field_pdf_bytes


class _Boom:
    def __init__(self, fails_remaining: list[int]):
        self.fails_remaining = fails_remaining

    def fill(self, template, values):
        if self.fails_remaining[0] > 0:
            self.fails_remaining[0] -= 1
            raise RuntimeError("secret Ada")
        from app.services.forms.filler import PdfFormFiller

        return PdfFormFiller().fill(template, values)


def _upload(client, headers):
    return client.post(
        "/forms/templates",
        headers=headers,
        files={"file": ("form.pdf", text_field_pdf_bytes("Name"), "application/pdf")},
    )


def _enable_app_logs():
    for name, obj in logging.root.manager.loggerDict.items():
        if isinstance(obj, logging.Logger) and (name == "app" or name.startswith("app.")):
            obj.disabled = False


def test_filler_succeeds_on_the_third_try(client, register_user, monkeypatch):
    headers = register_user("retry@example.com")
    template_id = _upload(client, headers).json()["id"]
    created = client.post(
        "/forms/jobs",
        headers=headers,
        json={"template_id": template_id, "values": {"Name": "Ada"}},
    )
    job_id = created.json()["id"]
    remaining = [2]
    monkeypatch.setattr(
        "app.services.forms.fill_job.get_filler",
        lambda mime: _Boom(remaining),
    )

    with pytest.raises(RuntimeError):
        asyncio.run(run_form_fill({"job_try": 1}, job_id))
    with Session(engine) as session:
        assert session.get(FormFillJob, job_id).status == "running"

    with pytest.raises(RuntimeError):
        asyncio.run(run_form_fill({"job_try": 2}, job_id))
    with Session(engine) as session:
        assert session.get(FormFillJob, job_id).status == "running"

    asyncio.run(run_form_fill({"job_try": 3}, job_id))
    body = client.get(f"/forms/jobs/{job_id}", headers=headers).json()
    assert body["status"] == "completed"
    assert body["confidence"] == 1.0
    assert body["extracted_values"] == {"Name": "Ada"}


def test_filler_fails_after_three_tries(client, register_user, monkeypatch, caplog):
    headers = register_user("always@example.com")
    template_id = _upload(client, headers).json()["id"]
    created = client.post(
        "/forms/jobs",
        headers=headers,
        json={"template_id": template_id, "values": {"Name": "Ada"}},
    )
    job_id = created.json()["id"]
    monkeypatch.setattr(
        "app.services.forms.fill_job.get_filler",
        lambda mime: _Boom([99]),
    )
    _enable_app_logs()
    caplog.set_level(logging.INFO)
    worker_log = logging.getLogger("app.services.forms.fill_job")
    worker_log.addHandler(caplog.handler)

    try:
        with pytest.raises(RuntimeError):
            asyncio.run(run_form_fill({"job_try": 1}, job_id))
        with pytest.raises(RuntimeError):
            asyncio.run(run_form_fill({"job_try": 2}, job_id))
        asyncio.run(run_form_fill({"job_try": 3}, job_id))
    finally:
        worker_log.removeHandler(caplog.handler)

    body = client.get(f"/forms/jobs/{job_id}", headers=headers).json()
    assert body["status"] == "failed"
    assert body["error"] == "The form could not be filled."
    assert "Ada" not in body["error"]
    with Session(engine) as session:
        assert session.get(FormFillJob, job_id).status == "failed"
    assert "run_form_fill finished" in caplog.text
    assert "status=failed" in caplog.text
    assert "Ada" not in caplog.text


def test_seed_refuses_outside_development(capsys):
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 1
    assert "development" in capsys.readouterr().err
    with Session(engine) as session:
        assert session.exec(select(ConnectedAccount)).first() is None


def test_seed_creates_the_demo_inbox(client, monkeypatch, capsys):
    monkeypatch.setenv("APP_ENV", "development")
    get_settings.cache_clear()
    try:
        main()
        captured = capsys.readouterr().out
        assert DEMO_EMAIL in captured
        assert DEMO_PASSWORD not in captured
        login = client.post(
            "/auth/login",
            json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD},
        )
        assert login.status_code == 200, login.text
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        messages = client.get("/messages", headers=headers).json()
        assert sorted(item["priority_score"] for item in messages) == [10.0, 50.0, 90.0]
        assert {item["provider"] for item in messages} == {"microsoft"}
        templates = client.get("/forms/templates", headers=headers).json()
        assert len(templates) == 1
        assert templates[0]["kind"] == "acroform"
        assert templates[0]["field_count"] == 1
        with Session(engine) as session:
            providers = {row.provider for row in session.exec(select(ConnectedAccount)).all()}
        assert providers == {"microsoft"}
    finally:
        get_settings.cache_clear()
