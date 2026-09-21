# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""既存 Delivery の target_kind を推定して埋める (#5 納品ターゲティング)。

新規列 target_kind は default=oneoff。既存行のうち判別可能なものを補正する:
  ① 子 DeliveryFile に intended_kind=cm を含む → cm
  ② program がレギュラー番組 (program.series あり) → series_episode
  ③ それ以外 → oneoff (既定のまま)
channel は program があれば program.channel から補完 (信頼できる派生)。
episode は履歴に出所がないため埋めない。reverse は noop (列は前 migration が落とす)。
"""

from __future__ import annotations

from django.db import migrations

_CM = "cm"  # medialib AssetKind.CM
_SERIES_EPISODE = "series_episode"
_ONEOFF = "oneoff"


def backfill_target_kind(apps, schema_editor):
    Delivery = apps.get_model("delivery", "Delivery")
    for d in Delivery.objects.all().iterator():
        if d.files.filter(intended_kind=_CM).exists():
            kind = _CM
        elif d.program_id and d.program.series_id:
            kind = _SERIES_EPISODE
        else:
            kind = _ONEOFF
        updates = []
        if d.target_kind != kind:
            d.target_kind = kind
            updates.append("target_kind")
        if d.program_id and d.channel_id is None:
            d.channel_id = d.program.channel_id
            updates.append("channel")
        if updates:
            d.save(update_fields=updates)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("delivery", "0002_delivery_channel_delivery_episode_and_more"),
    ]
    operations = [migrations.RunPython(backfill_target_kind, noop)]
