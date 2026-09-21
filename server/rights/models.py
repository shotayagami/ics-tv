# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""権利・コンプライアンス (#RIGHTS-02)。

RIGHTS-02 配信権: コンテンツ(番組)ごとの権利元/契約/許諾媒体(linear/vod/youtube)/配信可能期間。
VOD公開はこの権利でゲートする (rights.services.filter_vod_allowed)。権利レコードが無い番組は
編成側 (vod_visibility/vod_available_until) のみで制御 (= 全番組に権利入力を強制しない)。
"""

from __future__ import annotations

from django.db import models


class DistributionRight(models.Model):
    """配信権 (#RIGHTS-02)。番組単位の権利元/契約/許諾媒体/配信可能期間。

    VOD公開は filter_vod_allowed がこの権利で絞る (権利レコードが在るのに有効な vod 許諾が無い番組は
    VOD から外す)。レコードが無い番組は編成側設定のみで判断 (全番組への権利入力を強制しない)。
    """

    program = models.ForeignKey(
        "scheduling.Program", on_delete=models.CASCADE, related_name="distribution_rights"
    )
    holder = models.CharField("権利元", max_length=200)
    contract_ref = models.CharField("契約ID", max_length=100, blank=True, default="")
    allow_linear = models.BooleanField("リニア配信", default=True)
    allow_vod = models.BooleanField("VOD配信", default=True)
    allow_youtube = models.BooleanField("YouTube配信", default=True)
    available_from = models.DateTimeField("配信可能 開始", null=True, blank=True)
    available_until = models.DateTimeField("配信可能 終了", null=True, blank=True)  # null=無期限
    note = models.TextField("備考", blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "rights_distribution"
        indexes = [models.Index(fields=["program"], name="idx_distright_program")]

    def __str__(self):
        return f"right p={self.program_id} {self.holder}"

    def vod_active(self, now) -> bool:
        """今 VOD 許諾が有効か (媒体=vod かつ期間内)。"""
        if not self.allow_vod:
            return False
        if self.available_from and self.available_from > now:
            return False
        return not (self.available_until and self.available_until <= now)
