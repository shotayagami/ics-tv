# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P6b staff-owned multipart upload API and storage-boundary tests."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta

import pytest
from django.contrib.auth.models import Permission
from django.utils import timezone

from core import r2
from medialib import upload_services
from medialib.models import AssetUploadStatus

START = "/api/v1/admin/media-uploads/start"
PAYLOAD = {
    "kind": "program",
    "title": "upload fixture",
    "original_filename": "episode.mp4",
    "content_type": "video/mp4",
    "expected_size_bytes": 3,
    "expected_sha256": hashlib.sha256(b"abc").hexdigest(),
}


def _grant(user):
    user.user_permissions.add(
        Permission.objects.get(codename="add_asset", content_type__app_label="medialib")
    )


def _post(client, url, payload=None, *, idem=None):
    headers = {"HTTP_IDEMPOTENCY_KEY": idem} if idem else {}
    return client.post(
        url,
        data=json.dumps(payload or {}),
        content_type="application/json",
        **headers,
    )


def _stub_start(monkeypatch):
    monkeypatch.setattr(
        upload_services.r2, "create_multipart", lambda key, content_type: "upload-1"
    )
    monkeypatch.setattr(
        upload_services.r2,
        "presign_part",
        lambda key, upload_id, number, expires=3600: f"https://signed.invalid/{number}",
    )


def test_start_openapi_declares_required_idempotency_header():
    from api.management.commands.dump_openapi import _merged_schema

    operation = _merged_schema()["paths"][START]["post"]
    header = next(
        parameter
        for parameter in operation["parameters"]
        if parameter["in"] == "header" and parameter["name"] == "Idempotency-Key"
    )
    assert header["required"] is True
    assert header["schema"]["maxLength"] == 200


def test_start_requires_explicit_asset_permission(staff_client, monkeypatch, db):
    _stub_start(monkeypatch)
    response = _post(staff_client, START, PAYLOAD, idem="request-1")
    assert response.status_code == 403


def test_start_is_owner_scoped_idempotent_and_server_keys_filename(
    staff_client, staff_user, monkeypatch, db
):
    _grant(staff_user)
    _stub_start(monkeypatch)
    payload = {**PAYLOAD, "original_filename": "../../episode.mp4"}

    first = _post(staff_client, START, payload, idem="request-1")
    assert first.status_code == 200
    body = first.json()
    assert body["status"] == "uploading"
    assert body["parts"] == [{"part_number": 1, "url": "https://signed.invalid/1"}]

    upload = staff_user.asset_uploads.get()
    assert upload.original_filename == "episode.mp4"
    assert upload.staging_key == f"upload-staging/v1/{upload.id}/source"
    assert "episode.mp4" not in upload.staging_key
    assert (
        _post(staff_client, START, payload, idem="request-1").json()["upload_uuid"]
        == body["upload_uuid"]
    )

    changed = {**payload, "title": "different"}
    assert _post(staff_client, START, changed, idem="request-1").status_code == 409


def test_other_owner_cannot_observe_upload(
    staff_client,
    staff_user,
    django_user_model,
    monkeypatch,
    db,
):
    _grant(staff_user)
    _stub_start(monkeypatch)
    upload_id = _post(staff_client, START, PAYLOAD, idem="owner-a").json()["upload_uuid"]

    other = django_user_model.objects.create_user("other", password="x", is_staff=True)
    _grant(other)
    staff_client.force_login(other)
    assert staff_client.get(f"/api/v1/admin/media-uploads/{upload_id}").status_code == 404
    assert _post(staff_client, f"/api/v1/admin/media-uploads/{upload_id}/abort").status_code == 404


