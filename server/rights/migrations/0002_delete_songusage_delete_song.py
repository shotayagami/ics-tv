# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
# 楽曲使用管理 (#RIGHTS-01) の物理除去。SongUsage.song が PROTECT の FK のため、
# 参照側 SongUsage を先に落としてから Song を落とす。

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("rights", "0001_initial"),
    ]

    operations = [
        migrations.DeleteModel(
            name="SongUsage",
        ),
        migrations.DeleteModel(
            name="Song",
        ),
    ]
