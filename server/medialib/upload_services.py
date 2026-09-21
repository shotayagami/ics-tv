# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Staff-owned multipart upload sessions for media sources."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
from datetime import timedelta
from pathlib import Path, PurePath

from django.db import IntegrityError, transaction
from django.utils import timezone

from core import r2
from medialib import mezz
from medialib.models import Asset, AssetKind, AssetUpload, AssetUploadStatus, NormalizeStatus

PART_SIZE_BYTES = 16 * 1024 * 1024
MAX_PARTS = 10_000
# D017: 名目上限を実質上限へ合わせる。旧値は500GiB固定でMAX_PARTS×PART_SIZE_BYTES(156.25GiB)と
# 食い違っていた (名目の方が緩い状態)。数十GB級の実素材に対して実質上限はなお数倍の余裕があるため、
# パートサイズは上げず、名目の方を実質へ一致させる (変更は定数の定義だけ)。
DEFAULT_MAX_BYTES = MAX_PARTS * PART_SIZE_BYTES  # 167,772,160,000 bytes = 156.25 GiB
# D016: 初回発行および再署名(start_upload再呼び出し)ごとに実際の進捗があれば与え直す延長幅。
# 個々の署名URL自体の上限(_part_urlsのmin(3600, remaining))はこれと独立に短いまま保つ
# (漏えい時の暴露を最小にするのが方式選択の理由なので、ここは変えない)。
UPLOAD_TTL = timedelta(hours=1)
# D016: 延長の通算上限 (created_atからの絶対時刻)。無進捗のまま延長を繰り返す経路が無いことに加え、
# 進捗があっても際限なく延ばさないための天井が要る。根拠: 数十GB級素材を家庭/オフィスの細い回線
# (下り優先の回線でも上りの目安として10Mbps程度は珍しくない) でも完走できる幅を確保する。
# 60GB @ 10Mbps ≈ 13.7時間。中断/再開を挟む余地を見て24時間とした。
MAX_UPLOAD_SESSION_LIFETIME = timedelta(
    seconds=int(os.environ.get("MEDIA_UPLOAD_SESSION_MAX_SECONDS", str(24 * 3600)))
)
# verifyingで滞留したsessionを回収するまでの猶予 (最終防衛線)。verify_asset_uploadの通常の
# retry(60s×3)より十分長く取り、broker投入自体が失われた場合だけを拾う。大容量ファイルの
# 実download+hash+probeは短くない見込みだが、上限は経験則の固定値であり、実際の最大ファイル
# サイズ/回線速度に応じてenvで調整する前提 (ponytail: 固定閾値の天井。進捗heartbeatに基づく
# 判定へ上げるのはP6d以降の実測を待つ)。
VERIFY_STALL_TIMEOUT = timedelta(
    seconds=int(os.environ.get("MEDIA_UPLOAD_VERIFY_STALL_SECONDS", "3600"))
)
ALLOWED_CONTENT_TYPES = {
    "video/mp4",
    "video/quicktime",
    "video/webm",
    "video/x-matroska",
}
SHA256_RE = re.compile(r"[0-9a-f]{64}")


class UploadError(Exception):
    code = "upload_error"


class UploadValidationError(UploadError):
    code = "invalid_upload"


class UploadConflictError(UploadError):
    code = "upload_conflict"


class UploadStorageUnavailableError(UploadError):
    code = "storage_unavailable"


class UploadVerificationError(UploadError):
    code = "verification_failed"


def _clean_filename(value: str) -> str:
    name = PurePath(value.replace("\\", "/")).name.strip()
    if not name or len(name) > 255 or any(ord(char) < 32 for char in name):
        raise UploadValidationError("invalid original filename")
    return name


