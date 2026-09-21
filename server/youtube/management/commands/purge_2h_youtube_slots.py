# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""未開始 (ready) の将来枠を YouTube API でキャンセルして DB から削除する。

2h → 4h 切り替え後に旧 2h 境界の ready 枠を一掃し、beat が 4h 枠を再生成できるようにする。
live/complete/error 枠には触れない。

  python manage.py purge_2h_youtube_slots [--channel SLUG] [--dry-run]
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from googleapiclient.errors import HttpError

from youtube.api import delete_broadcast
from youtube.models import YoutubeSlot, YtSlotStatus


class Command(BaseCommand):
    help = "未開始 ready 枠を YouTube API でキャンセル・DB 削除する (2h→4h 切り替え後の一掃用)"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--channel", help="対象チャンネル slug (未指定なら enabled 全ch)")
        parser.add_argument("--dry-run", action="store_true", help="削除せずに対象枠を表示する")

    def handle(self, *args, **opts) -> None:
        now = timezone.now()
        qs = YoutubeSlot.objects.filter(
            status=YtSlotStatus.READY, window_start__gt=now
        ).select_related("channel")
        if opts.get("channel"):
            qs = qs.filter(channel__slug=opts["channel"])
            if not qs.exists():
                raise CommandError(
                    f"対象スロットが見つからない (channel={opts['channel']!r}, status=ready, future)"
                )

        total = qs.count()
        self.stdout.write(f"対象: {total} 枠 (ready かつ window_start > now)")

        if opts["dry_run"]:
            for slot in qs.order_by("window_start"):
                self.stdout.write(
                    f"  [dry] {slot.channel.slug} "
                    f"{slot.window_start.strftime('%Y-%m-%d %H:%M')}-"
                    f"{slot.window_end.strftime('%H:%M')} JST  broadcast={slot.broadcast_id}"
                )
            self.stdout.write(f"[dry-run] {total} 枠を削除予定 (実行は --dry-run を外す)")
            return

        deleted = 0
        api_errors = 0
        for slot in qs.order_by("window_start"):
            if slot.broadcast_id:
                try:
                    delete_broadcast(slot.channel, slot.broadcast_id)
                except HttpError as e:
                    if e.resp.status == 404:
                        pass  # 既に削除済み
                    else:
                        self.stderr.write(f"  [warn] API error slot={slot.pk}: {e}")
                        api_errors += 1
            slot.delete()
            deleted += 1

        self.stdout.write(
            self.style.SUCCESS(f"完了: {deleted} 枠削除 (API エラー {api_errors} 件は warn のみ)")
        )
        self.stdout.write("次の beat サイクル (最大 10 分) で 4h 枠が自動生成されます。")
