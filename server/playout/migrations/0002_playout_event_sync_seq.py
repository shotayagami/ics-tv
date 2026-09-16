# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""playout_event に sync_seq を追加。gRPC SubscribeEvents の resume cursor。

BIGSERIAL を独立 SEQUENCE で発行し、BEFORE INSERT OR UPDATE トリガーで自動採番。
UPDATE のたびに seq を bump させるのは意図的: agent は「最新状態」を都度受け取りたいので、
status 変更 (scheduled → done など) も新しい sync_seq として伝送される。
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("playout", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="playoutevent",
            name="sync_seq",
            field=models.BigIntegerField(blank=True, editable=False, null=True, unique=True),
        ),
        migrations.RunSQL(
            sql=(
                "CREATE SEQUENCE playout_event_sync_seq;\n"
                "CREATE OR REPLACE FUNCTION bump_playout_event_sync_seq() RETURNS trigger AS $$\n"
                "BEGIN\n"
                "  NEW.sync_seq := nextval('playout_event_sync_seq');\n"
                "  RETURN NEW;\n"
                "END;\n"
                "$$ LANGUAGE plpgsql;\n"
                "CREATE TRIGGER trg_playout_event_sync_seq\n"
                "  BEFORE INSERT OR UPDATE ON playout_event\n"
                "  FOR EACH ROW\n"
                "  EXECUTE FUNCTION bump_playout_event_sync_seq();"
            ),
            reverse_sql=(
                "DROP TRIGGER IF EXISTS trg_playout_event_sync_seq ON playout_event;\n"
                "DROP FUNCTION IF EXISTS bump_playout_event_sync_seq();\n"
                "DROP SEQUENCE IF EXISTS playout_event_sync_seq;"
            ),
        ),
    ]
