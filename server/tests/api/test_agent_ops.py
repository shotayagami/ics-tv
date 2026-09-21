# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""#7 Phase O-A サーバ側: Heartbeat 永続化 / ReportInterrupt / 死活 beat / notifier。"""

from __future__ import annotations

import uuid
from datetime import timedelta

from asgiref.sync import async_to_sync
from django.utils import timezone
from icstv.v1 import playout_pb2

from core import notify as notify_mod
from core.models import Notification, NotificationSeverity
from playout.grpc_service import (
    _fetch_events_after,
    apply_interrupt,
    persist_heartbeat,
)
from playout.models import AgentStatus, PlayoutAction, PlayoutEvent, PlayoutStatus
from playout.tasks import check_agent_liveness

KIND = playout_pb2.ReportInterruptRequest.InterruptKind


# ---- notifier ----


def test_notify_creates_row_and_throttles(channel):
    n1 = notify_mod.notify(NotificationSeverity.WARN, "cm_stock_low", "在庫不足", channel=channel)
    assert n1.pk and n1.severity == "warn" and n1.kind == "cm_stock_low"
    # 同一 (kind, channel) はクールダウン内 → 記録は残るが配送は抑止 (_recently_notified True)
    assert notify_mod._recently_notified("cm_stock_low", channel.id) is True
    notify_mod.notify(NotificationSeverity.WARN, "cm_stock_low", "在庫不足2", channel=channel)
    assert Notification.objects.filter(kind="cm_stock_low").count() == 2


# ---- Heartbeat 永続化 ----


def test_persist_heartbeat_upserts_agent_status(channel):
    persist_heartbeat(channel.slug, 42, 3, "healthy", False, "ok", True, False)
    st = AgentStatus.objects.get(channel=channel)
    assert st.last_received_seq == 42
    assert st.queue_depth == 3
    assert st.caspar_health == "healthy"
    assert st.feed_state == "ok"
    assert st.auto_return is True
    # 2 回目は upsert (重複行を作らない)
    persist_heartbeat(channel.slug, 50, 0, "healthy", True, "lost", True, False)
    st.refresh_from_db()
    assert st.last_received_seq == 50
    assert st.slate_active is True
    assert AgentStatus.objects.filter(channel=channel).count() == 1


def test_persist_heartbeat_keeps_offline_notified(channel):
    """Heartbeat は offline_notified を触らない (遷移検出は beat 管轄)。"""
    persist_heartbeat(channel.slug, 1, 0, "healthy", False, "ok", True, False)
    AgentStatus.objects.filter(channel=channel).update(offline_notified=True)
    persist_heartbeat(channel.slug, 2, 0, "healthy", False, "ok", True, False)
    assert AgentStatus.objects.get(channel=channel).offline_notified is True


# ---- ReportInterrupt ----


def test_apply_interrupt_slate_on(channel):
    persist_heartbeat(channel.slug, 1, 0, "healthy", False, "ok", True, False)
    key = str(uuid.uuid4())
    assert apply_interrupt(channel.slug, KIND.INTERRUPT_KIND_SLATE_ON, key, None, "publisher lost")

    ev = PlayoutEvent.objects.get(idempotency_key=key)
    assert ev.action == PlayoutAction.PLAY_SLATE
    assert ev.status == PlayoutStatus.DONE  # DONE で記録 → O10 で agent へ配信されない
    assert ev.params.get("interrupt") is True

    st = AgentStatus.objects.get(channel=channel)
    assert st.slate_active is True and st.feed_state == "lost"
    assert Notification.objects.filter(kind="feed_lost", severity="crit").exists()

    # O10 連携: DONE の割り込み記録は agent へエコー配信されない
    got, _ = async_to_sync(_fetch_events_after)(channel.slug, 0, 100)
    assert all(str(e.idempotency_key) != key for e in got)

    # 冪等: 同 interrupt_key の再送で重複イベントを作らない
    apply_interrupt(channel.slug, KIND.INTERRUPT_KIND_SLATE_ON, key, None, "publisher lost")
    assert PlayoutEvent.objects.filter(idempotency_key=key).count() == 1


