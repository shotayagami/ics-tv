# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""fanclub の非同期タスク。クリエイター個人 YouTube チャンネル宛シミュルキャストの窓制御 (#27 Part B)。

docs/fanclub.md §5.5・§6.5: per-creator OAuth (Google Compliance Audit 必須) を避け、クリエイター
自身が YouTube Studio で取得した永続ストリームキーを CF Live Output の宛先として使う。共有 Live
Input (Channel 単位) を常時タップするとその創作者のスロット外でも他番組がシミュルキャストされて
しまうため、on-air 窓 (creator の CreatorSeriesLink 経由の Series の Program) に合わせて
Output.enabled を切替える (作成/削除は最小限、Output リソース自体は使う限り常設)。

beat schedule (config/settings.py):
- reconcile_creator_youtube_outputs: 10 秒周期 (expires 25 秒)。周期がそのまま「番組が公開から
  ファンクラブ限定へ切り替わってから中継が止まるまでの最大遅延」になる。番組境界の到来は DB
  書き込みを伴わないためシグナルでは捕捉できず、ポーリング周期を短く保つのが直接的な手段
  (docs/fanclub.md §9)。定常時は軽量クエリのみで、CF API は状態遷移時しか叩かない。

既知の限界 (docs/fanclub.md §5.5 のグレーゾーン注記、#27 site-only-broadcast.md との統合は別途):
共有 Live Input は CasparCG 本線 channel の出力をそのまま常時ミラーしており、exposure_policy の
YTミラー(公開M/メンバーP)ゲートはそこにかかっていない。よって resolved_exposure_policy による
以下のガードは防御的な追加チェックに過ぎず、本線 Live Input 自体を exposure_policy に応じて
ゲートする仕組みではない (それには専用ミラー channel 相当の分離が要る。将来の統合課題)。
"""

from __future__ import annotations

import logging

import httpx
from celery import shared_task
from django.db.models import Q
from django.utils import timezone

from core import cloudflare_api
from core.cloudflare_api import CloudflareNotConfiguredError
from fanclub.models import Creator, CreatorYoutubeOutput, SlotContractStatus, SlotDestination
from scheduling.models import ExposurePolicy, Program

logger = logging.getLogger(__name__)

_CF_ERRORS = (CloudflareNotConfiguredError, httpx.HTTPError)


def _eligible_creators():
    """youtube_destination=creator_channel の有効な契約 (期間内・active) を持ち、
    宛先ストリームキーを設定済みの creator。"""
    today = timezone.localdate()
    return (
        Creator.objects.filter(
            slot_contracts__status=SlotContractStatus.ACTIVE,
            slot_contracts__youtube_destination=SlotDestination.CREATOR_CHANNEL,
            slot_contracts__starts_on__lte=today,
        )
        .filter(Q(slot_contracts__ends_on__isnull=True) | Q(slot_contracts__ends_on__gte=today))
        .exclude(youtube_destination_stream_key="")
        .exclude(youtube_destination_stream_key__isnull=True)
        .distinct()
    )


def _on_air_programs(creator: Creator, now) -> list[Program]:
    """creator にリンクされたシリーズのうち、今まさに on-air な Program を全 channel 分。

    Program.Meta の ExclusionConstraint (program_no_overlap_per_channel) により、同一 channel の
    同時刻の番組は高々1件なので、返る Program の channel は重複しない。順序は DB 任せにせず
    (start_at, channel_id) で固定する。
    """
    return list(
        Program.objects.select_related("channel", "series")
        .filter(series__fanclub_link__creator=creator, start_at__lte=now, end_at__gt=now)
        .order_by("start_at", "channel_id")
    )


def _is_simulcastable(program: Program) -> bool:
    """この番組をクリエイター自身の公開 YouTube チャンネルへ流してよいか。

    exposure_policy が site_members/members_yt_site (公開 YouTube 非公開の意図) を明示している
    窓は、YTミラー未対応でも防御的にシミュルキャスト対象から外す (§冒頭の限界注記を参照)。
    fc_required_level (ファンクラブ ティア限定、#27 Phase B) が設定されている窓も同様に除外する
    (サイト側でティア限定にした番組を、クリエイター自身の公開 YouTube チャンネルへは
    無条件に流してしまうと、サイト側のゲートが意味を失うため)。
    """
    if program.resolved_exposure_policy != ExposurePolicy.PUBLIC:
        return False
    return program.fc_required_level is None


def _simulcast_program(creator: Creator, now, *, current_channel_id: int | None) -> Program | None:
    """シミュルキャスト元として選ぶ Program (対象が無ければ None)。

    宛先ストリームキーは Creator.youtube_destination_stream_key の1本しか無く、同じ宛先へ複数
    channel を同時送出すると YouTube 側で衝突するため、選べるのは高々1件。除外は「その番組を
    候補から落とす」だけに留め、同一クリエイターが別 channel で同時に持っている完全公開番組を
    巻き添えで止めない (docs/fanclub.md §9 の既知の限界だったもの)。

    候補が複数あるときは、いま有効な Output の channel を優先する。番組境界のたびに宛先が入れ替
    わって YouTube 側の配信が途切れるのを避けるための stickiness で、それが候補から外れた場合の
    み (start_at, channel_id) 順の先頭へ移る。
    """
    candidates = [p for p in _on_air_programs(creator, now) if _is_simulcastable(p)]
    if not candidates:
        return None
    for program in candidates:
        if program.channel_id == current_channel_id:
            return program
    return candidates[0]


@shared_task
def reconcile_creator_youtube_outputs() -> dict[str, int]:
    now = timezone.now()
    stats = {"enabled": 0, "disabled": 0, "created": 0, "errors": 0, "skipped": 0}

    for creator in _eligible_creators().prefetch_related("youtube_outputs__channel"):
        outputs = {o.channel_id: o for o in creator.youtube_outputs.all()}
        enabled_channel_ids = sorted(cid for cid, o in outputs.items() if o.enabled)
        on_air = _simulcast_program(
            creator, now, current_channel_id=enabled_channel_ids[0] if enabled_channel_ids else None
        )
        desired_channel_id = on_air.channel_id if on_air else None

        for channel_id, output in outputs.items():
            desired = channel_id == desired_channel_id
            if output.enabled == desired:
                continue
            try:
                cloudflare_api.update_live_output(
                    output.channel, output.cf_output_uid, enabled=desired
                )
            except _CF_ERRORS:
                logger.exception(
                    "creator youtube output update failed creator=%s channel=%s",
                    creator.id,
                    channel_id,
                )
                stats["errors"] += 1
                continue
            output.enabled = desired
            output.save(update_fields=["enabled", "updated_at"])
            stats["enabled" if desired else "disabled"] += 1

        if on_air is not None and on_air.channel_id not in outputs:
            channel = on_air.channel
            if not channel.cf_live_input_id:
                logger.warning(
                    "creator youtube output: channel=%s has no cf_live_input_id (skip)",
                    channel.slug,
                )
                stats["skipped"] += 1
                continue
            try:
                result = cloudflare_api.create_live_output(
                    channel,
                    target_url=creator.youtube_destination_ingest_url,
                    stream_key=creator.youtube_destination_stream_key,
                )
            except _CF_ERRORS:
                logger.exception(
                    "creator youtube output create failed creator=%s channel=%s",
                    creator.id,
                    channel.id,
                )
                stats["errors"] += 1
                continue
            CreatorYoutubeOutput.objects.create(
                creator=creator,
                channel=channel,
                cf_output_uid=result.get("uid", ""),
                enabled=True,
            )
            stats["created"] += 1
            stats["enabled"] += 1

    return stats
