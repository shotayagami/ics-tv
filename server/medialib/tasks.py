# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""medialib の非同期タスク。正規化(ffmpeg)は CPU/IO 律速のため queue='normalize' で独立スケール。"""

from __future__ import annotations

import logging
import os
from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from core import r2
from medialib.models import Asset, AssetKind, CaptionStatus, NormalizeStatus
from medialib.normalize import normalize

logger = logging.getLogger(__name__)

# 本物のハング打ち切り (invariant: ffmpeg_timeout ≤ soft ≤ hard < broker visibility_timeout)。
# soft で SoftTimeLimitExceeded → normalize() の except で failed 化、hard は最終手段の SIGKILL。
_SOFT_LIMIT = int(os.environ.get("NORMALIZE_SOFT_LIMIT", "21600"))  # 6h
_HARD_LIMIT = int(os.environ.get("NORMALIZE_HARD_LIMIT", "23400"))  # 6.5h

# 字幕自動生成タスクの時間上限 (#ADMIN-04)。int8 CPU で番組素材を文字起こし。normalize より短め。
_CAPTION_SOFT_LIMIT = int(os.environ.get("CAPTION_SOFT_LIMIT", "7200"))  # 2h
_CAPTION_HARD_LIMIT = int(os.environ.get("CAPTION_HARD_LIMIT", "8100"))  # 2.25h


def _maybe_enqueue_captions(asset: Asset) -> None:
    """正規化 READY 後、番組素材だけ字幕自動生成を後続キュー(captions・別 pod)へ流す。

    CM/フィラー/スレート/バンパーは対象外 (ボリュームの大半は Whisper を回さない)。
    CAPTIONS_ENABLED=false でキルスイッチ。字幕生成は送出/正規化に一切影響しない (別キュー)。
    caption_status が既に NONE でない場合 (slidecast 取り込みが台本由来の VTT を先付け済み等)は
    whisper 起こしより正確な字幕が既にあるとみなしスキップする (決定#26)。
    """
    if os.environ.get("CAPTIONS_ENABLED", "true").lower() != "true":
        return
    if asset.normalize_status != NormalizeStatus.READY or asset.kind != AssetKind.PROGRAM:
        return
    if asset.caption_status != CaptionStatus.NONE:
        return
    transcribe_asset.delay(asset.id)


@shared_task(bind=True, max_retries=3, default_retry_delay=60, queue="verify")
def verify_asset_upload(self, upload_id: str) -> str:
    """multipart完了後のbytes/media検証とAsset作成。provider障害は同じsessionで再試行。

    queueはnormalizeから分離した専任のverify (所有者決定G)。理由はCELERY_TASK_ROUTES
    (config/settings.py)のコメントを参照。
    """
    from medialib import upload_services

    try:
        upload_services.verify_upload(upload_id)
    except upload_services.UploadVerificationError:
        upload_services.mark_verification_failed(upload_id)
        return "failed"
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            upload_services.mark_verification_failed(upload_id, "verification_retries_exhausted")
            return "failed"
        raise self.retry(exc=exc) from exc
    return "completed"


@shared_task(queue="default")
def cleanup_asset_uploads() -> int:
    """DB台帳から期限切れ・terminal sessionのexact keyだけを回収する。"""
    from medialib import upload_services

    return upload_services.cleanup_due_uploads()


@shared_task(
    queue="normalize",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    soft_time_limit=_SOFT_LIMIT,
    time_limit=_HARD_LIMIT,
)
def normalize_asset(self, asset_id: int) -> None:
    """OMV マスタ → 正規化 (mezzanine) → R2 アップロード。詳細は docs/overview.md 3.2。

    ffmpeg / ffprobe / boto3 を使い、結果を asset (r2_key / duration_ms / metadata) に反映。
    失敗時は max_retries=3 (60s 遅延) で自動再試行。
    """
    asset = Asset.objects.get(pk=asset_id)
    try:
        normalize(asset)
    except Exception as exc:
        logger.warning(
            "normalize_asset retry asset=%s attempt=%d/%d",
            asset_id,
            self.request.retries + 1,
            self.max_retries + 1,
        )
        raise self.retry(exc=exc) from exc
    # 成功路のみ: 番組素材なら字幕自動生成へ (別キュー・別 pod、送出/正規化に無影響)。
    _maybe_enqueue_captions(asset)