def test_apply_interrupt_feed_restored(channel):
    persist_heartbeat(channel.slug, 1, 0, "healthy", True, "lost", True, False)
    key = str(uuid.uuid4())
    assert apply_interrupt(channel.slug, KIND.INTERRUPT_KIND_FEED_RESTORED, key, None, "")
    ev = PlayoutEvent.objects.get(idempotency_key=key)
    assert ev.action == PlayoutAction.CUT_LIVE and ev.status == PlayoutStatus.DONE
    st = AgentStatus.objects.get(channel=channel)
    assert st.slate_active is False and st.feed_state == "ok"
    assert Notification.objects.filter(kind="feed_restored", severity="info").exists()


def test_apply_interrupt_auto_return_suspended(channel):
    persist_heartbeat(channel.slug, 1, 0, "healthy", False, "ok", True, False)
    key = str(uuid.uuid4())
    assert apply_interrupt(channel.slug, KIND.INTERRUPT_KIND_AUTO_RETURN_SUSPENDED, key, None, "")
    st = AgentStatus.objects.get(channel=channel)
    # フラップ停止は auto_return(operator 意図) とは別フィールドに反映
    assert st.auto_return_suspended is True
    assert st.auto_return is True
    assert Notification.objects.filter(kind="auto_return_suspended", severity="warn").exists()


def test_apply_interrupt_unknown_channel_returns_false(db):
    key = str(uuid.uuid4())
    assert apply_interrupt("nope", KIND.INTERRUPT_KIND_SLATE_ON, key, None, "") is False


# ---- 死活 beat ----


def test_check_agent_liveness_offline_then_recover(channel):
    # 心拍が 5 分前 = 途絶 (>90s)
    persist_heartbeat(channel.slug, 1, 0, "healthy", False, "ok", True, False)
    AgentStatus.objects.filter(channel=channel).update(
        last_heartbeat_at=timezone.now() - timedelta(minutes=5)
    )
    res = check_agent_liveness()
    assert res["offline"] == 1
    assert AgentStatus.objects.get(channel=channel).offline_notified is True
    assert Notification.objects.filter(kind="agent_offline", severity="crit").count() == 1

    # 再実行しても二重通知しない (遷移していないため)
    res2 = check_agent_liveness()
    assert res2["offline"] == 0
    assert Notification.objects.filter(kind="agent_offline").count() == 1

    # 心拍復帰 → recovered 通知 + フラグ解除
    AgentStatus.objects.filter(channel=channel).update(last_heartbeat_at=timezone.now())
    res3 = check_agent_liveness()
    assert res3["recovered"] == 1
    assert AgentStatus.objects.get(channel=channel).offline_notified is False
    assert Notification.objects.filter(kind="agent_recovered", severity="info").count() == 1


# ---- op_overlay: 手動グラフィック (#18 §B) ----


def test_op_overlay_emits_overlay_op_event(staff_client, channel):
    res = staff_client.post(
        f"/ops/ch/{channel.slug}/overlay/",
        {"layer": "40", "kind": "text", "op": "show", "text": "速報テスト"},
    )
    assert res.status_code == 200
    ev = (
        PlayoutEvent.objects.filter(channel=channel, action=PlayoutAction.OVERLAY_OP)
        .order_by("-created_at")
        .first()
    )
    assert ev is not None
    assert ev.params["overlay_layer"] == "40"
    assert ev.params["overlay_op"] == "show"
    assert ev.params["overlay_kind"] == "text"
    assert "速報テスト" in ev.params["overlay_data"]


def test_op_overlay_chime_category_fires_layer41(staff_client, channel):
    """手動速報で "cat:<category>" を選ぶと層41で当該カテゴリの音 (選択無→既定) を併発する。"""
    res = staff_client.post(
        f"/ops/ch/{channel.slug}/overlay/",
        {"layer": "40", "kind": "text", "op": "show", "text": "速報", "chime": "cat:general"},
    )
    assert res.status_code == 200
    chimes = PlayoutEvent.objects.filter(
        channel=channel, action=PlayoutAction.OVERLAY_OP, params__overlay_layer="41"
    )
    assert chimes.filter(params__overlay_op="show", params__overlay_clip="sfx/general").exists()
    assert chimes.filter(params__overlay_op="hide").exists()


