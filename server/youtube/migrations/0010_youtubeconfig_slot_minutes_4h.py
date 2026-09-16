# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""slot_minutes のデフォルトを 120(2h) → 240(4h) へ変更し、既存レコードも更新。"""

from django.db import migrations, models


def set_slot_minutes_4h(apps, schema_editor):
    YoutubeConfig = apps.get_model("youtube", "YoutubeConfig")
    YoutubeConfig.objects.filter(slot_minutes=120).update(slot_minutes=240)


class Migration(migrations.Migration):
    dependencies = [
        ("youtube", "0009_programbroadcast"),
    ]

    operations = [
        migrations.AlterField(
            model_name="youtubeconfig",
            name="slot_minutes",
            field=models.IntegerField(default=240),
        ),
        migrations.RunPython(set_slot_minutes_4h, migrations.RunPython.noop),
    ]
