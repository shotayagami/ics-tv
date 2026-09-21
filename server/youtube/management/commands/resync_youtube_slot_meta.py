# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""既存 YouTube 枠のタイトル/説明を現在の YoutubeConfig テンプレートで再生成し反映する。

description_template / title_template を変更した後、generate_slots が skip してしまう既存枠へ
内容を一括で行き渡らせるための同期コマンド。対象は未終了 (window_end > now) の枠。

  python manage.py resync_youtube_slot_meta [--channel SLUG] [--include-manual] [--dry-run]

--dry-run は YouTube/DB を更新せず、各枠の新しいタイトル/説明だけを表示する。
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.models import Channel
from youtube.models import YoutubeConfig, YoutubeSlot, YtSlotStatus
from youtube.tasks import _compose_slot_meta, resync_slot_meta


class Command(BaseCommand):
    help = "既存 YouTube 枠のタイトル/説明をテンプレートで再生成し YouTube に反映する"

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--channel", help="対象チャンネル slug (未指定なら enabled 全チャンネル)"
        )
        parser.add_argument(
            "--include-manual",
            action="store_true",
            help="手動編集 (manual=True) の枠も上書き対象にする",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="YouTube/DB を更新せず、対象枠の新タイトル/説明を表示する",
        )

    def handle(self, *args, **opts) -> None:
        slug = opts.get("channel")
        channels = Channel.objects.filter(enabled=True)
        if slug:
            channels = channels.filter(slug=slug)
            if not channels.exists():
                raise CommandError(f"channel slug={slug!r} が見つからない (enabled=True)")

        for channel in channels:
            cfg = YoutubeConfig.objects.filter(channel=channel).first()
            if cfg is None:
                self.stdout.write(f"[skip] {channel.slug}: youtube_config なし")
                continue
            if opts["dry_run"]:
                self._dry_run(channel, cfg, include_manual=opts["include_manual"])
            else:
                stats = resync_slot_meta(channel.id, include_manual=opts["include_manual"])
                self.stdout.write(self.style.SUCCESS(f"[done] {channel.slug}: {stats}"))

    def _dry_run(self, channel, cfg, *, include_manual: bool) -> None:
        qs = YoutubeSlot.objects.filter(channel=channel, window_end__gt=timezone.now()).exclude(
            status__in=[YtSlotStatus.COMPLETE, YtSlotStatus.ERROR],
        )
        if not include_manual:
            qs = qs.filter(manual=False)
        for slot in qs:
            title, desc = _compose_slot_meta(
                channel,
                slot.window_start,
                slot.window_end,
                cfg.title_template,
                cfg.description_template,
            )
            self.stdout.write(
                f"--- slot={slot.id} status={slot.status} "
                f"window_start={timezone.localtime(slot.window_start).isoformat()}"
            )
            self.stdout.write(f"title: {title}")
            self.stdout.write(f"description:\n{desc}\n")
        self.stdout.write(f"[dry-run] {channel.slug}: {qs.count()} 枠が対象")