def test_op_overlay_chime_library_pick(staff_client, channel):
    """ "snd:<id>" でライブラリ音源を発火時に直接指定できる。"""
    from core.models import ChimeSound

    s = ChimeSound.objects.create(name="ピック", r2_key="chime/lib/pk.mp3")
    res = staff_client.post(
        f"/ops/ch/{channel.slug}/overlay/",
        {"layer": "40", "kind": "text", "op": "show", "text": "速報", "chime": f"snd:{s.id}"},
    )
    assert res.status_code == 200
    assert PlayoutEvent.objects.filter(
        channel=channel, params__overlay_layer="41", params__overlay_clip="chime/lib/pk"
    ).exists()


def test_op_overlay_chime_none_is_silent(staff_client, channel):
    """chime=none(または未指定)では層41を出さない。"""
    res = staff_client.post(
        f"/ops/ch/{channel.slug}/overlay/",
        {"layer": "40", "kind": "text", "op": "show", "text": "速報", "chime": "none"},
    )
    assert res.status_code == 200
    assert not PlayoutEvent.objects.filter(channel=channel, params__overlay_layer="41").exists()


def test_op_overlay_rejects_reserved_layer_90(staff_client, channel):
    res = staff_client.post(
        f"/ops/ch/{channel.slug}/overlay/", {"layer": "90", "op": "show", "text": "x"}
    )
    assert res.status_code == 200  # _ok でメッセージ返すが event は作らない
    assert not PlayoutEvent.objects.filter(
        channel=channel, action=PlayoutAction.OVERLAY_OP
    ).exists()


def test_op_overlay_graphic_builds_elements(staff_client, channel):
    import json

    res = staff_client.post(
        f"/ops/ch/{channel.slug}/overlay/",
        {
            "layer": "45",
            "kind": "graphic",
            "op": "show",
            "image_url": "https://tv.example.com/t/thumbnails/x.png",
            "text": "組テスト",
            "x": "5",
            "y": "80",
            "w": "30",
        },
    )
    assert res.status_code == 200
    ev = (
        PlayoutEvent.objects.filter(channel=channel, action=PlayoutAction.OVERLAY_OP)
        .order_by("-created_at")
        .first()
    )
    el = json.loads(ev.params["overlay_data"])["elements"][0]
    assert el["media"] == "image" and el["url"].endswith("/x.png")
    assert el["text"] == "組テスト" and el["x"] == 5 and el["y"] == 80 and el["w"] == 30


# ---- 死活 beat: enabled フィルタ / on-air severity 分岐 (2026-09-06) ----


def test_check_agent_liveness_skips_disabled_channel(channel):
    """退役 (enabled=False) した channel は走査対象外。

    除外しないと最後の心拍で止まった AgentStatus 行が永久に offline 判定になり、
    offline_notified が latch されたきり誰も ack できない孤児になる (ch2 の実例)。
    """
    persist_heartbeat(channel.slug, 1, 0, "healthy", False, "ok", True, False)
    AgentStatus.objects.filter(channel=channel).update(
        last_heartbeat_at=timezone.now() - timedelta(days=30)
    )
    channel.enabled = False
    channel.save(update_fields=["enabled"])

    res = check_agent_liveness()

    assert res["offline"] == 0
    assert Notification.objects.filter(kind="agent_offline").count() == 0
    assert AgentStatus.objects.get(channel=channel).offline_notified is False


def test_check_agent_liveness_off_air_is_warn(channel):
    """休止帯の断は WARN。放送中の 1 件が埋もれないようにするため。"""
    # 現在時刻を必ず外す窓を置く (窓は JST 前提。1 分幅なので now がまず入らない)。
    now_jst = timezone.localtime(timezone.now())
    dead = (now_jst + timedelta(hours=6)).strftime("%H:%M")
    dead_end = (now_jst + timedelta(hours=6, minutes=1)).strftime("%H:%M")
    channel.broadcast_windows = [{"start": dead, "end": dead_end}]
    channel.save(update_fields=["broadcast_windows"])
    assert channel.is_on_air(timezone.now()) is False

    persist_heartbeat(channel.slug, 1, 0, "healthy", False, "ok", True, False)
    AgentStatus.objects.filter(channel=channel).update(
        last_heartbeat_at=timezone.now() - timedelta(minutes=5)
    )

    res = check_agent_liveness()

    assert res["offline"] == 1
    n = Notification.objects.get(kind="agent_offline")
    assert n.severity == NotificationSeverity.WARN
    assert "休止中" in n.message


