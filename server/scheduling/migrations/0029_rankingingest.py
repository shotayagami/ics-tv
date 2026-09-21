# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('medialib', '0011_asset_offload'),
        ('scheduling', '0028_slidecastingest'),
    ]

    operations = [
        migrations.CreateModel(
            name='RankingIngest',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('source_key', models.CharField(max_length=500, unique=True)),
                ('target_start_at', models.DateTimeField()),
                ('state', models.CharField(choices=[('normalizing', '正規化中'), ('bound', '枠に反映済'), ('no_slot', '対象枠なし'), ('failed', '失敗')], default='normalizing', max_length=12)),
                ('error', models.TextField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('bound_at', models.DateTimeField(blank=True, null=True)),
                ('asset', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='medialib.asset')),
                ('channel', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.channel')),
                ('program', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='scheduling.program')),
            ],
            options={
                'db_table': 'ranking_ingest',
                'indexes': [models.Index(fields=['state'], name='idx_ranking_ingest_state')],
            },
        ),
    ]
