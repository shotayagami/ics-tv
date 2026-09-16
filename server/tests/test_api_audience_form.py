# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""AudienceForm/AudienceSubmission: サービス検証 + admin API (フォーム CRUD・投稿一覧)。"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from django.utils import timezone

from scheduling.models import AudienceForm, AudienceFormKind, Series


def _series(channel):
    return Series.objects.create(channel=channel, title="Radio Show")


def _form(
    series, *, kind=AudienceFormKind.MESSAGE, enabled=True, requires_login=False, fields=None
):
    return AudienceForm.objects.create(
        series=series,
        kind=kind,
        title="メッセージを送る",
        enabled=enabled,
        requires_login=requires_login,
        fields=fields
        or [
            {
                "key": "radio_name",
                "label": "ラジオネーム",
                "type": "text",
                "required": True,
                "options": [],
                "help": "",
            },
            {
                "key": "message",
                "label": "メッセージ",
                "type": "textarea",
                "required": True,
                "options": [],
                "help": "",
            },
        ],
    )


BASE = "/api/v1/admin/scheduling/{slug}/series/{series_id}/forms"


# ===== validate_fields =====


def test_validate_fields_ok():
    from scheduling.services import validate_fields

    validate_fields(
        [
            {
                "key": "name",
                "label": "名前",
                "type": "text",
                "required": True,
                "options": [],
                "help": "",
            },
            {
                "key": "genre",
                "label": "選択",
                "type": "select",
                "required": False,
                "options": ["A", "B"],
                "help": "",
            },
        ]
    )


def test_validate_fields_missing_label():
    from scheduling.services import FieldSchemaError, validate_fields

    with pytest.raises(FieldSchemaError, match="label"):
        validate_fields(
            [{"key": "k", "label": "", "type": "text", "required": True, "options": [], "help": ""}]
        )


def test_validate_fields_duplicate_key():
    from scheduling.services import FieldSchemaError, validate_fields

    with pytest.raises(FieldSchemaError, match="重複"):
        validate_fields(
            [
                {
                    "key": "k",
                    "label": "A",
                    "type": "text",
                    "required": True,
                    "options": [],
                    "help": "",
                },
                {
                    "key": "k",
                    "label": "B",
                    "type": "text",
                    "required": False,
                    "options": [],
                    "help": "",
                },
            ]
        )


def test_validate_fields_select_needs_options():
    from scheduling.services import FieldSchemaError, validate_fields

    with pytest.raises(FieldSchemaError, match="options"):
        validate_fields(
            [
                {
                    "key": "sel",
                    "label": "選択",
                    "type": "select",
                    "required": False,
                    "options": [],
                    "help": "",
                }
            ]
        )


def test_validate_fields_invalid_type():
    from scheduling.services import FieldSchemaError, validate_fields

    with pytest.raises(FieldSchemaError, match="type"):
        validate_fields(
            [
                {
                    "key": "k",
                    "label": "X",
                    "type": "file",
                    "required": False,
                    "options": [],
                    "help": "",
                }
            ]
        )


# ===== validate_payload =====


def test_validate_payload_ok(channel, db):
    from scheduling.services import validate_payload

    af = _form(_series(channel))
    validate_payload(af, {"radio_name": "DJ田中", "message": "こんにちは"})


def test_validate_payload_missing_required(channel, db):
    from scheduling.services import PayloadValidationError, validate_payload

    af = _form(_series(channel))
    with pytest.raises(PayloadValidationError, match="必須"):
        validate_payload(af, {"message": "hi"})


def test_validate_payload_select_out_of_range(channel, db):
    from scheduling.services import PayloadValidationError, validate_payload

    af = AudienceForm.objects.create(
        series=_series(channel),
        kind=AudienceFormKind.MESSAGE,
        title="T",
        enabled=True,
        fields=[
            {
                "key": "area",
                "label": "地域",
                "type": "select",
                "required": True,
                "options": ["関東", "関西"],
                "help": "",
            }
        ],
    )
    with pytest.raises(PayloadValidationError, match="選択肢"):
        validate_payload(af, {"area": "四国"})


def test_validate_payload_email_format(channel, db):
    from scheduling.services import PayloadValidationError, validate_payload

    af = AudienceForm.objects.create(
        series=_series(channel),
        kind=AudienceFormKind.MESSAGE,
        title="T",
        enabled=True,
        fields=[
            {
                "key": "email",
                "label": "メール",
                "type": "email",
                "required": True,
                "options": [],
                "help": "",
            }
        ],
    )
    with pytest.raises(PayloadValidationError, match="メールアドレス"):
        validate_payload(af, {"email": "not-an-email"})


# ===== is_open =====


def test_is_open_message_always_open(channel, db):
    af = _form(_series(channel), kind=AudienceFormKind.MESSAGE)
    assert af.is_open(timezone.now())


def test_is_open_campaign_within_window(channel, db):
    now = timezone.now()
    af = AudienceForm.objects.create(
        series=_series(channel),
        kind=AudienceFormKind.CAMPAIGN,
        title="T",
        enabled=True,
        starts_at=now - timedelta(hours=1),
        ends_at=now + timedelta(hours=1),
    )
    assert af.is_open(now)


def test_is_open_campaign_before_start(channel, db):
    now = timezone.now()
    af = AudienceForm.objects.create(
        series=_series(channel),
        kind=AudienceFormKind.CAMPAIGN,
        title="T",
        enabled=True,
        starts_at=now + timedelta(hours=1),
        ends_at=now + timedelta(hours=2),
    )
    assert not af.is_open(now)


