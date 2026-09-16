# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""sales の非同期タスク (#6)。契約変更/考査変更時の再充填。"""

from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from medialib.models import CmBundleItem, CmCreative
from playout.models import PlayoutAction, PlayoutEvent, PlayoutStatus
from sales.models import (
    Airing,
    MakeGood,
    MakeGoodStatus,
    Placement,
    SponsorshipMaterial,
    SpotOrderMaterial,
)
from scheduling.models import AdBreak, AdBreakItem

logger = logging.getLogger(__name__)

_AIR_ACTIONS = (PlayoutAction.PLAY_CM, PlayoutAction.PLAY_CM_BUNDLE)


@shared_task
def refill_window(channel_id: int, hours: int = 48) -> dict:
    """契約/考査変更を反映するため、窓内 ad_break のうち全 playout_event が SCHEDULED の枠を
    再充填する (#6 S11)。

    紐づく playout_event に EXECUTING/DONE を含む枠は不変。安全な枠のみ ad_break_item を削除
    (placement は CASCADE) → 即時再解決で provider が再充填。フォールバック仕様
    「契約は登録から最大 48h 後の枠から効く」は refill が走れば実際は即反映。
    """
    now = timezone.now()
    horizon = now + timedelta(hours=hours)
    breaks = AdBreak.objects.filter(
        program__channel_id=channel_id,
        program__end_at__gt=now,
        program__start_at__lt=horizon,
    )
    cleared = 0
    for br in breaks:
        events = PlayoutEvent.objects.filter(ad_break_item__ad_break=br)
        if events.exclude(status=PlayoutStatus.SCHEDULED).exists():
            continue  # EXECUTING/DONE を含む枠は不変
        with transaction.atomic():
            deleted, _ = AdBreakItem.objects.filter(ad_break=br).delete()
        if deleted:
            cleared += 1

    if cleared:
        # 即時再解決で再充填 (provider が新しい契約条件で割付)
        from scheduling.tasks import resolve_channel_now

        resolve_channel_now(channel_id)
    logger.info("refill_window channel=%s cleared=%d", channel_id, cleared)
    return {"channel_id": channel_id, "cleared": cleared}


# ---- 放確台帳 (#6 S7/S10/S12) ----


def _placement_contract(ad_break_item_id) -> tuple[int | None, int | None]:
    """placement 経由で (spot_order_id, sponsorship_id) を確定逆引き。無ければ (None, None)。"""
    if ad_break_item_id is None:
        return (None, None)
    pl = (
        Placement.objects.filter(ad_break_item_id=ad_break_item_id)
        .values("spot_order_id", "sponsorship_id")
        .first()
    )
    if pl is None:
        return (None, None)
    return (pl["spot_order_id"], pl["sponsorship_id"])


def _bundle_contract(cm_asset_id) -> tuple[int | None, int | None]:
    """バンドル CM の契約帰属を素材一致で解決。一意なら採用、複数/無は契約外 (None,None)+警告。"""
    so_ids = list(
        SpotOrderMaterial.objects.filter(cm_asset_id=cm_asset_id)
        .values_list("spot_order_id", flat=True)
        .distinct()
    )
    sp_ids = list(
        SponsorshipMaterial.objects.filter(cm_asset_id=cm_asset_id)
        .values_list("sponsorship_id", flat=True)
        .distinct()
    )
    if len(so_ids) + len(sp_ids) == 1:
        return (so_ids[0], None) if so_ids else (None, sp_ids[0])
    if len(so_ids) + len(sp_ids) > 1:
        logger.warning("bundle CM %s が複数契約に該当 → 契約外扱い", cm_asset_id)
    return (None, None)


def _upsert_airing(ev, bundle_seq, cm_asset_id, so_id, sp_id, duration_ms, aired_at, estimated):
    Airing.objects.update_or_create(
        playout_event=ev,
        bundle_seq=bundle_seq,
        defaults={
            "channel_id": ev.channel_id,
            "cm_asset_id": cm_asset_id,
            "spot_order_id": so_id,
            "sponsorship_id": sp_id,
            "program_id": ev.program_id,
            "program_title": ev.program.title if ev.program else None,
            "duration_ms": duration_ms,
            "aired_at": aired_at,
            "aired_at_estimated": estimated,
        },
    )


