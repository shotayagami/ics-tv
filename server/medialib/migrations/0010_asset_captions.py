# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
# Generated for 字幕(WebVTT)自動生成のサイドカーフィールド (#ADMIN-04 / PLAYER-04・決定#24)。
# 既定 none/空 のみ追加 = 既存行/既存挙動は不変。

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("medialib", "0009_asset_rerun_eligible"),
    ]

    operations = [
        migrations.AddField(
            model_name="asset",
            name="caption_status",
            field=models.CharField(
                choices=[
                    ("none", "未生成"),
                    ("processing", "生成中"),
                    ("ready", "生成済"),
                    ("failed", "失敗"),
                ],
                default="none",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="asset",
            name="caption_r2_key",
            field=models.CharField(blank=True, max_length=500, null=True),
        ),
        migrations.AddField(
            model_name="asset",
            name="caption_lang",
            field=models.CharField(default="ja", max_length=8),
        ),
        migrations.AddField(
            model_name="asset",
            name="caption_error",
            field=models.TextField(blank=True, null=True),
        ),
    ]
