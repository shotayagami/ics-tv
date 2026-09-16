# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""視聴計測 (#ADMIN-02)。ライブ視聴のハートビートから 同時接続数 + 番組別ユニーク視聴を測る。

匿名 viewer は ランダム cookie (`icstv_vid`, PII 無し) で識別する。在席 (ViewerPresence) は
短命で prune タスクが掃除し、番組別ユニーク視聴 (ProgramView) は累積保持して人気ランキングの
信号に使う。視聴維持率 (retention) は将来課題。
"""

from __future__ import annotations

from django.db import models


class ViewerPresence(models.Model):
    """ライブ視聴の在席 (viewer×channel を upsert)。同時接続数 = 直近 N 秒の distinct viewer。

    プレイヤーが再生中に周期 beat → last_seen を更新。古い行は prune_stale_presence が掃除。
    """

    viewer_id = models.CharField(max_length=64)  # ランダム cookie (匿名・PII 無し)
    channel = models.ForeignKey("core.Channel", on_delete=models.CASCADE, related_name="+")
    program = models.ForeignKey(
        "scheduling.Program",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )  # beat 時点の放送中番組 (フィラー/未投入なら NULL)
    last_seen = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "viewer_presence"
        constraints = [
            models.UniqueConstraint(fields=["viewer_id", "channel"], name="uq_presence_viewer_ch"),
        ]
        indexes = [models.Index(fields=["channel", "last_seen"], name="idx_presence_ch_seen")]

    def __str__(self):
        return f"presence v={self.viewer_id[:8]} ch={self.channel_id}"


class ProgramView(models.Model):
    """番組×viewer のユニーク視聴 (#ADMIN-02)。番組別ユニーク視聴者数 = 人気ランキング信号。

    ある viewer がその番組の放送中に初めて beat した時点で 1 行作成 (以後重複しない)。
    累積保持 (prune しない)。番組削除で CASCADE。
    """

    program = models.ForeignKey(
        "scheduling.Program", on_delete=models.CASCADE, related_name="views"
    )
    viewer_id = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "program_view"
        constraints = [
            models.UniqueConstraint(
                fields=["program", "viewer_id"], name="uq_progview_prog_viewer"
            ),
        ]
        indexes = [models.Index(fields=["program"], name="idx_progview_program")]

    def __str__(self):
        return f"view p={self.program_id} v={self.viewer_id[:8]}"


class AccessLogEntry(models.Model):
    """HTTP アクセスログ (awstats 的な集計用)。AccessLogMiddleware が全リクエストを1行記録する。

    ingress-nginx の既定ログには Host が乗らず tv.*/studio.*/ops.* を区別できない (実運用で判明) ため、
    Host を知っている Django 層で記録する。retention は tasks.prune_access_log が担う。
    """

    host = models.CharField(max_length=100, db_index=True)
    method = models.CharField(max_length=8)
    path = models.CharField(max_length=512)
    status = models.PositiveSmallIntegerField()
    # /api/ /ws/ /static/ /internal/ 等を除いた「ページ」相当かどうか (集計の質を上げる補助フラグ)。
    is_page = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "access_log_entry"
        indexes = [models.Index(fields=["host", "created_at"], name="idx_accesslog_host_created")]

    def __str__(self):
        return f"{self.host} {self.method} {self.path} {self.status}"