def _validated_input(
    *,
    kind: str,
    title: str,
    original_filename: str,
    content_type: str,
    expected_size_bytes: int,
    expected_sha256: str,
) -> dict:
    if kind not in AssetKind.values:
        raise UploadValidationError("invalid asset kind")
    title = title.strip()
    if not title or len(title) > 300:
        raise UploadValidationError("invalid title")
    content_type = content_type.strip().lower()
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise UploadValidationError("unsupported content type")
    max_bytes = int(os.environ.get("MEDIA_UPLOAD_MAX_BYTES", str(DEFAULT_MAX_BYTES)))
    if expected_size_bytes < 1 or expected_size_bytes > max_bytes:
        raise UploadValidationError("invalid upload size")
    expected_sha256 = expected_sha256.strip().lower()
    if not SHA256_RE.fullmatch(expected_sha256):
        raise UploadValidationError("invalid SHA-256")
    return {
        "kind": kind,
        "title": title,
        "original_filename": _clean_filename(original_filename),
        "content_type": content_type,
        "expected_size_bytes": expected_size_bytes,
        "expected_sha256": expected_sha256,
    }


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _fingerprint(values: dict) -> str:
    raw = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _digest(raw)


def _part_count(size: int) -> int:
    count = (size + PART_SIZE_BYTES - 1) // PART_SIZE_BYTES
    if count > MAX_PARTS:
        raise UploadValidationError("upload requires too many parts")
    return count


def _keys(upload_id) -> tuple[str, str]:
    canonical_uuid = str(upload_id)
    return (
        f"upload-staging/v1/{canonical_uuid}/source",
        f"sources/v1/{canonical_uuid}/source",
    )


def _part_urls(upload: AssetUpload) -> list[dict]:
    if not upload.multipart_upload_id:
        return []
    remaining = int((upload.expires_at - timezone.now()).total_seconds())
    if remaining <= 0:
        raise UploadConflictError("upload session has expired")
    expires = min(3600, remaining)
    return [
        {
            "part_number": number,
            "url": r2.presign_part(
                upload.staging_key,
                upload.multipart_upload_id,
                number,
                expires=expires,
            ),
        }
        for number in range(1, _part_count(upload.expected_size_bytes) + 1)
    ]


def _maybe_extend_ttl(upload: AssetUpload) -> None:
    """D016: 再署名 (start_uploadの同一idempotency-keyでの再呼び出し) のたびに、providerへ
    実際に新しいpartが届いていれば期限を延ばす。届いていなければ延ばさない。

    進捗の判定はclient申告(etag一覧)を信じず、_provider_total_size等と同じくprovider
    (list_multipart_parts) を権威とする。単に「前回同じpartを再送しただけ」ではpart数は
    増えないため、無進捗のまま延長だけを繰り返す経路にはならない。
    延長は付加価値でありprovider一時障害でも再署名自体 (_part_urls) は継続させたいため、
    list_multipart_parts失敗時は例外を外へ出さず単に延長をあきらめる。
    """
    now = timezone.now()
    if now >= upload.expires_at:
        return  # 既に期限切れ: _part_urls() 側の通常のUploadConflictErrorに委ねる
    deadline = upload.created_at + MAX_UPLOAD_SESSION_LIFETIME
    if upload.expires_at >= deadline:
        return  # 既に通算上限に到達済み
    provider_parts = None
    with contextlib.suppress(Exception):
        assert upload.multipart_upload_id is not None  # UPLOADING への遷移時に設定済み
        provider_parts = r2.list_multipart_parts(upload.staging_key, upload.multipart_upload_id)
    if provider_parts is None:
        return
    completed = len(provider_parts)
    if completed <= upload.last_progress_part_count:
        return  # 前回チェック以降の新規完了パートが無い = 無進捗
    new_expiry = min(now + UPLOAD_TTL, deadline)
    if new_expiry <= upload.expires_at:
        return
    upload.expires_at = new_expiry
    upload.last_progress_part_count = completed
    upload.save(update_fields=["expires_at", "last_progress_part_count", "updated_at"])


