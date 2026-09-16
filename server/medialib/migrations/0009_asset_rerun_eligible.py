# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
# Generated for 再放送露出フラグ (フィラー送出時に視聴者向け面へ素材タイトルを「再放送」露出してよいか)

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("medialib", "0008_asset_usable_roles"),
    ]

    operations = [
        migrations.AddField(
            model_name="asset",
            name="rerun_eligible",
            field=models.BooleanField(default=False),
        ),
    ]