def test_complete_validates_parts_and_enqueues_verification(
    staff_client,
    staff_user,
    monkeypatch,
    db,
):
    _grant(staff_user)
    _stub_start(monkeypatch)
    upload_id = _post(staff_client, START, PAYLOAD, idem="complete-1").json()["upload_uuid"]
    monkeypatch.setattr(
        upload_services.r2, "complete_multipart", lambda *args: {"ETag": "multipart"}
    )
    monkeypatch.setattr(
        upload_services.r2,
        "list_multipart_parts",
        lambda *args: [{"PartNumber": 1, "ETag": '"etag-1"', "Size": 3}],
    )

    bad = _post(
        staff_client,
        f"/api/v1/admin/media-uploads/{upload_id}/complete",
        {"parts": [{"part_number": 2, "etag": "bad"}]},
    )
    assert bad.status_code == 422

    monkeypatch.setattr(upload_services.r2, "list_multipart_parts", lambda *args: [])
    mismatch = _post(
        staff_client,
        f"/api/v1/admin/media-uploads/{upload_id}/complete",
        {"parts": [{"part_number": 1, "etag": "etag-1"}]},
    )
    assert mismatch.status_code == 409
    monkeypatch.setattr(
        upload_services.r2,
        "list_multipart_parts",
        lambda *args: [{"PartNumber": 1, "ETag": '"etag-1"', "Size": 3}],
    )

    queued = []
    from medialib import tasks

    monkeypatch.setattr(tasks.verify_asset_upload, "delay", queued.append)
    monkeypatch.setattr(upload_services.transaction, "on_commit", lambda callback: callback())
    response = _post(
        staff_client,
        f"/api/v1/admin/media-uploads/{upload_id}/complete",
        {"parts": [{"part_number": 1, "etag": "etag-1"}]},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "verifying"
    assert queued == [upload_id]

    # Lost/repeated client response cannot complete the provider upload twice, but it repairs a
    # possible commit-to-broker gap by re-enqueueing the idempotent verifier.
    repeated = _post(
        staff_client,
        f"/api/v1/admin/media-uploads/{upload_id}/complete",
        {"parts": [{"part_number": 1, "etag": "etag-1"}]},
    )
    assert repeated.json()["status"] == "verifying"
    assert queued == [upload_id, upload_id]


@pytest.mark.parametrize("provider_size", [4, 2], ids=["larger", "smaller"])
def test_complete_rejects_provider_total_size_mismatch(
    staff_client,
    staff_user,
    monkeypatch,
    db,
    provider_size,
):
    """F: providerが報告するpart合計サイズがexpected_size_bytes(3)と食い違えばcompleteを弾く。

    presign_partはContent-Lengthを署名しないため宣言超過/不足のpartをclientが書けてしまう
    ことへの対策(所有者決定F)。全体downloadより前、complete_multipart呼び出しより前に検出する。
    """
    _grant(staff_user)
    _stub_start(monkeypatch)
    upload_id = _post(staff_client, START, PAYLOAD, idem=f"size-mismatch-{provider_size}").json()[
        "upload_uuid"
    ]
    monkeypatch.setattr(
        upload_services.r2,
        "list_multipart_parts",
        lambda *args: [{"PartNumber": 1, "ETag": '"etag-1"', "Size": provider_size}],
    )
    called = []
    monkeypatch.setattr(
        upload_services.r2,
        "complete_multipart",
        lambda *args: called.append(args) or {"ETag": "should-not-be-used"},
    )

    response = _post(
        staff_client,
        f"/api/v1/admin/media-uploads/{upload_id}/complete",
        {"parts": [{"part_number": 1, "etag": "etag-1"}]},
    )

    assert response.status_code == 409
    assert called == []  # サイズ不一致はcomplete_multipart呼び出し自体を止める

    upload = staff_user.asset_uploads.get(pk=upload_id)
    assert upload.status == AssetUploadStatus.UPLOADING  # atomicがロールバックしVERIFYINGへ進まない


def test_complete_retry_path_also_rejects_provider_total_size_mismatch(
    staff_client,
    staff_user,
    monkeypatch,
    db,
):
    """F: 再試行経路(_retry_complete_multipart)でも同じサイズ照合を通す。"""
    _grant(staff_user)
    _stub_start(monkeypatch)
    upload_id = _post(staff_client, START, PAYLOAD, idem="size-mismatch-retry").json()[
        "upload_uuid"
    ]
    monkeypatch.setattr(
        upload_services.r2,
        "list_multipart_parts",
        lambda *args: [{"PartNumber": 1, "ETag": '"etag-1"', "Size": 3}],
    )
    monkeypatch.setattr(
        upload_services.r2,
        "complete_multipart",
        lambda *args: (_ for _ in ()).throw(RuntimeError("connection reset")),
    )
    body = {"parts": [{"part_number": 1, "etag": "etag-1"}]}

    # 1回目: complete_multipart自体が(Bの経路で)失敗し、VERIFYING/object_etag_or_version=Noneに残る。
    first = _post(staff_client, f"/api/v1/admin/media-uploads/{upload_id}/complete", body)
    assert first.status_code == 503

    # providerのpart一覧が宣言と食い違うようになっている状態で再試行させる。
    monkeypatch.setattr(
        upload_services.r2,
        "list_multipart_parts",
        lambda *args: [{"PartNumber": 1, "ETag": '"etag-1"', "Size": 999}],
    )
    second = _post(staff_client, f"/api/v1/admin/media-uploads/{upload_id}/complete", body)
    assert second.status_code == 409

    upload = staff_user.asset_uploads.get(pk=upload_id)
    assert upload.status == AssetUploadStatus.VERIFYING  # 既にVERIFYING、UPLOADINGへは戻せない
    assert upload.object_etag_or_version is None


def test_verify_asset_upload_routes_to_dedicated_queue():
    """G: verify_asset_uploadはnormalizeと直列競合しない専用queueへ投入される。"""
    from django.conf import settings

    from medialib import tasks

    assert tasks.verify_asset_upload.queue == "verify"
    assert settings.CELERY_TASK_ROUTES["medialib.tasks.verify_asset_upload"] == {"queue": "verify"}


def test_idempotent_presign_failure_keeps_existing_session_uploading(
    staff_user,
    monkeypatch,
    db,
):
    _grant(staff_user)
    _stub_start(monkeypatch)
    upload, _ = upload_services.start_upload(
        owner=staff_user, idempotency_key="retry-sign-1", **PAYLOAD
    )
    monkeypatch.setattr(
        upload_services.r2,
        "presign_part",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("temporary")),
    )

    with pytest.raises(upload_services.UploadStorageUnavailableError):
        upload_services.start_upload(owner=staff_user, idempotency_key="retry-sign-1", **PAYLOAD)
    upload.refresh_from_db()
    assert upload.status == AssetUploadStatus.UPLOADING
    assert upload.cleanup_after is None