def start_upload(*, owner, idempotency_key: str, **payload) -> tuple[AssetUpload, list[dict]]:
    if not idempotency_key or len(idempotency_key) > 200:
        raise UploadValidationError("Idempotency-Key is required")
    values = _validated_input(**payload)
    # パート数上限 (MAX_PARTS×PART_SIZE_BYTES) は create_multipart / DB行insertより前に確定させる。
    # ここで弾かず _part_urls() 内 (multipart作成後) まで遅らせると、422エラー時にDB行だけ
    # atomicでロールバックされ、providerには孤児のuncompleted multipartが残ってしまう
    # (P6a契約でbucket listingによる回収ができないため永久に見えない)。
    _part_count(values["expected_size_bytes"])
    idem_hash = _digest(idempotency_key)
    fingerprint = _fingerprint(values)
    upload: AssetUpload | None = None
    created_multipart = False
    try:
        with transaction.atomic():
            upload = (
                AssetUpload.objects.select_for_update()
                .filter(owner=owner, idempotency_key_hash=idem_hash)
                .first()
            )
            if upload:
                if upload.request_fingerprint != fingerprint:
                    raise UploadConflictError("Idempotency-Key was used with different input")
                if upload.status == AssetUploadStatus.UPLOADING:
                    _maybe_extend_ttl(upload)  # D016: 再署名は進捗があれば期限延長も兼ねる
                    return upload, _part_urls(upload)
                return upload, []

            upload = AssetUpload(owner=owner, idempotency_key_hash=idem_hash, **values)
            staging_key, canonical_key = _keys(upload.id)
            upload.staging_key = staging_key
            upload.canonical_key = canonical_key
            upload.request_fingerprint = fingerprint
            upload.expires_at = timezone.now() + UPLOAD_TTL
            upload.save(force_insert=True)
            upload.multipart_upload_id = r2.create_multipart(staging_key, upload.content_type)
            created_multipart = True
            urls = _part_urls(upload)
            upload.status = AssetUploadStatus.UPLOADING
            upload.save(update_fields=["multipart_upload_id", "status", "updated_at"])
            return upload, urls
    except (UploadConflictError, UploadValidationError):
        raise
    except IntegrityError:
        existing = AssetUpload.objects.get(owner=owner, idempotency_key_hash=idem_hash)
        if existing.request_fingerprint != fingerprint:
            raise UploadConflictError("Idempotency-Key was used with different input") from None
        return existing, _part_urls(
            existing
        ) if existing.status == AssetUploadStatus.UPLOADING else []
    except Exception as exc:
        if upload and created_multipart and upload.multipart_upload_id:
            with contextlib.suppress(Exception):
                r2.abort_multipart(upload.staging_key, upload.multipart_upload_id)
        raise UploadStorageUnavailableError("could not start storage upload") from exc


def _normalize_parts(parts: list[dict], expected_count: int) -> list[dict]:
    normalized: list[dict] = []
    for item in parts:
        number = item.get("part_number")
        etag = item.get("etag")
        if not isinstance(number, int) or not isinstance(etag, str) or not etag or len(etag) > 256:
            raise UploadValidationError("invalid multipart completion list")
        normalized.append({"PartNumber": number, "ETag": etag})
    normalized.sort(key=lambda item: item["PartNumber"])
    if [item["PartNumber"] for item in normalized] != list(range(1, expected_count + 1)):
        raise UploadValidationError("multipart parts must be consecutive and complete")
    return normalized


def _same_provider_parts(expected: list[dict], observed: list[dict]) -> bool:
    def normalized(rows: list[dict]) -> list[tuple[int, str]]:
        return sorted((int(row["PartNumber"]), str(row["ETag"]).strip('"')) for row in rows)

    return normalized(expected) == normalized(observed)


def _provider_total_size(provider_parts: list[dict]) -> int:
    """provider (r2.list_multipart_parts) が報告するpart Sizeの合計。

    R2は最終part以外のpart size均一を要求するため、全partを一律に超過/不足サイズで書けば
    均一性自体は保たれてしまう。したがってこの照合は「宣言(expected_size_bytes)と実体の不一致を
    全体downloadより前に検出する」ものであり、悪意あるstaffが宣言と一致する合計サイズで
    (中身だけ差し替えて)書くケースは防げない。最終的な権威はverify_upload()のdownload後の
    サイズ比較 (所有者決定F)。
    """
    return sum(int(item["Size"]) for item in provider_parts)