@shared_task
def record_airing(playout_event_id: int) -> dict:
    """初回 DONE の CM/CM バンドルを放確台帳 airing に記録 (S7)。(event, bundle_seq) UNIQUE で冪等。"""
    ev = (
        PlayoutEvent.objects.select_related("program")
        .filter(pk=playout_event_id, status=PlayoutStatus.DONE)
        .first()
    )
    if ev is None or ev.action not in _AIR_ACTIONS:
        return {"skipped": True}
    aired_at = ev.actual_at or ev.scheduled_at  # NULL は scheduled_at で補完
    estimated = ev.actual_at is None

    if ev.action == PlayoutAction.PLAY_CM:
        if ev.asset_id is None:
            return {"skipped": True}
        cm = CmCreative.objects.select_related("asset").filter(pk=ev.asset_id).first()
        if cm is None:
            return {"skipped": True}
        so_id, sp_id = _placement_contract(ev.ad_break_item_id)
        duration = int(ev.params.get("duration_ms") or (cm.asset.duration_ms or 0))
        _upsert_airing(ev, 0, cm.asset_id, so_id, sp_id, duration, aired_at, estimated)
        return {"airing": 1}

    # play_cm_bundle: CmBundleItem を展開し 1 本ごとに airing (S12)
    if ev.cm_bundle_id is None:
        return {"skipped": True}
    items = list(
        CmBundleItem.objects.filter(cm_bundle_id=ev.cm_bundle_id)
        .select_related("cm_asset__asset")
        .order_by("seq")
    )
    offset_ms = 0
    for it in items:
        dur = it.cm_asset.asset.duration_ms or 0
        so_id, sp_id = _bundle_contract(it.cm_asset_id)
        _upsert_airing(
            ev,
            it.seq,
            it.cm_asset_id,
            so_id,
            sp_id,
            dur,
            aired_at + timedelta(milliseconds=offset_ms),
            estimated,
        )
        offset_ms += dur
    return {"airing": len(items)}


@shared_task
def reconcile_airings(hours: int = 48) -> dict:
    """DONE なのに airing が無い CM/バンドルを走査して record_airing 再発火 (broker 不達の回収)。"""
    since = timezone.now() - timedelta(hours=hours)
    evs = (
        PlayoutEvent.objects.filter(
            status=PlayoutStatus.DONE,
            action__in=_AIR_ACTIONS,
            scheduled_at__gte=since,
            airings__isnull=True,
        )
        .distinct()
        .values_list("id", flat=True)
    )
    n = 0
    for ev_id in evs:
        record_airing(ev_id)
        n += 1
    return {"reconciled": n}


@shared_task
def detect_missed_airings(hours: int = 48) -> dict:
    """failed/skipped の placement 済みイベントから make_good(open) を起票 (CANCELLED は対象外)。"""
    since = timezone.now() - timedelta(hours=hours)
    evs = PlayoutEvent.objects.filter(
        status__in=(PlayoutStatus.FAILED, PlayoutStatus.SKIPPED),
        action=PlayoutAction.PLAY_CM,
        ad_break_item__isnull=False,
        scheduled_at__gte=since,
    )
    created = 0
    for ev in evs:
        pl = Placement.objects.filter(ad_break_item_id=ev.ad_break_item_id).first()
        if pl is None:
            continue  # 契約外は欠送対象外
        _, was_created = MakeGood.objects.get_or_create(
            missed_playout_event=ev,
            defaults={
                "spot_order_id": pl.spot_order_id,
                "sponsorship_id": pl.sponsorship_id,
                "reason": f"{ev.status} 欠送",
                "status": MakeGoodStatus.OPEN,
            },
        )
        if was_created:
            created += 1
    return {"missed": created}
