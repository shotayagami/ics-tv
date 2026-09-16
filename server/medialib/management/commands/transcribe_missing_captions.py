# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""既存の番組素材で字幕(VTT)未生成のものを captions キューへ投入する (#ADMIN-04・決定#24)。

Phase A 以前に正規化済みの VOD 素材への遡及用。既定は dry-run、--apply で実投入する。

    python manage.py transcribe_missing_captions            # 対象件数を表示のみ
    python manage.py transcribe_missing_captions --apply    # captions キューへ投入
    python manage.py transcribe_missing_captions --apply --include-failed --limit 20
"""

from django.core.management.base import BaseCommand

from medialib.models import Asset, AssetKind, CaptionStatus, NormalizeStatus


class Command(BaseCommand):
    help = "字幕未生成の番組素材を captions キューへ投入する (既定 dry-run)"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="実際にキュー投入する")
        parser.add_argument(
            "--include-failed",
            action="store_true",
            help="過去に失敗した素材も再投入する",
        )
        parser.add_argument("--limit", type=int, default=0, help="投入上限 (0=無制限)")

    def handle(self, *args, **options):
        statuses = [CaptionStatus.NONE]
        if options["include_failed"]:
            statuses.append(CaptionStatus.FAILED)
        qs = (
            Asset.objects.filter(
                kind=AssetKind.PROGRAM,
                normalize_status=NormalizeStatus.READY,
                caption_status__in=statuses,
            )
            .exclude(r2_key__isnull=True)
            .exclude(r2_key="")
            .order_by("-id")
        )
        limit = options["limit"]
        if limit > 0:
            qs = qs[:limit]
        ids = list(qs.values_list("id", flat=True))

        self.stdout.write(f"対象 {len(ids)} 件 (statuses={statuses})")
        if not options["apply"]:
            self.stdout.write(self.style.WARNING("dry-run (--apply で投入)"))
            return

        from medialib.tasks import transcribe_asset

        for aid in ids:
            transcribe_asset.delay(aid)
        self.stdout.write(self.style.SUCCESS(f"{len(ids)} 件を captions キューへ投入した"))