def test_retry_presigned_url_does_not_outlive_session(staff_user, monkeypatch, db):
    _grant(staff_user)
    _stub_start(monkeypatch)
    upload, _ = upload_services.start_upload(
        owner=staff_user, idempotency_key="retry-expiry-1", **PAYLOAD
    )
    upload.expires_at = timezone.now() + timedelta(seconds=30)
    upload.save(update_fields=["expires_at"])
    observed = []
    monkeypatch.setattr(
        upload_services.r2,
        "presign_part",
        lambda key, upload_id, number, expires: observed.append(expires) or "url",
    )

    upload_services.start_upload(owner=staff_user, idempotency_key="retry-expiry-1", **PAYLOAD)
    assert observed and 0 < observed[0] <= 30


def _multipart(*part_numbers: int) -> list[dict]:
    return [
        {"PartNumber": n, "ETag": f'"part-{n}"', "Size": upload_services.PART_SIZE_BYTES}
        for n in part_numbers
    ]


def test_start_extends_expiry_when_provider_reports_new_progress(staff_user, monkeypatch, db):
    """D016: 再署名(start_uploadの再呼び出し)時にproviderへ新しいpartが届いていれば期限を延ばす。"""
    _grant(staff_user)
    _stub_start(monkeypatch)
    big = {**PAYLOAD, "expected_size_bytes": upload_services.PART_SIZE_BYTES * 2 + 1}
    upload, _ = upload_services.start_upload(owner=staff_user, idempotency_key="extend-1", **big)
    shrunk = timezone.now() + timedelta(minutes=5)
    upload.expires_at = shrunk
    upload.save(update_fields=["expires_at"])
    monkeypatch.setattr(upload_services.r2, "list_multipart_parts", lambda *a: _multipart(1))

    upload_services.start_upload(owner=staff_user, idempotency_key="extend-1", **big)

    upload.refresh_from_db()
    assert upload.expires_at > shrunk + timedelta(minutes=30)  # ほぼUPLOAD_TTL分延びた
    assert upload.last_progress_part_count == 1