@shared_task(
    queue="captions",
    bind=True,
    max_retries=1,
    default_retry_delay=120,
    soft_time_limit=_CAPTION_SOFT_LIMIT,
    time_limit=_CAPTION_HARD_LIMIT,
)
def transcribe_asset(self, asset_id: int) -> None:
    """mezzanine 音声を faster-whisper で文字起こし → WebVTT を R2 保存 (#ADMIN-04)。

    決定論的失敗 (壊れた素材等) の poison loop を避けるため max_retries=1。
    詳細は medialib.captions.transcribe。
    """
    from medialib.captions import transcribe

    asset = Asset.objects.get(pk=asset_id)
    try:
        transcribe(asset)
    except Exception as exc:
        logger.warning(
            "transcribe_asset retry asset=%s attempt=%d/%d",
            asset_id,
            self.request.retries + 1,
            self.max_retries + 1,
        )
        raise self.retry(exc=exc) from exc


@shared_task
def dispatch_normalize(asset_id: int) -> str:
    """正規化の入口: Windows オフロード可否を判定し、不可ならローカル正規化を投入する。

    queue は CELERY_TASK_ROUTES の明示指定により offload。外部オフロードを無効にしても、
    ローカル正規化の入口として offload queue の消費 worker が必要。
    normalize queue は concurrency=1 でローカル encode に数時間塞がり得るため、軽い振り分け判定を
    そこに積むと Windows が暇でも順番待ちになる。判定は R2 HEAD + ffprobe ヘッダ読みで数秒。
    詳細は docs/normalize-offload.md §3.2。
    """
    from medialib import offload

    try:
        reason = offload.dispatch(asset_id)
    except Exception:
        # dispatch (R2 PUT / DB) の一過性失敗で asset を PENDING のまま取り残さない。安全側で
        # ローカル正規化に倒す (レビュー #8/#14: PENDING はどの reconcile も監視しないため)。
        logger.warning(
            "dispatch_normalize 失敗 asset=%s → ローカル正規化へ", asset_id, exc_info=True
        )
        normalize_asset.delay(asset_id)
        return "dispatch-error-local"
    if reason in offload.LOCAL_REASONS:
        normalize_asset.delay(asset_id)  # クラスタ内正規化 (従来経路)
    # "offload" は依頼済で完了は reconcile が拾う。"not-pending"/"gone" は何もしない。
    return reason


@shared_task
def reconcile_normalize_offload() -> dict:
    """オフロード中 asset の成果物取り込み / 停滞フォールバック / 孤児掃除 (beat 60s, offload queue)。

    _enabled() では gate しない: 途中で機能を無効化しても in-flight は finalize/fallback で
    畳む必要があるため。DB 起点 (offload_request_id 付き PROCESSING) なので R2 全走査はしない。
    詳細は docs/normalize-offload.md §3.3。
    """
    from medialib import offload

    ids = list(
        Asset.objects.filter(
            normalize_status=NormalizeStatus.PROCESSING,
            offload_request_id__isnull=False,
        ).values_list("pk", flat=True)
    )
    stats = {"finalized": 0, "fallback": 0, "waiting": 0}
    for aid in ids:
        outcome = _reconcile_offload_one(aid)
        if outcome in stats:
            stats[outcome] += 1
    stats["orphans"] = offload.scan_orphans()
    return stats