def test_is_open_campaign_after_end(channel, db):
    now = timezone.now()
    af = AudienceForm.objects.create(
        series=_series(channel),
        kind=AudienceFormKind.CAMPAIGN,
        title="T",
        enabled=True,
        starts_at=now - timedelta(hours=2),
        ends_at=now - timedelta(hours=1),
    )
    assert not af.is_open(now)


# ===== Admin API =====


def test_forms_list_requires_auth(channel, http_client, db):
    s = _series(channel)
    url = BASE.format(slug=channel.slug, series_id=s.id)
    assert http_client.get(url).status_code == 401


def test_forms_list_empty(staff_client, channel, db):
    s = _series(channel)
    url = BASE.format(slug=channel.slug, series_id=s.id)
    assert staff_client.get(url).json() == []


def test_forms_create_and_list(staff_client, channel, db):
    s = _series(channel)
    url = BASE.format(slug=channel.slug, series_id=s.id)
    body = {
        "kind": "message",
        "title": "送って",
        "description": "",
        "enabled": True,
        "requires_login": False,
        "fields": [
            {
                "key": "name",
                "label": "名前",
                "type": "text",
                "required": True,
                "options": [],
                "help": "",
            }
        ],
        "success_message": "送信しました",
        "notify_email": "",
        "starts_at": "",
        "ends_at": "",
        "prize": "",
    }
    resp = staff_client.post(url, data=json.dumps(body), content_type="application/json")
    assert resp.status_code == 200
    created = resp.json()
    assert created["title"] == "送って"
    assert created["submission_count"] == 0

    items = staff_client.get(url).json()
    assert len(items) == 1 and items[0]["id"] == created["id"]


def test_forms_update(staff_client, channel, db):
    s = _series(channel)
    af = _form(s)
    url = BASE.format(slug=channel.slug, series_id=s.id) + f"/{af.id}"
    body = {
        "kind": "message",
        "title": "更新後タイトル",
        "description": "",
        "enabled": False,
        "requires_login": False,
        "fields": [],
        "success_message": "OK",
        "notify_email": "",
        "starts_at": "",
        "ends_at": "",
        "prize": "",
    }
    resp = staff_client.put(url, data=json.dumps(body), content_type="application/json")
    assert resp.status_code == 200
    af.refresh_from_db()
    assert af.title == "更新後タイトル" and af.enabled is False


def test_forms_delete(staff_client, channel, db):
    s = _series(channel)
    af = _form(s)
    url = BASE.format(slug=channel.slug, series_id=s.id) + f"/{af.id}"
    assert staff_client.delete(url).status_code == 200
    assert not AudienceForm.objects.filter(pk=af.id).exists()


def test_forms_create_invalid_fields(staff_client, channel, db):
    """select 項目に options なしは 422。"""
    s = _series(channel)
    url = BASE.format(slug=channel.slug, series_id=s.id)
    body = {
        "kind": "message",
        "title": "T",
        "description": "",
        "enabled": True,
        "requires_login": False,
        "fields": [
            {
                "key": "sel",
                "label": "選択",
                "type": "select",
                "required": True,
                "options": [],
                "help": "",
            }
        ],
        "success_message": "OK",
        "notify_email": "",
        "starts_at": "",
        "ends_at": "",
        "prize": "",
    }
    resp = staff_client.post(url, data=json.dumps(body), content_type="application/json")
    assert resp.status_code == 422


# ===== Submission list / action =====


def test_submissions_list_empty(staff_client, channel, db):
    s = _series(channel)
    af = _form(s)
    url = BASE.format(slug=channel.slug, series_id=s.id) + f"/{af.id}/submissions"
    assert staff_client.get(url).json() == []


def test_submissions_soft_delete_and_restore(staff_client, channel, db):
    from scheduling.models import AudienceSubmission

    s = _series(channel)
    af = _form(s)
    sub = AudienceSubmission.objects.create(
        form=af, payload={"radio_name": "DJ", "message": "hi"}, submitter_name="DJ"
    )
    base = BASE.format(slug=channel.slug, series_id=s.id) + f"/{af.id}"

    # delete
    resp = staff_client.post(f"{base}/submissions/{sub.id}?action=delete")
    assert resp.status_code == 200
    sub.refresh_from_db()
    assert sub.deleted_at is not None

    # restore
    resp = staff_client.post(f"{base}/submissions/{sub.id}?action=restore")
    assert resp.status_code == 200
    sub.refresh_from_db()
    assert sub.deleted_at is None


def test_submissions_status_transition(staff_client, channel, db):
    from scheduling.models import AudienceSubmission, AudienceSubmissionStatus

    s = _series(channel)
    af = _form(s)
    sub = AudienceSubmission.objects.create(
        form=af, payload={"radio_name": "DJ", "message": "hi"}, submitter_name="DJ"
    )
    base = BASE.format(slug=channel.slug, series_id=s.id) + f"/{af.id}"

    staff_client.post(f"{base}/submissions/{sub.id}?action=read")
    sub.refresh_from_db()
    assert sub.status == AudienceSubmissionStatus.READ

    staff_client.post(f"{base}/submissions/{sub.id}?action=handled")
    sub.refresh_from_db()
    assert sub.status == AudienceSubmissionStatus.HANDLED


def test_submissions_csv(staff_client, channel, db):
    from scheduling.models import AudienceSubmission

    s = _series(channel)
    af = _form(s)
    AudienceSubmission.objects.create(
        form=af,
        payload={"radio_name": "リスナー太郎", "message": "テストメッセージ"},
        submitter_name="リスナー太郎",
    )
    url = BASE.format(slug=channel.slug, series_id=s.id) + f"/{af.id}/submissions.csv"
    resp = staff_client.get(url)
    assert resp.status_code == 200
    content = b"".join(resp.streaming_content).decode("utf-8-sig")
    assert "リスナー太郎" in content
    assert "テストメッセージ" in content