def test_start_does_not_extend_expiry_without_new_progress(staff_user, monkeypatch, db):
    """D016: providerの完了パート数が前回チェックから増えていなければ延長しない。

    放棄された(送信が止まった)sessionが、無進捗のまま再署名だけを繰り返して延命し続ける経路を
    作らないための確認。
    """
    _grant(staff_user)
    _stub_start(monkeypatch)
    big = {**PAYLOAD, "expected_size_bytes": upload_services.PART_SIZE_BYTES * 2 + 1}
    upload, _ = upload_services.start_upload(owner=staff_user, idempotency_key="no-extend-1", **big)
    monkeypatch.setattr(upload_services.r2, "list_multipart_parts", lambda *a: _multipart(1))

    upload_services.start_upload(owner=staff_user, idempotency_key="no-extend-1", **big)
    upload.refresh_from_db()
    first_extension = upload.expires_at
    assert upload.last_progress_part_count == 1

    # 2回目: providerの完了パート数は変わらない(同じ1パートのまま) → 延長しない
    upload_services.start_upload(owner=staff_user, idempotency_key="no-extend-1", **big)
    upload.refresh_from_db()
    assert upload.expires_at == first_extension
    assert upload.last_progress_part_count == 1


def test_start_extension_is_capped_by_max_session_lifetime(staff_user, monkeypatch, db):
    """D016: 進捗があっても通算の上限(MAX_UPLOAD_SESSION_LIFETIME)は超えて延びない。"""
    _grant(staff_user)
    _stub_start(monkeypatch)
    big = {**PAYLOAD, "expected_size_bytes": upload_services.PART_SIZE_BYTES * 2 + 1}
    upload, _ = upload_services.start_upload(owner=staff_user, idempotency_key="cap-1", **big)
    now = timezone.now()
    # 通算上限まで残りわずかな状態を再現する (created_atを過去へずらす)。
    upload.created_at = now - upload_services.MAX_UPLOAD_SESSION_LIFETIME + timedelta(minutes=5)
    upload.expires_at = now + timedelta(minutes=1)
    upload.save(update_fields=["created_at", "expires_at"])
    deadline = upload.created_at + upload_services.MAX_UPLOAD_SESSION_LIFETIME
    monkeypatch.setattr(upload_services.r2, "list_multipart_parts", lambda *a: _multipart(1))

    upload_services.start_upload(owner=staff_user, idempotency_key="cap-1", **big)
    upload.refresh_from_db()
    assert upload.expires_at <= deadline
    assert upload.expires_at > now + timedelta(minutes=1)  # 進捗により延びてはいる
    at_cap = upload.expires_at

    # さらに進捗があっても通算上限を超えて延びない
    monkeypatch.setattr(upload_services.r2, "list_multipart_parts", lambda *a: _multipart(1, 2))
    upload_services.start_upload(owner=staff_user, idempotency_key="cap-1", **big)
    upload.refresh_from_db()
    assert upload.expires_at == at_cap


def test_default_max_bytes_matches_structural_part_limit():
    """D017: 名目上限(DEFAULT_MAX_BYTES)が実質上限(MAX_PARTS×PART_SIZE_BYTES)と一致することを
    表明する。将来どちらか一方だけを変えて食い違わせた場合にここで検出する。
    """
    assert upload_services.MAX_PARTS == 10_000
    assert upload_services.PART_SIZE_BYTES == 16 * 1024 * 1024
    assert (
        upload_services.DEFAULT_MAX_BYTES
        == upload_services.MAX_PARTS * upload_services.PART_SIZE_BYTES
        == 167_772_160_000  # 156.25 GiB
    )


def test_verification_creates_asset_and_cleanup_preserves_canonical(
    staff_user,
    monkeypatch,
    db,
):
    _grant(staff_user)
    _stub_start(monkeypatch)
    upload, _ = upload_services.start_upload(
        owner=staff_user, idempotency_key="verify-1", **PAYLOAD
    )
    upload.status = AssetUploadStatus.VERIFYING
    upload.verify_started_at = upload.created_at
    upload.save(update_fields=["status", "verify_started_at"])
    monkeypatch.setattr(
        upload_services,
        "_download_and_probe",
        lambda current: (3, PAYLOAD["expected_sha256"], {"streams": [{"codec_type": "video"}]}),
    )
    copies = []
    monkeypatch.setattr(
        upload_services.r2, "copy_object", lambda src, dst, content_type: copies.append((src, dst))
    )

    completed = upload_services.verify_upload(upload.id)
    assert completed.status == AssetUploadStatus.COMPLETED
    assert completed.asset.source_path == f"r2://{completed.canonical_key}"
    assert copies == [(completed.staging_key, completed.canonical_key)]

    deleted = []
    monkeypatch.setattr(upload_services.r2, "delete_object", deleted.append)
    assert upload_services.cleanup_upload(completed.id) is True
    assert deleted == [completed.staging_key]