def _retry_complete_multipart(upload: AssetUpload) -> None:
    """繰り返しのcomplete要求で、前回未確定のまま終わったcomplete_multipartを再試行する。

    provider側のpart一覧を都度取り直して使う(client提出値はここでは信頼しない)。
    NoSuchUpload(=providerは既に完了済で応答だけが失われた)は成功とみなす。それ以外の
    失敗はUploadStorageUnavailableErrorとして呼び出し元へ伝え、verify taskを投入させない。
    object_etag_or_versionが未設定のまま残るため、次のcomplete要求(同じsession)が
    ここへ再度到達できる
    (契約: docs/P6_UPLOAD_DESIGN.md「transientなprovider障害はverifyingの同じsessionを再試行」)。
    サイズ照合(所有者決定F)が失敗した場合はUploadConflictErrorのまま呼び出し元(HTTP 409)へ
    伝える。session はVERIFYINGに残り、最終的にはcleanup_due_uploads()のstall回収に委ねる
    (通常経路の弾き方と違い、ここでは既にVERIFYINGへ遷移済みでUPLOADINGへは戻せないため)。
    """
    from botocore.exceptions import ClientError

    try:
        assert upload.multipart_upload_id is not None  # VERIFYING への遷移時に確認済み
        provider_parts = r2.list_multipart_parts(upload.staging_key, upload.multipart_upload_id)
        parts = sorted(provider_parts, key=lambda item: item["PartNumber"])
        # 再試行経路でも通常経路(complete_upload)と同じサイズ照合を通す(所有者決定F)。
        if _provider_total_size(provider_parts) != upload.expected_size_bytes:
            raise UploadConflictError(
                "provider multipart total size does not match the declared size"
            )
        result = r2.complete_multipart(upload.staging_key, upload.multipart_upload_id, parts)
    except UploadConflictError:
        raise
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "NoSuchUpload":
            return
        raise UploadStorageUnavailableError("could not complete multipart upload") from exc
    except Exception as exc:
        raise UploadStorageUnavailableError("could not complete multipart upload") from exc
    etag = result.get("ETag") or result.get("VersionId")
    if etag:
        AssetUpload.objects.filter(pk=upload.pk, status=AssetUploadStatus.VERIFYING).update(
            object_etag_or_version=str(etag)[:512]
        )


def complete_upload(*, owner, upload_id, parts: list[dict]) -> AssetUpload:
    newly_verifying = False
    retry_complete = False
    with transaction.atomic():
        upload = AssetUpload.objects.select_for_update().filter(pk=upload_id, owner=owner).first()
        if upload is None:
            raise AssetUpload.DoesNotExist
        if upload.status == AssetUploadStatus.COMPLETED:
            return upload
        if upload.status == AssetUploadStatus.VERIFYING:
            normalized = []
            # 直前のcomplete要求でcomplete_multipartが失敗/不明のまま終わっていれば
            # (object_etag_or_version未設定)、この要求で再試行する。既に確定済みなら
            # providerを二度と呼ばず検証taskの再投入だけ行う。
            retry_complete = upload.object_etag_or_version is None
        else:
            if upload.status != AssetUploadStatus.UPLOADING or not upload.multipart_upload_id:
                raise UploadConflictError("upload cannot be completed in its current state")
            normalized = _normalize_parts(parts, _part_count(upload.expected_size_bytes))
            try:
                provider_parts = r2.list_multipart_parts(
                    upload.staging_key,
                    upload.multipart_upload_id,
                )
            except Exception as exc:
                raise UploadStorageUnavailableError("could not inspect multipart upload") from exc
            if not _same_provider_parts(normalized, provider_parts):
                raise UploadConflictError(
                    "provider multipart state does not match completion request"
                )
            # 宣言(expected_size_bytes)とproviderの実サイズ合計の照合(所有者決定F)。presign_partは
            # Content-Lengthを署名しないため、宣言超過/不足のpartをclientが書けてしまうことへの対策。
            # 全体downloadより前に検出できるが、最終的な権威はverify_upload()のdownload後比較のまま。
            if _provider_total_size(provider_parts) != upload.expected_size_bytes:
                raise UploadConflictError(
                    "provider multipart total size does not match the declared size"
                )
            upload.status = AssetUploadStatus.VERIFYING
            upload.verify_started_at = timezone.now()
            upload.save(update_fields=["status", "verify_started_at", "updated_at"])
            newly_verifying = True

    if newly_verifying:
        try:
            assert upload.multipart_upload_id is not None  # 上の分岐で確認済み
            result = r2.complete_multipart(
                upload.staging_key, upload.multipart_upload_id, normalized
            )
        except Exception as exc:
            # 失敗をverify taskへ丸投げしない: object_etag_or_versionが未設定のまま残るので、
            # 次のcomplete要求がretry_complete分岐で同じsessionを再試行できる。
            raise UploadStorageUnavailableError("could not complete multipart upload") from exc
        etag = result.get("ETag") or result.get("VersionId")
        if etag:
            AssetUpload.objects.filter(pk=upload.pk, status=AssetUploadStatus.VERIFYING).update(
                object_etag_or_version=str(etag)[:512]
            )
    elif retry_complete:
        _retry_complete_multipart(upload)

    from medialib.tasks import verify_asset_upload

    # A repeated complete request repairs the commit-to-broker gap by re-enqueueing the same
    # idempotent verifier. The verifier locks before Asset creation, so duplicate delivery is safe.
    transaction.on_commit(lambda: verify_asset_upload.delay(str(upload.id)))
    upload.refresh_from_db()
    return upload