def test_offline_message_is_self_sufficient(channel):
    """Discord の 1 通で切り分けが付くよう、経過秒・状態・24h 件数を載せる。"""
    persist_heartbeat(channel.slug, 1, 0, "healthy", False, "ok", True, False)
    AgentStatus.objects.filter(channel=channel).update(
        last_heartbeat_at=timezone.now() - timedelta(minutes=5)
    )

    check_agent_liveness()

    n = Notification.objects.get(kind="agent_offline")
    # 文面が「agent が落ちた」と読めないこと (実際は経路断で agent は生きている)。
    assert "心拍途絶" not in n.message
    assert "heartbeat 未達" in n.message
    assert "放送中" in n.message  # fixture は broadcast_windows 未設定 = 常時 on-air
    assert "1 件目" in n.message


# ---- webhook severity 下限 ----


def test_webhook_skips_below_min_severity(channel, settings, monkeypatch):
    """既定 (crit) では WARN / INFO を配送しない。DB 行は残る。"""
    settings.ICSTV_NOTIFY_WEBHOOK_URL = "https://example.invalid/hook"
    sent: list[str] = []
    monkeypatch.setattr(
        notify_mod.httpx, "post", lambda url, **kw: sent.append(kw["json"]["content"])
    )

    notify_mod.WebhookBackend().send(
        Notification.objects.create(
            channel=channel, severity=NotificationSeverity.WARN, kind="k_warn", message="m"
        )
    )
    notify_mod.WebhookBackend().send(
        Notification.objects.create(
            channel=channel, severity=NotificationSeverity.INFO, kind="k_info", message="m"
        )
    )
    assert sent == []

    notify_mod.WebhookBackend().send(
        Notification.objects.create(
            channel=channel, severity=NotificationSeverity.CRIT, kind="k_crit", message="m"
        )
    )
    assert len(sent) == 1 and "[CRIT]" in sent[0]


def test_webhook_min_severity_can_be_lowered(channel, settings, monkeypatch):
    """ "warn" に下げれば休止帯の WARN も Discord へ出せる。"""
    settings.ICSTV_NOTIFY_WEBHOOK_URL = "https://example.invalid/hook"
    settings.ICSTV_NOTIFY_WEBHOOK_MIN_SEVERITY = NotificationSeverity.WARN
    sent: list[str] = []
    monkeypatch.setattr(
        notify_mod.httpx, "post", lambda url, **kw: sent.append(kw["json"]["content"])
    )

    notify_mod.WebhookBackend().send(
        Notification.objects.create(
            channel=channel, severity=NotificationSeverity.WARN, kind="k_warn", message="m"
        )
    )
    notify_mod.WebhookBackend().send(
        Notification.objects.create(
            channel=channel, severity=NotificationSeverity.INFO, kind="k_info", message="m"
        )
    )
    assert len(sent) == 1 and "[WARN]" in sent[0]


def test_webhook_delivers_unknown_severity(channel, settings, monkeypatch):
    """未知の severity は握り潰さず配送する (新 severity 追加時の無言消失を防ぐ)。

    severity カラムは varchar(4) なので、将来足すとしても 4 文字以内になる。
    """
    settings.ICSTV_NOTIFY_WEBHOOK_URL = "https://example.invalid/hook"
    sent: list[str] = []
    monkeypatch.setattr(
        notify_mod.httpx, "post", lambda url, **kw: sent.append(kw["json"]["content"])
    )

    notify_mod.WebhookBackend().send(
        Notification.objects.create(channel=channel, severity="fatl", kind="k_new", message="m")
    )
    assert len(sent) == 1