def test_cleanup_compensates_only_unlinked_promotion(staff_user, monkeypatch, db):
    _grant(staff_user)
    _stub_start(monkeypatch)
    upload, _ = upload_services.start_upload(
        owner=staff_user, idempotency_key="orphan-1", **PAYLOAD
    )
    upload.status = AssetUploadStatus.FAILED
    upload.verify_started_at = upload.created_at
    upload.cleanup_after = upload.created_at
    upload.save(update_fields=["status", "verify_started_at", "cleanup_after"])

    monkeypatch.setattr(upload_services.r2, "abort_multipart", lambda *args: None)
    deleted = []
    monkeypatch.setattr(upload_services.r2, "delete_object", deleted.append)
    assert upload_services.cleanup_upload(upload.id) is True
    assert deleted == [upload.staging_key, upload.canonical_key]


def test_verification_mismatch_never_promotes(staff_user, monkeypatch, db):
    _grant(staff_user)
    _stub_start(monkeypatch)
    upload, _ = upload_services.start_upload(
        owner=staff_user, idempotency_key="mismatch-1", **PAYLOAD
    )
    upload.status = AssetUploadStatus.VERIFYING
    upload.verify_started_at = timezone.now()
    upload.save(update_fields=["status", "verify_started_at"])
    monkeypatch.setattr(
        upload_services,
        "_download_and_probe",
        lambda current: (4, hashlib.sha256(b"nope").hexdigest(), {}),
    )
    monkeypatch.setattr(
        upload_services.r2,
        "copy_object",
        lambda *args: pytest.fail("mismatched bytes must not be promoted"),
    )

    with pytest.raises(upload_services.UploadVerificationError):
        upload_services.verify_upload(upload.id)
    upload_services.mark_verification_failed(upload.id)
    upload.refresh_from_db()
    assert upload.status == AssetUploadStatus.FAILED
    assert upload.asset_id is None


def test_cleanup_due_expires_active_session(staff_user, monkeypatch, db):
    _grant(staff_user)
    _stub_start(monkeypatch)
    upload, _ = upload_services.start_upload(
        owner=staff_user, idempotency_key="expire-1", **PAYLOAD
    )
    upload.expires_at = timezone.now() - upload_services.UPLOAD_TTL
    upload.save(update_fields=["expires_at"])
    monkeypatch.setattr(upload_services.r2, "abort_multipart", lambda *args: None)
    deleted = []
    monkeypatch.setattr(upload_services.r2, "delete_object", deleted.append)

    assert upload_services.cleanup_due_uploads() == 1
    upload.refresh_from_db()
    assert upload.status == AssetUploadStatus.EXPIRED
    assert upload.object_deleted_at is not None
    assert deleted == [upload.staging_key]


def test_cleanup_retries_when_multipart_abort_fails(staff_user, monkeypatch, db):
    _grant(staff_user)
    _stub_start(monkeypatch)
    upload, _ = upload_services.start_upload(
        owner=staff_user, idempotency_key="abort-retry-1", **PAYLOAD
    )
    upload.status = AssetUploadStatus.ABORTED
    upload.cleanup_after = timezone.now()
    upload.save(update_fields=["status", "cleanup_after"])
    monkeypatch.setattr(
        upload_services.r2,
        "abort_multipart",
        lambda *args: (_ for _ in ()).throw(RuntimeError("temporary")),
    )
    monkeypatch.setattr(
        upload_services.r2,
        "delete_object",
        lambda *args: pytest.fail("object deletion must wait until multipart abort succeeds"),
    )

    assert upload_services.cleanup_upload(upload.id) is False
    upload.refresh_from_db()
    assert upload.object_deleted_at is None
    assert upload.cleanup_attempts == 1
    assert upload.cleanup_after > timezone.now()


def test_start_rejects_too_many_parts_before_creating_multipart_or_row(staff_user, monkeypatch, db):
    """P6-fixes A: パート数上限はDB行insert/create_multipartより前に弾かれ、孤児を残さない。"""
    _grant(staff_user)
    created = []
    monkeypatch.setattr(
        upload_services.r2,
        "create_multipart",
        lambda key, content_type: created.append(key) or "should-not-be-created",
    )
    too_big = {
        **PAYLOAD,
        "expected_size_bytes": upload_services.MAX_PARTS * upload_services.PART_SIZE_BYTES + 1,
    }

    with pytest.raises(upload_services.UploadValidationError):
        upload_services.start_upload(
            owner=staff_user, idempotency_key="too-many-parts-1", **too_big
        )

    assert created == []
    assert not staff_user.asset_uploads.exists()


