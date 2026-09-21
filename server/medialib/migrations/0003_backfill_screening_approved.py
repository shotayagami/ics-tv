# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""既存 cm_creative を screening_status='approved' にバックフィル (#6 S8)。

考査ゲート導入時に既存素材が一斉に弾かれてフィラー全滅するのを構造的に防ぐため、
導入時点の既存 CM は「みなし考査済 (approved)」とする。新規行は default 'pending'。
"""

from django.db import migrations


def backfill_approved(apps, schema_editor):
    CmCreative = apps.get_model("medialib", "CmCreative")
    CmCreative.objects.all().update(screening_status="approved")


class Migration(migrations.Migration):
    dependencies = [
        ("medialib", "0002_cmcreative_screening_status"),
    ]
    operations = [
        migrations.RunPython(backfill_approved, migrations.RunPython.noop),
    ]