def _reconcile_offload_one(asset_id: int) -> str:
    """1 asset を finalize / fallback / 待機に判定。戻り値 finalized|fallback|waiting|skip。

    重い R2 I/O (ffprobe/copy) はロック外で行い、DB 状態遷移だけを短い select_for_update +
    request_id CAS に閉じる (レビュー #11: 昇格 copy 中に行ロックを保持して運用者の再正規化を
    ブロックしない)。request_id が変わっていれば finalize/fallback をスキップ (運用者の再正規化・
    並行 reconciler と競合しない)。R2 の LastModified で鮮度を測り外部機の時計ずれに依存しない。
    """
    from medialib import offload

    now = timezone.now()
    # ロック外で現況を読む (rid/kind/dispatched + R2 状態)。
    asset = (
        Asset.objects.filter(
            pk=asset_id,
            normalize_status=NormalizeStatus.PROCESSING,
            offload_request_id__isnull=False,
        )
        .only("id", "kind", "offload_request_id", "offload_dispatched_at")
        .first()
    )
    if asset is None or asset.offload_request_id is None:
        return "skip"
    rid = asset.offload_request_id
    kind = asset.kind
    dispatched = asset.offload_dispatched_at or now

    result = offload._read_json(offload.result_key(asset_id))
    if result is not None and result.get("requestId") == rid:
        # 検証 + mezzanine 昇格 copy はロック外 (数分かかりうる)。成功で probe、NG/失敗で None。
        probe = offload.produce_mezzanine(asset_id, kind, result)
        return _finalize_or_fallback(asset_id, rid, kind, probe)

    # 絶対上限: dispatch から MAX_WAIT を超えたら諦めてローカル (ops 通知)。
    if (now - dispatched).total_seconds() > offload._max_wait_sec():
        return _fallback_locked(asset_id, rid, "max-wait 超過", notify_ops=True)

    status = offload._read_json(offload.status_key(asset_id))
    if status is None or status.get("requestId") != rid:
        # まだ claim されていない (or 旧 requestId の残骸) → claim 猶予で判定。
        if (now - dispatched).total_seconds() > offload._claim_sec():
            return _fallback_locked(asset_id, rid, "claim されず (機不在/直後死)")
        return "waiting"

    phase = status.get("phase")
    if phase == "failed":
        return _fallback_locked(asset_id, rid, f"watcher failed: {status.get('error')!r}")
    if phase == "encoding":
        meta = r2.head_object(offload.status_key(asset_id))
        lm = meta.get("last_modified") if meta else None
        if lm is not None and (now - lm).total_seconds() > offload._progress_stale_sec():
            return _fallback_locked(asset_id, rid, "進捗停滞 (encode 中に機停止)")
    return "waiting"


def _finalize_or_fallback(asset_id: int, rid: str, kind: str, probe: dict | None) -> str:
    """ロック外の昇格結果 (probe) を短い CAS トランザクションで DB に反映。"""
    with transaction.atomic():
        asset = (
            Asset.objects.select_for_update(skip_locked=True)
            .filter(
                pk=asset_id,
                normalize_status=NormalizeStatus.PROCESSING,
                offload_request_id=rid,
            )
            .first()
        )
        if asset is None:
            return "skip"  # rid が変わった (再正規化) / 別 reconciler が確定済
        if probe is None:
            return _fallback(asset, "検証NG")
        offload_normalize_finalize(asset, kind, probe)
        return "finalized"


def offload_normalize_finalize(asset: Asset, kind: str, probe: dict) -> None:
    """昇格済 mezzanine を Asset に確定 (READY) + 字幕連鎖 + R2 掃除 (呼び出し側の atomic 内)。"""
    from medialib import normalize as _normalize
    from medialib import offload

    final_key = offload.mezzanine_key(kind, asset.id)
    _normalize.apply_ready_metadata(asset, r2_key=final_key, probe=probe, passthrough=False)
    offload.clear_offload_fields(asset)
    asset_id = asset.id
    # 字幕連鎖は normalize_asset 成功路と同じ (offload 経由の番組だけ字幕が付かない轍を踏まない)。
    # commit 後に投入 (ロールバック時に幽霊タスクを撒かない)。
    transaction.on_commit(lambda: _maybe_enqueue_captions(asset))
    transaction.on_commit(lambda: offload.cleanup_after_finalize(asset_id))
    logger.info("offload finalized asset=%s r2=%s", asset_id, final_key)