def abort_upload(*, owner, upload_id) -> AssetUpload:
    with transaction.atomic():
        upload = AssetUpload.objects.select_for_update().filter(pk=upload_id, owner=owner).first()
        if upload is None:
            raise AssetUpload.DoesNotExist
        if upload.status == AssetUploadStatus.COMPLETED:
            raise UploadConflictError("completed uploads cannot be aborted")
        if upload.status == AssetUploadStatus.VERIFYING:
            raise UploadConflictError("verification is already in progress")
        if upload.status in (AssetUploadStatus.ABORTED, AssetUploadStatus.EXPIRED):
            return upload
        upload.status = AssetUploadStatus.ABORTED
        upload.aborted_at = timezone.now()
        upload.cleanup_after = timezone.now()
        upload.save(update_fields=["status", "aborted_at", "cleanup_after", "updated_at"])

    cleanup_upload(upload.id)
    upload.refresh_from_db()
    return upload


def _download_and_probe(upload: AssetUpload) -> tuple[int, str, dict]:
    fd, name = tempfile.mkstemp(suffix=".src")
    os.close(fd)
    path = Path(name)
    try:
        r2.client().download_file(r2.bucket(), upload.staging_key, str(path))
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
        probe = mezz.ffprobe(path)
        if mezz.stream(probe, "video") is None:
            raise UploadVerificationError("uploaded object has no video stream")
        return size, digest.hexdigest(), probe
    finally:
        path.unlink(missing_ok=True)


def verify_upload(upload_id) -> AssetUpload:
    with transaction.atomic():
        upload = AssetUpload.objects.select_for_update().get(pk=upload_id)
        if upload.status == AssetUploadStatus.COMPLETED:
            return upload
        if upload.status != AssetUploadStatus.VERIFYING:
            raise UploadConflictError("upload is not awaiting verification")
        # heartbeat: 実処理(download+hash+probe)へ入る直前でverify_started_atを進める。
        # cleanup_due_uploads()のstall回収はこの時刻を見るため、試行中(再試行含む)のverifyは
        # 開始のたびに時計が進み回収対象から外れる。一度も走っていない(broker投入喪失)sessionだけが
        # 古い時刻のまま残り回収される。select_for_update同士のロックにより、この更新とreclaim側の
        # skip_locked select_for_updateが同時に同じ行を見て競合することもない。
        # 残る天井: 1回の試行がVERIFY_STALL_TIMEOUTを超えて実行中の場合だけは、途中でstall回収され
        # うる (download+hash+probe中に定期heartbeatを打つ設計への引き上げはP6d以降の実測を待つ)。
        upload.verify_started_at = timezone.now()
        upload.save(update_fields=["verify_started_at", "updated_at"])

    size, sha256, _probe = _download_and_probe(upload)
    if size != upload.expected_size_bytes or sha256 != upload.expected_sha256:
        raise UploadVerificationError("uploaded bytes do not match the declaration")

    assert upload.canonical_key is not None  # start_upload が設定済み
    r2.copy_object(upload.staging_key, upload.canonical_key, upload.content_type)
    with transaction.atomic():
        locked = AssetUpload.objects.select_for_update().get(pk=upload.pk)
        if locked.status == AssetUploadStatus.COMPLETED:
            return locked
        if locked.status != AssetUploadStatus.VERIFYING:
            raise UploadConflictError("upload state changed during verification")
        asset = Asset.objects.create(
            kind=locked.kind,
            title=locked.title,
            source_path=f"r2://{locked.canonical_key}",
            checksum=sha256,
            normalize_status=NormalizeStatus.PENDING,
        )
        now = timezone.now()
        locked.asset = asset
        locked.observed_size_bytes = size
        locked.verified_sha256 = sha256
        locked.status = AssetUploadStatus.COMPLETED
        locked.completed_at = now
        locked.cleanup_after = now
        locked.last_error_code = None
        locked.save(
            update_fields=[
                "asset",
                "observed_size_bytes",
                "verified_sha256",
                "status",
                "completed_at",
                "cleanup_after",
                "last_error_code",
                "updated_at",
            ]
        )
    return locked