def test_complete_multipart_transient_failure_recovers_on_retry(
    staff_client,
    staff_user,
    monkeypatch,
    db,
):
    """P6-fixes B: complete_multipart の一時失敗は握りつぶさず、再complete要求で復旧する。"""
    _grant(staff_user)
    _stub_start(monkeypatch)
    upload_id = _post(staff_client, START, PAYLOAD, idem="complete-retry-1").json()["upload_uuid"]
    monkeypatch.setattr(
        upload_services.r2,
        "list_multipart_parts",
        lambda *args: [{"PartNumber": 1, "ETag": '"etag-1"', "Size": 3}],
    )
    calls = {"n": 0}

    def flaky_complete(*args):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("connection reset")
        return {"ETag": "multipart-final"}

    monkeypatch.setattr(upload_services.r2, "complete_multipart", flaky_complete)
    queued = []
    from medialib import tasks

    monkeypatch.setattr(tasks.verify_asset_upload, "delay", queued.append)
    monkeypatch.setattr(upload_services.transaction, "on_commit", lambda callback: callback())

    body = {"parts": [{"part_number": 1, "etag": "etag-1"}]}
    first = _post(staff_client, f"/api/v1/admin/media-uploads/{upload_id}/complete", body)
    assert first.status_code == 503
    assert queued == []  # provider側が未確定のうちは検証taskを投入しない

    upload = staff_user.asset_uploads.get(pk=upload_id)
    assert upload.status == AssetUploadStatus.VERIFYING
    assert upload.object_etag_or_version is None

    second = _post(staff_client, f"/api/v1/admin/media-uploads/{upload_id}/complete", body)
    assert second.status_code == 200
    assert second.json()["status"] == "verifying"
    assert queued == [upload_id]
    assert calls["n"] == 2

    upload.refresh_from_db()
    assert upload.object_etag_or_version == "multipart-final"


def test_cleanup_reclaims_stalled_verifying_session(staff_user, monkeypatch, db):
    """P6-fixes C: broker投入が失われverifyingへ滞留したsessionをcleanup beatが回収する。"""
    _grant(staff_user)
    _stub_start(monkeypatch)
    stalled, _ = upload_services.start_upload(
        owner=staff_user, idempotency_key="stalled-1", **PAYLOAD
    )
    stalled.status = AssetUploadStatus.VERIFYING
    stalled.verify_started_at = (
        timezone.now() - upload_services.VERIFY_STALL_TIMEOUT - timedelta(seconds=1)
    )
    stalled.save(update_fields=["status", "verify_started_at"])

    fresh, _ = upload_services.start_upload(
        owner=staff_user, idempotency_key="fresh-verifying-1", **PAYLOAD
    )
    fresh.status = AssetUploadStatus.VERIFYING
    fresh.verify_started_at = timezone.now()
    fresh.save(update_fields=["status", "verify_started_at"])

    # フェーズ1: reclaim単体を観測する (cleanup_uploadを無効化し、failed化とlast_error_codeを確認)。
    real_cleanup_upload = upload_services.cleanup_upload
    monkeypatch.setattr(upload_services, "cleanup_upload", lambda upload_id: False)
    upload_services.cleanup_due_uploads()

    stalled.refresh_from_db()
    assert stalled.status == AssetUploadStatus.FAILED
    assert stalled.last_error_code == "verify_stalled"
    fresh.refresh_from_db()
    assert fresh.status == AssetUploadStatus.VERIFYING  # 閾値未満のsessionは無傷

    # フェーズ2: 通常のcleanup_uploadに戻すと、reclaim済みのsessionが実際に回収される
    # (multipart abort + staging/canonical削除。この経路が無いとorphanが永久に残る)。
    monkeypatch.setattr(upload_services, "cleanup_upload", real_cleanup_upload)
    aborted = []
    monkeypatch.setattr(
        upload_services.r2, "abort_multipart", lambda key, mpid: aborted.append((key, mpid))
    )
    deleted = []
    monkeypatch.setattr(upload_services.r2, "delete_object", deleted.append)

    assert upload_services.cleanup_due_uploads() == 1

    stalled.refresh_from_db()
    assert stalled.object_deleted_at is not None
    # 回収完了でlast_error_codeはcleanup_uploadの成功パスによりクリアされる
    # (test_verification_creates_asset_and_cleanup_preserves_canonical と同じ扱い)。
    assert stalled.last_error_code is None
    assert aborted == [(stalled.staging_key, stalled.multipart_upload_id)]
    # verify_started_at済み・asset未リンクなのでstaging/canonical双方を回収する
    # (test_cleanup_compensates_only_unlinked_promotion と同じ経路)。
    assert deleted == [stalled.staging_key, stalled.canonical_key]


