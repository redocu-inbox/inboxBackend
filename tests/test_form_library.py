"""Phase 8: upload reads AcroForm fields, and the library is per user."""

from datetime import datetime, timezone
from uuid import UUID

from pypdf.generic import NameObject
from sqlmodel import Session, select

from app.db import engine
from app.models.form_fill_job import FormFillJob
from app.models.form_template import FormTemplate
from app.services.forms.schema import _field_record
from tests.pdf_fixtures import blank_pdf_bytes, choice_field_pdf_bytes, text_field_pdf_bytes

_SUMMARY_KEYS = {
    "id",
    "filename",
    "mime_type",
    "tags",
    "description",
    "kind",
    "field_count",
    "created_at",
}


def _upload(client, headers, data, filename="form.pdf", content_type="application/pdf"):
    return client.post(
        "/forms/templates",
        headers=headers,
        files={"file": (filename, data, content_type)},
    )


def test_one_field_pdf_returns_that_field(client, register_user):
    headers = register_user("fields@example.com")
    uploaded = _upload(client, headers, text_field_pdf_bytes(), filename="waiver.pdf")
    assert uploaded.status_code == 200, uploaded.text
    body = uploaded.json()
    assert set(body) == {"id", "filename", "mime_type", "kind", "field_count"}
    assert body["filename"] == "waiver.pdf"
    assert body["kind"] == "acroform"
    assert body["field_count"] == 1

    detail = client.get(f"/forms/templates/{body['id']}", headers=headers)
    assert detail.status_code == 200, detail.text
    schema = detail.json()["schema"]
    assert schema["kind"] == "acroform"
    assert schema["fields"] == [{"name": "FullName", "type": "text", "options": []}]
    assert "label" not in schema["fields"][0]


def test_pdf_with_no_fields_is_kind_none(client, register_user):
    headers = register_user("empty@example.com")
    uploaded = _upload(client, headers, blank_pdf_bytes())
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["kind"] == "none"
    assert uploaded.json()["field_count"] == 0

    detail = client.get(f"/forms/templates/{uploaded.json()['id']}", headers=headers)
    assert detail.json()["schema"] == {"kind": "none", "fields": []}


def test_choice_options_are_export_values(client, register_user):
    headers = register_user("choice@example.com")
    uploaded = _upload(client, headers, choice_field_pdf_bytes())
    assert uploaded.status_code == 200, uploaded.text
    detail = client.get(f"/forms/templates/{uploaded.json()['id']}", headers=headers)
    field = detail.json()["schema"]["fields"][0]
    assert field["name"] == "Color"
    assert field["type"] == "choice"
    assert field["options"] == ["R", "Blue"]


def test_button_types_and_radio_states():
    assert _field_record("Agree", {"/FT": NameObject("/Btn"), "/Ff": 0}) == {
        "name": "Agree",
        "type": "checkbox",
        "options": [],
    }
    assert _field_record("Go", {"/FT": NameObject("/Btn"), "/Ff": 1 << 16})["type"] == "other"
    radio = _field_record(
        "Choice",
        {
            "/FT": NameObject("/Btn"),
            "/Ff": 1 << 15,
            "/_States_": [NameObject("/Yes"), NameObject("/Off"), NameObject("/No")],
        },
    )
    assert radio == {"name": "Choice", "type": "choice", "options": ["Yes", "No"]}
    assert _field_record("Sign", {"/FT": NameObject("/Sig")})["type"] == "other"


