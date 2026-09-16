# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
import uuid

from django.db import models


class PlayoutAction(models.TextChoices):
    PLAY_ASSET = "play_asset", "play_asset"
    PLAY_CM = "play_cm", "play_cm"
    PLAY_CM_BUNDLE = "play_cm_bundle", "play_cm_bundle"
    CUT_LIVE = "cut_live", "cut_live"
    PLAY_FILLER = "play_filler", "play_filler"
    PLAY_SLATE = "play_slate", "play_slate"
    YT_TRANSITION = "yt_transition", "yt_transition"
    CLEAR_SLATE = "clear_slate", "clear_slate"  # スレート解除 (CLEAR {ch}-90。#7 O4)
    OVERLAY_OP = "overlay_op", "overlay_op"  # 手動グラフィック操作 (#18 §B)
    PLAY_VT = "play_vt", "play_vt"
    # exposure_policy(#27, docs/site-only-broadcast.md): YTミラー(公開M/メンバーP)の制御。
    # plan() には通さず agent が action 判定して MirrorController へ委譲する (§4.4)。
    YT_MIRROR_FILLER = "yt_mirror_filler", "yt_mirror_filler"
    YT_MIRROR_ROUTE = "yt_mirror_route", "yt_mirror_route"


class PlayoutStatus(models.TextChoices):
    SCHEDULED = "scheduled", "scheduled"
    EXECUTING = "executing", "executing"
    DONE = "done", "done"
    SKIPPED = "skipped", "skipped"
    FAILED = "failed", "failed"
    # リゾルバ再解決で消えた未実行イベント。agent には tombstone=true で伝搬される。
    CANCELLED = "cancelled", "cancelled"


class PlayoutEvent(models.Model):
    """as-run / 解決済み送出イベント。scheduler が決定的 uuid5 を idempotency_key に設定する。"""

    idempotency_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    # gRPC SubscribeEvents の resume cursor。BIGSERIAL を独立 SEQUENCE で発行し、
    # BEFORE INSERT/UPDATE トリガーで自動採番 (update 時も最新値に bump → agent が拾える)。
    # NULL は許容 (Django ORM 経由でも空のままレコード作成可能。トリガーが必ず埋める)。
    sync_seq = models.BigIntegerField(null=True, blank=True, unique=True, editable=False)
    channel = models.ForeignKey(
        "core.Channel",
        on_delete=models.PROTECT,
        related_name="playout_events",
    )
    scheduled_at = models.DateTimeField()
    action = models.CharField(max_length=20, choices=PlayoutAction.choices)
    # 対象(action により使い分け; ログ保全のため SET_NULL)
    asset = models.ForeignKey(
        "medialib.Asset",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    program = models.ForeignKey(
        "scheduling.Program",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    live_source = models.ForeignKey(
        "core.LiveSource",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    cm_bundle = models.ForeignKey(
        "medialib.CmBundle",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    youtube_slot = models.ForeignKey(
        "youtube.YoutubeSlot",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    # 放確の確定的な逆引き経路 (#6 S10)。resolver が PLAY_CM 生成時にセット。program 削除で
    # ad_break_item は CASCADE するため SET_NULL (過去実績の契約帰属は airing 側スナップショットが保持)。
    ad_break_item = models.ForeignKey(
        "scheduling.AdBreakItem",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    params = models.JSONField(default=dict, blank=True)  # AMCP補助(layer/transition等)
    status = models.CharField(
        max_length=12,
        choices=PlayoutStatus.choices,
        default=PlayoutStatus.SCHEDULED,
    )
    actual_at = models.DateTimeField(null=True, blank=True)
    note = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "playout_event"
        indexes = [
            models.Index(fields=["channel", "scheduled_at"], name="idx_pe_ch_sched"),
            models.Index(
                fields=["status"],
                condition=models.Q(status="scheduled"),
                name="idx_pe_scheduled",
            ),
        ]


class AgentStatus(models.Model):
    """送出 agent の死活/監視状態 (channel 1:1)。Heartbeat が upsert し、運行ダッシュボードと
    死活 beat が読む (docs/operations.md 決定 O6/O7)。"""

    channel = models.OneToOneField(
        "core.Channel",
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="agent_status",
    )
    last_heartbeat_at = models.DateTimeField()
    last_received_seq = models.BigIntegerField(default=0)
    queue_depth = models.IntegerField(default=0)
    caspar_health = models.CharField(max_length=20, blank=True, null=True)
    feed_state = models.CharField(max_length=10, blank=True, null=True)  # ok / lost / idle
    slate_active = models.BooleanField(default=False)
    # operator トグルの意図 (フラップ停止でも ON のまま。Heartbeat と双方向同期)
    auto_return = models.BooleanField(default=True)
    # フラップ保護による一時サスペンド (agent 内部状態。auto_return とは別管理)
    auto_return_suspended = models.BooleanField(default=False)
    # 死活 beat の状態遷移検出用 (online↔offline の二重通知防止)
    offline_notified = models.BooleanField(default=False)
    # スレート固着 beat の状態遷移検出用 (放送中スレート固着の二重通知防止)
    slate_stuck_notified = models.BooleanField(default=False)
    # #18 CG レイヤ状態スナップショット [{layer,role,occupied,producer,content,visible}, …]
    layers = models.JSONField(default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "agent_status"

    def __str__(self):
        return f"agent_status({self.channel_id})"
