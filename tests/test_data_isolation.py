"""One user must never be able to reach another user's data."""

from datetime import datetime, timezone
from uuid import UUID

import pytest
from sqlmodel import Session

from app.db import engine
from app.models.connected_account import ConnectedAccount
from app.models.message import Message
from tests.pdf_fixtures import blank_pdf_bytes

PDF_UPLOAD = {"file": ("form.pdf", blank_pdf_bytes(), "application/pdf")}


def _upload_template(client, headers) -> str:
    response = client.post("/forms/templates", headers=headers, files=PDF_UPLOAD)
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _insert_message(user_id: str) -> str:
    owner = UUID(user_id)
    with Session(engine) as session:
        account = ConnectedAccount(
            user_id=owner,
            provider_account_id=f"oid-{user_id}",
            email="mailbox@example.com",
            access_token="access",
        )
        session.add(account)
        session.flush()
        message = Message(
            user_id=owner,
            connected_account_id=account.id,
            graph_id="graph-1",
            received_at=datetime.now(timezone.utc),
        )
        session.add(message)
        session.commit()
        return str(message.id)


@pytest.fixture
def ana(register_user):
    return register_user("ana@example.com")


@pytest.fixture
def bob(register_user):
    return register_user("bob@example.com")


@pytest.mark.parametrize(
    "method, path, kwargs",
    [
        ("post", "/forms/templates", {"files": PDF_UPLOAD}),
        ("get", "/forms/templates", {}),
        ("get", "/forms/templates/00000000-0000-0000-0000-000000000000", {}),
        (
            "patch",
            "/forms/templates/00000000-0000-0000-0000-000000000000",
            {"json": {}},
        ),
        ("delete", "/forms/templates/00000000-0000-0000-0000-000000000000", {}),
        (
            "post",
            "/forms/jobs",
            {"json": {"template_id": "00000000-0000-0000-0000-000000000000"}},
        ),
        ("get", "/forms/jobs/00000000-0000-0000-0000-000000000000", {}),
        ("get", "/users/me", {}),
        ("patch", "/users/me", {"json": {}}),
        ("get", "/messages", {}),
        ("get", "/messages/00000000-0000-0000-0000-000000000000", {}),
        ("post", "/messages/sync", {}),
        ("post", "/auth/logout", {}),
        ("post", "/auth/microsoft/disconnect", {}),
        ("post", "/auth/google/disconnect", {}),
    ],
)
def test_user_routes_require_auth(client, method, path, kwargs):
    assert getattr(client, method)(path, **kwargs).status_code == 401


def test_health_and_webhook_stay_public(client):
    # /health reports dependency status, so only the absence of a 401 matters here.
    assert client.get("/health").status_code != 401
    handshake = client.post("/webhooks/graph", params={"validationToken": "abc"})
    assert handshake.status_code == 200
    assert handshake.text == "abc"


def test_me_returns_the_callers_own_profile(client, ana, bob):
    assert client.get("/users/me", headers=ana).json()["email"] == "ana@example.com"
    assert client.get("/users/me", headers=bob).json()["email"] == "bob@example.com"


def test_cannot_read_another_users_job(client, ana, bob):
    template_id = _upload_template(client, ana)
    job = client.post(
        "/forms/jobs",
        headers=ana,
        json={"template_id": template_id, "values": {"Name": "Ada"}},
    ).json()

    assert client.get(f"/forms/jobs/{job['id']}", headers=ana).status_code == 200
    assert client.get(f"/forms/jobs/{job['id']}", headers=bob).status_code == 404


def test_cannot_use_another_users_template(client, ana, bob):
    template_id = _upload_template(client, ana)
    response = client.post(
        "/forms/jobs",
        headers=bob,
        json={"template_id": template_id, "values": {"Name": "Ada"}},
    )
    assert response.status_code == 404


def test_cannot_attach_another_users_message(client, ana, bob):
    ana_id = client.get("/users/me", headers=ana).json()["id"]
    ana_message = _insert_message(ana_id)
    bob_template = _upload_template(client, bob)

    response = client.post(
        "/forms/jobs",
        headers=bob,
        json={"template_id": bob_template, "message_id": ana_message},
    )
    assert response.status_code == 404


def test_can_attach_own_message(client, ana):
    ana_id = client.get("/users/me", headers=ana).json()["id"]
    message = _insert_message(ana_id)
    template = _upload_template(client, ana)

    response = client.post(
        "/forms/jobs", headers=ana, json={"template_id": template, "message_id": message}
    )
    assert response.status_code == 200