def test_oversize_non_pdf_and_corrupt_store_nothing(client, register_user, tmp_path):
    headers = register_user("reject@example.com")

    too_big = _upload(client, headers, b"0" * (11 * 1024 * 1024))
    assert too_big.status_code == 413
    assert too_big.json() == {"detail": "PDF must be 10 MB or smaller."}

    other_type = client.post(
        "/forms/templates",
        headers=headers,
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert other_type.status_code == 415
    assert other_type.json() == {"detail": "Only application/pdf is accepted"}

    corrupt = _upload(client, headers, b"%PDF-1.4")
    assert corrupt.status_code == 422
    assert corrupt.json() == {"detail": "This PDF could not be read."}

    assert list(tmp_path.rglob("*")) == []
    with Session(engine) as session:
        assert session.exec(select(FormTemplate)).all() == []


def test_list_is_newest_first_and_hides_other_users(client, register_user):
    ana = register_user("ana-lib@example.com")
    bob = register_user("bob-lib@example.com")
    older = _upload(client, ana, blank_pdf_bytes(), filename="older.pdf").json()
    newer = _upload(client, ana, blank_pdf_bytes(), filename="newer.pdf").json()
    bob_upload = _upload(client, bob, blank_pdf_bytes(), filename="bob.pdf").json()

    with Session(engine) as session:
        row = session.get(FormTemplate, UUID(older["id"]))
        row.created_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
        session.add(row)
        session.commit()

    listed = client.get("/forms/templates", headers=ana)
    assert listed.status_code == 200
    items = listed.json()
    assert [item["id"] for item in items] == [newer["id"], older["id"]]
    assert set(items[0]) == _SUMMARY_KEYS
    assert "schema" not in items[0]
    assert bob_upload["id"] not in {item["id"] for item in items}

    foreign = client.get(f"/forms/templates/{newer['id']}", headers=bob)
    assert foreign.status_code == 404
    assert foreign.json() == {"detail": "Form template not found."}
    missing = client.get(
        "/forms/templates/00000000-0000-0000-0000-000000000000",
        headers=ana,
    )
    assert missing.status_code == 404
    assert missing.json() == {"detail": "Form template not found."}


def test_delete_removes_the_file_and_cascades_the_job(client, register_user, tmp_path):
    ana = register_user("ana-del@example.com")
    bob = register_user("bob-del@example.com")
    uploaded = _upload(client, ana, blank_pdf_bytes(), filename="keep.pdf")
    assert uploaded.status_code == 200, uploaded.text
    template_id = uploaded.json()["id"]
    job = client.post("/forms/jobs", headers=ana, json={"template_id": template_id})
    assert job.status_code == 200, job.text
    stored = list(tmp_path.rglob("keep.pdf"))
    assert len(stored) == 1

    rejected = client.delete(f"/forms/templates/{template_id}", headers=bob)
    assert rejected.status_code == 404
    assert stored[0].is_file()

    removed = client.delete(f"/forms/templates/{template_id}", headers=ana)
    assert removed.status_code == 204
    assert removed.content == b""
    assert not stored[0].exists()
    gone = client.get(f"/forms/templates/{template_id}", headers=ana)
    assert gone.status_code == 404
    with Session(engine) as session:
        assert session.get(FormTemplate, UUID(template_id)) is None
        assert session.get(FormFillJob, UUID(job.json()["id"])) is None


def test_delete_succeeds_when_the_file_is_already_gone(client, register_user, tmp_path):
    headers = register_user("missing-file@example.com")
    uploaded = _upload(client, headers, blank_pdf_bytes(), filename="gone.pdf")
    template_id = uploaded.json()["id"]
    stored = list(tmp_path.rglob("gone.pdf"))
    assert len(stored) == 1
    stored[0].unlink()

    removed = client.delete(f"/forms/templates/{template_id}", headers=headers)
    assert removed.status_code == 204
    assert client.get(f"/forms/templates/{template_id}", headers=headers).status_code == 404


def test_patch_saves_tags_description_and_label(client, register_user):
    headers = register_user("edit@example.com")
    template_id = _upload(client, headers, text_field_pdf_bytes()).json()["id"]

    saved = client.patch(
        f"/forms/templates/{template_id}",
        headers=headers,
        json={
            "tags": [" lien ", "waiver"],
            "description": "Site waiver",
            "field_labels": {"FullName": "Full name"},
        },
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert set(body) == _SUMMARY_KEYS | {"schema"}
    assert body["tags"] == ["lien", "waiver"]
    assert body["description"] == "Site waiver"
    assert body["schema"]["fields"] == [
        {"name": "FullName", "type": "text", "options": [], "label": "Full name"}
    ]

    only_description = client.patch(
        f"/forms/templates/{template_id}",
        headers=headers,
        json={"description": "Updated"},
    )
    assert only_description.json()["tags"] == ["lien", "waiver"]
    assert only_description.json()["schema"]["fields"][0]["label"] == "Full name"

    cleared = client.patch(
        f"/forms/templates/{template_id}",
        headers=headers,
        json={"description": None},
    )
    assert cleared.json()["description"] is None
    assert cleared.json()["tags"] == ["lien", "waiver"]


def test_patch_rejects_unknown_field_without_writing(client, register_user):
    headers = register_user("unknown@example.com")
    template_id = _upload(client, headers, text_field_pdf_bytes()).json()["id"]
    client.patch(
        f"/forms/templates/{template_id}",
        headers=headers,
        json={"tags": ["kept"], "description": "Hello"},
    )

    rejected = client.patch(
        f"/forms/templates/{template_id}",
        headers=headers,
        json={"tags": ["nope"], "field_labels": {"Missing": "x"}},
    )
    assert rejected.status_code == 422
    assert rejected.json() == {"detail": "Unknown field name."}

    detail = client.get(f"/forms/templates/{template_id}", headers=headers).json()
    assert detail["tags"] == ["kept"]
    assert detail["description"] == "Hello"
    assert "label" not in detail["schema"]["fields"][0]
    assert detail["schema"]["fields"][0]["name"] == "FullName"


def test_patch_rejects_overlong_tags_and_description(client, register_user):
    headers = register_user("limits@example.com")
    template_id = _upload(client, headers, blank_pdf_bytes()).json()["id"]

    too_many = client.patch(
        f"/forms/templates/{template_id}",
        headers=headers,
        json={"tags": [f"t{i}" for i in range(21)]},
    )
    assert too_many.status_code == 422
    assert too_many.json() == {"detail": "At most 20 tags."}

    too_long = client.patch(
        f"/forms/templates/{template_id}",
        headers=headers,
        json={"tags": ["x" * 41]},
    )
    assert too_long.status_code == 422
    assert too_long.json() == {"detail": "Each tag must be 1 to 40 characters."}

    blank_tag = client.patch(
        f"/forms/templates/{template_id}",
        headers=headers,
        json={"tags": ["   "]},
    )
    assert blank_tag.status_code == 422

    description = client.patch(
        f"/forms/templates/{template_id}",
        headers=headers,
        json={"description": "x" * 2001},
    )
    assert description.status_code == 422
    assert description.json() == {"detail": "Description must be 2,000 characters or fewer."}

    detail = client.get(f"/forms/templates/{template_id}", headers=headers).json()
    assert detail["tags"] == []
    assert detail["description"] is None


def test_unsafe_filename_stays_inside_storage(client, register_user, tmp_path):
    headers = register_user("name@example.com")
    nested = _upload(client, headers, blank_pdf_bytes(), filename="../../etc/passwd")
    assert nested.status_code == 200, nested.text
    assert nested.json()["filename"] == "passwd"
    stored = list(tmp_path.rglob("passwd"))
    assert len(stored) == 1
    assert ".." not in stored[0].parts

    dots = _upload(client, headers, blank_pdf_bytes(), filename="..")
    assert dots.status_code == 200, dots.text
    assert dots.json()["filename"] == "template.pdf"


def test_legacy_empty_schema_reads_as_none(client, register_user):
    headers = register_user("legacy@example.com")
    user_id = client.get("/users/me", headers=headers).json()["id"]
    with Session(engine) as session:
        row = FormTemplate(
            user_id=UUID(user_id),
            filename="old.pdf",
            storage_path=f"forms/{user_id}/legacy/old.pdf",
            mime_type="application/pdf",
            tags=[],
            form_schema={},
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        template_id = row.id

    detail = client.get(f"/forms/templates/{template_id}", headers=headers)
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["kind"] == "none"
    assert body["field_count"] == 0
    assert body["schema"] == {"kind": "none", "fields": []}


def test_fill_job_routes_are_unchanged(client, register_user):
    headers = register_user("jobs@example.com")
    template_id = _upload(client, headers, blank_pdf_bytes()).json()["id"]
    created = client.post("/forms/jobs", headers=headers, json={"template_id": template_id})
    assert created.status_code == 200, created.text
    assert set(created.json()) == {"id", "status"}
    assert created.json()["status"] == "pending"
    client.app.state.arq.enqueue_job.assert_awaited_once_with(
        "match_form_template", created.json()["id"]
    )

    fetched = client.get(f"/forms/jobs/{created.json()['id']}", headers=headers)
    assert fetched.status_code == 200
    assert set(fetched.json()) == {"status", "confidence", "error", "extracted_values"}
