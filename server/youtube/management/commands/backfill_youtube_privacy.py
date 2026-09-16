# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""既存の YouTube 枠 (broadcast) の公開設定を揃える (status.privacyStatus バックフィル)。

過去に privacy=unlisted/private で作られた broadcast を、現在の運用 (既定 public) に合わせて一括で
更新する。complete/error 以外 (= まだ生きている枠) で broadcast_id を持つものが対象。冪等
(既に希望値なら skip)。新規枠は YoutubeConfig.privacy が public ならそのまま public で生成される。

  python manage.py backfill_youtube_privacy [--channel SLUG] [--privacy public] [--dry-run]

--privacy 未指定なら各チャンネルの YoutubeConfig.privacy を使う (config が無ければ skip)。
注意: live 中の broadcast は YouTube 側で status 更新が拒否される場合がある。その枠は失敗として
ログし継続する (YouTube Studio で手動変更するか、次の枠ローテーションを待つ)。
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from googleapiclient.errors import HttpError

from core.models import Channel
from youtube.api import set_broadcast_privacy
from youtube.models import YoutubeConfig, YoutubeSlot, YtPrivacy, YtSlotStatus


class Command(BaseCommand):
    help = "既存 YouTube 枠の公開設定を揃える (privacyStatus バックフィル)"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--channel", help="対象チャンネル slug (未指定なら enabled 全ch)")
        parser.add_argument(
            "--privacy",
            choices=[c.value for c in YtPrivacy],
            help="揃える公開設定 (未指定なら各chの YoutubeConfig.privacy を使う)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="YouTube を変更せず、対象 broadcast と目標 privacy を表示する",
        )

    def handle(self, *args, **opts) -> None:
        slug = opts.get("channel")
        override = opts.get("privacy")
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
            if override:
                target = override
            else:
                cfg = YoutubeConfig.objects.filter(channel=channel).first()
                if cfg is None:
                    self.stdout.write(
                        f"[skip] {channel.slug}: youtube_config なし (--privacy 指定で対象化)"
                    )
                    continue
                target = cfg.privacy
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
                label = (
                    f"{channel.slug} {slot.window_start:%m-%d %H:%M} {slot.status} {bid} → {target}"
                )
                if opts["dry_run"]:
                    self.stdout.write(f"[dry-run] {label}")
                    ok += 1
                    continue
                try:
                    if set_broadcast_privacy(channel, bid, privacy=target):
                        changed += 1
                        self.stdout.write(self.style.SUCCESS(f"[set] {label}"))
                    else:
                        ok += 1  # 既に希望値
                except (HttpError, ValueError) as e:  # live 枠/削除済み等は弾かれうる
                    failed += 1
                    self.stderr.write(f"[fail] {label}: {e}")
            self.stdout.write(
                self.style.SUCCESS(
                    f"[{channel.slug}] target={target} 変更={changed} 既にOK/skip={ok} 失敗={failed}"
                )
            )
