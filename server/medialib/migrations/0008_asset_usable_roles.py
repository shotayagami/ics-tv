# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
# Generated for 役割フラグ (「フィラーかつ番組」: 1素材を複数用途で使えるように)

from django.db import migrations, models


def backfill_roles(apps, schema_editor):
    """既存素材の役割フラグを現在の kind から導出する (現行ピッカー条件と一致)。

    - usable_as_program: kind == 'program'
    - usable_as_filler:  kind not in ('program', 'cm')  (= filler/bumper/slate)
    """
    Asset = apps.get_model("medialib", "Asset")
    Asset.objects.filter(kind="program").update(usable_as_program=True)
    Asset.objects.exclude(kind__in=["program", "cm"]).update(usable_as_filler=True)


class Migration(migrations.Migration):
    dependencies = [
        ("medialib", "0007_asset_passthrough"),
    ]

    operations = [
        migrations.AddField(
            model_name="asset",
            name="usable_as_program",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="asset",
            name="usable_as_filler",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(backfill_roles, migrations.RunPython.noop),
    ]