def test_verify_heartbeat_protects_active_attempt_from_stall_reclaim(staff_user, monkeypatch, db):
    """P6-fixes C追補: verify_upload()開始時のheartbeatで、実行中の試行はstall回収されない。

    一度も走っていない(broker投入喪失)sessionと、実際に試行が始まった(が完了前に失敗した)
    sessionを、どちらもverify_started_atが同じだけ古い状態から出発させて区別する。heartbeatが
    無いと後者もcleanup_due_uploads()に回収され、進行中のcopy_object()がstaging/canonical削除の
    後に着地してA同様の孤児を生みうる (要修正指摘)。
    """
    _grant(staff_user)
    _stub_start(monkeypatch)
    stale_start = timezone.now() - upload_services.VERIFY_STALL_TIMEOUT - timedelta(seconds=1)

    active, _ = upload_services.start_upload(
        owner=staff_user, idempotency_key="active-verify-1", **PAYLOAD
    )
    active.status = AssetUploadStatus.VERIFYING
    active.verify_started_at = stale_start
    active.save(update_fields=["status", "verify_started_at"])

    never_started, _ = upload_services.start_upload(
        owner=staff_user, idempotency_key="never-started-1", **PAYLOAD
    )
    never_started.status = AssetUploadStatus.VERIFYING
    never_started.verify_started_at = stale_start
    never_started.save(update_fields=["status", "verify_started_at"])

    # activeだけ実際に試行が始まったことにする: heartbeatは効かせつつ、重い処理は失敗させて
    # 完了させない (heartbeat単体の効果を確認したいので、実際の検証成功までは進めない)。
    monkeypatch.setattr(
        upload_services,
        "_download_and_probe",
        lambda upload: (_ for _ in ()).throw(RuntimeError("still downloading")),
    )
    with pytest.raises(RuntimeError):
        upload_services.verify_upload(active.id)

    active.refresh_from_db()
    assert active.verify_started_at > stale_start  # heartbeatで時計が進んだ
    never_started.refresh_from_db()
    assert never_started.verify_started_at == stale_start  # 一度も走っていないので不変

    # never_startedはこの呼び出し内でfailed化 → 即座にdue_idsへも合流し回収まで進むので、
    # 実R2呼び出しに触れないようabort/delete先をstubしておく。
    monkeypatch.setattr(upload_services.r2, "abort_multipart", lambda *args: None)
    monkeypatch.setattr(upload_services.r2, "delete_object", lambda *args: None)
    assert upload_services.cleanup_due_uploads() == 1  # 回収されるのはnever_startedの1件だけ

    active.refresh_from_db()
    assert active.status == AssetUploadStatus.VERIFYING  # 試行中なので回収されない
    never_started.refresh_from_db()
    assert never_started.status == AssetUploadStatus.FAILED  # 一度も走っていないので回収される


def test_provider_part_listing_follows_markers(monkeypatch):
    class FakeClient:
        def __init__(self):
            self.calls = []

        def list_parts(self, **kwargs):
            self.calls.append(kwargs)
            if "PartNumberMarker" not in kwargs:
                return {
                    "Parts": [{"PartNumber": 1, "ETag": '"one"', "Size": 16 * 1024 * 1024}],
                    "IsTruncated": True,
                    "NextPartNumberMarker": 1,
                }
            return {
                "Parts": [{"PartNumber": 2, "ETag": '"two"', "Size": 5}],
                "IsTruncated": False,
            }

    client = FakeClient()
    monkeypatch.setattr(r2, "client", lambda: client)
    monkeypatch.setattr(r2, "bucket", lambda: "fixture")
    assert r2.list_multipart_parts("key", "upload") == [
        {"PartNumber": 1, "ETag": '"one"', "Size": 16 * 1024 * 1024},
        {"PartNumber": 2, "ETag": '"two"', "Size": 5},
    ]
    assert client.calls[1]["PartNumberMarker"] == 1
