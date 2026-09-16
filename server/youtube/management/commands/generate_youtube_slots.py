# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""未作成の YouTube 枠を今すぐ生成する (Celery beat の generate_slots を手動トリガ)。

通常は beat が 10 分周期で generate_slots_all を回すが、枠を削除して作り直したい等で即時に
埋めたいときに使う。冪等 (既存 window_start は skip)。新コードでは枠は JST の偶数時 (2h 境界) に揃う。

  python manage.py generate_youtube_slots [--channel SLUG] [--dry-run]

--dry-run は YouTube/DB を変更せず、これから作成される枠 (window/タイトル/説明) を表示する。
"""

from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.models import Channel
from youtube.models import YoutubeConfig, YoutubeSlot
from youtube.tasks import _compose_slot_meta, _windows, generate_slots


class Command(BaseCommand):
    help = "未作成の YouTube 枠を今すぐ生成する (generate_slots を手動トリガ)"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--channel", help="対象チャンネル slug (未指定なら enabled 全ch)")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="YouTube/DB を変更せず、作成される枠を表示する",
        )

    def handle(self, *args, **opts) -> None:
        slug = opts.get("channel")
        channels = Channel.objects.filter(
            enabled=True, youtube_livestream_id__isnull=False
        ).order_by("slug")
        if slug:
            channels = channels.filter(slug=slug)
            if not channels.exists():
                raise CommandError(
                    f"channel slug={slug!r} が見つからない (enabled=True かつ永続 liveStream が必須)"
                )

        for channel in channels:
            cfg = YoutubeConfig.objects.filter(channel=channel).first()
            if cfg is None:
                self.stdout.write(f"[skip] {channel.slug}: youtube_config なし")
                continue
            if opts["dry_run"]:
                self._dry_run(channel, cfg)
            else:
                stats = generate_slots(channel.id)
                self.stdout.write(self.style.SUCCESS(f"[done] {channel.slug}: {stats}"))

    def _dry_run(self, channel, cfg) -> None:
        now = timezone.localtime(timezone.now())
        t1 = now + timedelta(hours=cfg.rolling_hours)
        step = timedelta(minutes=cfg.slot_minutes)
        would_create = 0
        for w_start, w_end in _windows(now, t1, step):
            if YoutubeSlot.objects.filter(channel=channel, window_start=w_start).exists():
                continue
            would_create += 1
            title, desc = _compose_slot_meta(
                channel, w_start, w_end, cfg.title_template, cfg.description_template
            )
            self.stdout.write(
                f"--- {w_start.strftime('%Y-%m-%d %H:%M')}-{w_end.strftime('%H:%M')} JST"
            )
            self.stdout.write(f"title: {title}")
            self.stdout.write(f"description:\n{desc}\n")
        self.stdout.write(f"[dry-run] {channel.slug}: {would_create} 枠を新規作成予定")