def _fallback_locked(asset_id: int, rid: str, reason: str, *, notify_ops: bool = False) -> str:
    """短い CAS トランザクションで fallback (rid 一致時のみ)。実処理は _fallback。"""
    with transaction.atomic():
        asset = (
            Asset.objects.select_for_update(skip_locked=True)
            .filter(
                pk=asset_id,
                normalize_status=NormalizeStatus.PROCESSING,
                offload_request_id=rid,
            )
            .first()
        )
        if asset is None:
            return "skip"
        outcome = _fallback(asset, reason)
    if notify_ops:
        _notify_offload_giveup(asset_id, reason)
    return outcome


def _notify_offload_giveup(asset_id: int, reason: str) -> None:
    """MAX_WAIT 到達等でオフロードを諦めた際の運用アラート (agent liveness と同じ経路)。"""
    from core.notify import notify

    try:
        notify(
            "warn",
            "offload_giveup",
            f"正規化オフロードを諦めローカルへ差戻し (asset={asset_id}, {reason})",
            link=f"/studio/medialib/assets/{asset_id}/",
            throttle=True,
        )
    except Exception:
        logger.warning("offload giveup 通知失敗 asset=%s", asset_id, exc_info=True)


def _fallback(asset: Asset, reason: str) -> str:
    """オフロードを諦めクラスタ内正規化へ差し戻す (呼び出し側の atomic ブロック内で使う)。

    offload 追跡をクリア + normalize_started_at を now にリセット (ローカル正規化がキュー滞留で
    遅れても 8h stale 回収に誤検出されないため・レビュー #2/#12) + request.json を削除
    (watcher の以後の claim を止める) + normalize_asset を on_commit で投入。out/ は消さない
    (書き込み中かもしれないので orphan 掃除に委ねる)。
    """
    from medialib import offload

    offload.clear_offload_fields(asset, reset_started_at=True)
    with offload._suppress():
        r2.delete_object(offload.request_key(asset.id))
    asset_id = asset.id
    transaction.on_commit(lambda: normalize_asset.delay(asset_id))
    logger.warning("offload fallback asset=%s reason=%s → ローカル正規化", asset_id, reason)
    return "fallback"


@shared_task(queue="normalize")
def reconcile_stale_normalize() -> int:
    """閾値超で processing のまま滞留した asset を failed 化する (最終防衛線)。

    acks_late + reject_on_worker_lost + time_limit でほとんどの取りこぼしは再配信/失敗化
    されるが、それでも残る processing 滞留 (早期 ack 済み等) を回収して UI を凍結させない。
    閾値は broker visibility_timeout / hard time_limit より十分長く取る (誤回収防止)。
    自動 retry はせず failed にし、再投入は運用者の「再正規化」操作に委ねる(poison loop 回避)。
    """
    threshold = int(os.environ.get("NORMALIZE_STALE_SEC", "28800"))  # 8h
    cutoff = timezone.now() - timedelta(seconds=threshold)
    stale = list(
        Asset.objects.filter(
            normalize_status=NormalizeStatus.PROCESSING,
            normalize_started_at__lt=cutoff,
        )
    )
    for asset in stale:
        logger.warning(
            "stale normalize 回収 asset=%s started_at=%s (>%ds) → failed",
            asset.id,
            asset.normalize_started_at,
            threshold,
        )
        asset.normalize_status = NormalizeStatus.FAILED
        asset.normalize_error = (
            f"stale: processing が {threshold}s 超で完了せず回収 (再正規化で再投入可)"
        )
        fields = ["normalize_status", "normalize_error"]
        # オフロード中のまま滞留した asset は追跡もクリアし request.json を消す (reconcile_offload の
        # fallback と同じ後始末。offload_request_id を残すと reconcile_offload が finalize を試み続ける
        # ・レビュー #3)。8h まで来ている時点で機は不在/失敗とみなす。
        if asset.offload_request_id:
            from medialib import offload

            with offload._suppress():
                r2.delete_object(offload.request_key(asset.id))
            asset.offload_request_id = None
            asset.offload_dispatched_at = None
            fields += ["offload_request_id", "offload_dispatched_at"]
        asset.save(update_fields=fields)
    if stale:
        logger.warning("reconcile_stale_normalize: %d 件を failed に回収", len(stale))
    return len(stale)
