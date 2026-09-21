# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""既存の YouTube 枠 (broadcast) の埋め込みを有効化する (enableEmbed=True バックフィル)。

insert_broadcast に enableEmbed=True を入れる前に作られた broadcast は contentDetails.enableEmbed
が既定 false のままで、公開ビューア /ch/<slug>/ の埋め込みプレーヤーが「エラー153 動画プレーヤーの
設定エラー」になる。complete/error 以外 (= まだ生きている枠) で broadcast_id を持つものを対象に、
YouTube API で enableEmbed を有効化する。冪等 (既に true は skip)。

  python manage.py backfill_youtube_embed [--channel SLUG] [--dry-run]

注意: live 中の broadcast は YouTube 側で contentDetails 更新が拒否される場合がある。その枠は
失敗としてログして継続する (YouTube Studio で手動 ON するか、次の枠ローテーションを待つ)。
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from googleapiclient.errors import HttpError

from core.models import Channel
from youtube.api import set_broadcast_embed
from youtube.models import YoutubeSlot, YtSlotStatus


class Command(BaseCommand):
    help = "既存 YouTube 枠の埋め込みを有効化する (enableEmbed バックフィル)"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--channel", help="対象チャンネル slug (未指定なら enabled 全ch)")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="YouTube を変更せず、対象 broadcast を表示する",
        )

    def handle(self, *args, **opts) -> None:
        slug = opts.get("channel")
        channels = Channel.objects.filter(enabled=True).order_by("slug")
        if slug:
            channels = channels.filter(slug=slug)
            if not channels.exists():
                raise CommandError(f"channel slug={slug!r} が見つからない (enabled=True が必須)")

        # complete/error 以外 (= これから/現在使う枠) で broadcast_id を持つものが対象。
        live_statuses = [
            YtSlotStatus.CREATED,
            YtSlotStatus.READY,
            YtSlotStatus.TESTING,
            YtSlotStatus.LIVE,
        ]
        for channel in channels:
            slots = (
                YoutubeSlot.objects.filter(
                    channel=channel,
                    status__in=live_statuses,
                    broadcast_id__isnull=False,
                )
                .exclude(broadcast_id="")
                .order_by("window_start")
            )
            changed = ok = failed = 0
            for slot in slots:
                bid = slot.broadcast_id
                if not bid:  # queryset で除外済みだが型と防御のため
                    continue
                label = f"{channel.slug} {slot.window_start:%m-%d %H:%M} {slot.status} {bid}"
                if opts["dry_run"]:
                    self.stdout.write(f"[dry-run] {label}")
                    ok += 1
                    continue
                try:
                    if set_broadcast_embed(channel, bid, enable=True):
                        changed += 1
                        self.stdout.write(self.style.SUCCESS(f"[embed on] {label}"))
                    else:
                        ok += 1  # 既に enableEmbed=true
                except (HttpError, ValueError) as e:  # live 枠/削除済み等は弾かれうる
                    failed += 1
                    self.stderr.write(f"[fail] {label}: {e}")
            self.stdout.write(
                self.style.SUCCESS(
                    f"[{channel.slug}] 有効化={changed} 既にOK/skip={ok} 失敗={failed}"
                )
            )