def mark_verification_failed(upload_id, code: str = "verification_failed") -> None:
    AssetUpload.objects.filter(pk=upload_id, status=AssetUploadStatus.VERIFYING).update(
        status=AssetUploadStatus.FAILED,
        last_error_code=code[:64],
        cleanup_after=timezone.now(),
    )


def cleanup_upload(upload_id) -> bool:
    upload = AssetUpload.objects.get(pk=upload_id)
    try:
        if upload.multipart_upload_id and upload.status != AssetUploadStatus.COMPLETED:
            r2.abort_multipart(upload.staging_key, upload.multipart_upload_id)
        r2.delete_object(upload.staging_key)
        with transaction.atomic():
            locked = AssetUpload.objects.select_for_update().get(pk=upload.pk)
            if (
                locked.canonical_key
                and locked.asset_id is None
                and locked.status != AssetUploadStatus.COMPLETED
                and locked.verify_started_at is not None
            ):
                r2.delete_object(locked.canonical_key)
            locked.object_deleted_at = timezone.now()
            locked.cleanup_after = None
            locked.last_error_code = None
            locked.save(
                update_fields=[
                    "object_deleted_at",
                    "cleanup_after",
                    "last_error_code",
                    "updated_at",
                ]
            )
        return True
    except Exception:
        attempts = upload.cleanup_attempts + 1
        delay = min(3600, 2 ** min(attempts, 10) * 30)
        AssetUpload.objects.filter(pk=upload.pk).update(
            cleanup_attempts=attempts,
            cleanup_after=timezone.now() + timedelta(seconds=delay),
            last_error_code="cleanup_failed",
        )
        return False


def cleanup_due_uploads(*, limit: int = 100) -> int:
    now = timezone.now()
    with transaction.atomic():
        expired_ids = list(
            AssetUpload.objects.select_for_update(skip_locked=True)
            .filter(
                status__in=[AssetUploadStatus.PENDING, AssetUploadStatus.UPLOADING],
                expires_at__lte=now,
            )
            .values_list("id", flat=True)[:limit]
        )
        AssetUpload.objects.filter(id__in=expired_ids).update(
            status=AssetUploadStatus.EXPIRED,
            cleanup_after=now,
        )

    # broker投入の取りこぼし等でverifyingのまま滞留したsessionの最終防衛線。verify taskが
    # 一度も走らないとDB側には何のシグナルも残らず (abort_uploadは409で拒否、cleanupの対象外)、
    # session行がbucketの未完了multipartごと永久に残ってしまう。failed化すれば以降は通常の
    # 回収対象(下のdue_ids)としてmultipart abort + staging削除まで進む。
    stall_cutoff = now - VERIFY_STALL_TIMEOUT
    with transaction.atomic():
        stalled_ids = list(
            AssetUpload.objects.select_for_update(skip_locked=True)
            .filter(status=AssetUploadStatus.VERIFYING, verify_started_at__lte=stall_cutoff)
            .values_list("id", flat=True)[:limit]
        )
        AssetUpload.objects.filter(id__in=stalled_ids).update(
            status=AssetUploadStatus.FAILED,
            last_error_code="verify_stalled",
            cleanup_after=now,
        )

    due_ids = list(
        AssetUpload.objects.filter(
            status__in=[
                AssetUploadStatus.COMPLETED,
                AssetUploadStatus.ABORTED,
                AssetUploadStatus.EXPIRED,
                AssetUploadStatus.FAILED,
            ],
            cleanup_after__lte=now,
            object_deleted_at__isnull=True,
        )
        .order_by("cleanup_after")
        .values_list("id", flat=True)[:limit]
    )
    return sum(cleanup_upload(upload_id) for upload_id in due_ids)
