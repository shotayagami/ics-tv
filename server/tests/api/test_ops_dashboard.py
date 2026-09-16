# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""運行ダッシュボード + 即時操作 API のテスト (#7 O-B)。staff 認可 + 即時 INSERT を検証。"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from playout.models import PlayoutAction, PlayoutEvent, PlayoutStatus


def _url(slug: str, suffix: str) -> str:
    return f"/ops/ch/{slug}/{suffix}"


# ---- 認可 ----


def test_dashboard_requires_staff(http_client, channel):
    res = http_client.get(_url(channel.slug, "dashboard/"))
    assert res.status_code == 302  # staff_member_required → admin login へ


def test_dashboard_renders_for_staff(staff_client, channel):
    res = staff_client.get(_url(channel.slug, "dashboard/"))
    assert res.status_code == 200
    body = res.content.decode("utf-8")
    assert "運行ダッシュボード" in body
    assert channel.name in body


def test_panels_render_for_staff(staff_client, channel):
    for suffix, marker in [
        ("panel/now/", "NOW PLAYING"),
        ("panel/health/", "ヘルス"),
        ("panel/asrun/", "AS-RUN"),
    ]:
        res = staff_client.get(_url(channel.slug, suffix))
        assert res.status_code == 200, suffix
        assert marker in res.content.decode("utf-8"), suffix


def test_op_requires_staff(http_client, channel):
    res = http_client.post(_url(channel.slug, "clear-slate/"))
    assert res.status_code == 302


# ---- 即時操作 ----


def test_clear_slate_inserts_event(staff_client, channel):
    res = staff_client.post(_url(channel.slug, "clear-slate/"))
    assert res.status_code == 200
    ev = PlayoutEvent.objects.get(channel=channel, action=PlayoutAction.CLEAR_SLATE)
    assert ev.status == PlayoutStatus.SCHEDULED
    assert ev.params.get("interrupt") is True


def test_cut_live_return_reuses_last_live_and_clears_slate(staff_client, channel):
    # 直近の cut_live (load 設定) を用意
    PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=timezone.now() - timedelta(minutes=5),
        action=PlayoutAction.CUT_LIVE,
        params={"rtmp_url": "rtmp://127.0.0.1:1935/live/ch1"},
        status=PlayoutStatus.DONE,
    )
    res = staff_client.post(_url(channel.slug, "cut-live-return/"))
    assert res.status_code == 200
    # 新しい cut_live (interrupt) が直近設定を引き継ぐ
    new_live = (
        PlayoutEvent.objects.filter(channel=channel, action=PlayoutAction.CUT_LIVE)
        .order_by("-scheduled_at")
        .first()
    )
    assert new_live.params.get("interrupt") is True
    assert new_live.params.get("rtmp_url") == "rtmp://127.0.0.1:1935/live/ch1"
    # clear_slate も同時に入る
    assert PlayoutEvent.objects.filter(channel=channel, action=PlayoutAction.CLEAR_SLATE).exists()


def test_reload_main_412_without_on_air(staff_client, channel):
    res = staff_client.post(_url(channel.slug, "reload-main/"))
    assert res.status_code == 412


def test_reload_main_corrects_in_ms_for_play_asset(staff_client, channel):
    PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=timezone.now() - timedelta(seconds=30),
        action=PlayoutAction.PLAY_ASSET,
        params={"clip": "asset/1", "in_ms": "0"},
        status=PlayoutStatus.DONE,
    )
    res = staff_client.post(_url(channel.slug, "reload-main/"))
    assert res.status_code == 200
    reloaded = (
        PlayoutEvent.objects.filter(channel=channel, action=PlayoutAction.PLAY_ASSET)
        .order_by("-scheduled_at")
        .first()
    )
    # 経過 ~30s 分を頭出し補正 (無補正だと 0 のまま巻き戻る)
    assert int(reloaded.params["in_ms"]) >= 29_000
    assert reloaded.params.get("interrupt") is True


def test_emergency_slate_still_inserts(staff_client, channel):
    """helper 切り出し後も緊急スレートが play_slate を INSERT すること (退行防止)。"""
    res = staff_client.post(_url(channel.slug, "slate/"))
    assert res.status_code in (200, 302)
    assert PlayoutEvent.objects.filter(channel=channel, action=PlayoutAction.PLAY_SLATE).exists()


# ---- 自動復帰トグル (#7 O-B Commit2) ----


def _agent_status(channel, **kw):
    from playout.models import AgentStatus

    defaults = {"last_heartbeat_at": timezone.now()}
    defaults.update(kw)
    return AgentStatus.objects.create(channel=channel, **defaults)


def test_auto_return_toggle_412_without_status(staff_client, channel):
    res = staff_client.post(_url(channel.slug, "auto-return/"), {"auto_return": "off"})
    assert res.status_code == 412


def test_auto_return_toggle_off(staff_client, channel):
    _agent_status(channel, auto_return=True)
    res = staff_client.post(_url(channel.slug, "auto-return/"), {"auto_return": "off"})
    assert res.status_code == 200
    channel.agent_status.refresh_from_db()
    assert channel.agent_status.auto_return is False


def test_auto_return_toggle_on_clears_suspended(staff_client, channel):
    _agent_status(channel, auto_return=False, auto_return_suspended=True)
    res = staff_client.post(_url(channel.slug, "auto-return/"), {"auto_return": "on"})
    assert res.status_code == 200
    st = channel.agent_status
    st.refresh_from_db()
    assert st.auto_return is True
    assert st.auto_return_suspended is False


def test_heartbeat_preserves_operator_auto_return(channel, db):
    """operator が OFF にした auto_return を、agent 報告 (True) の heartbeat が上書きしないこと。"""
    from playout.grpc_service import persist_heartbeat

    _agent_status(channel, auto_return=False)
    persist_heartbeat(
        channel.slug,
        last_received_seq=5,
        queue_depth=0,
        caspar_health="healthy",
        slate_active=False,
        feed_state="ok",
        auto_return=True,  # agent はまだ ON を報告 (AgentControl 未到達)
        auto_return_suspended=False,
    )
    channel.agent_status.refresh_from_db()
    assert channel.agent_status.auto_return is False  # operator intent を維持
    assert channel.agent_status.last_received_seq == 5  # 他は反映


def test_heartbeat_seeds_auto_return_on_first_contact(channel, db):
    """agent_status 未生成時 (初回 heartbeat) は agent 報告値を採用する。"""
    from playout.grpc_service import persist_heartbeat
    from playout.models import AgentStatus

    persist_heartbeat(
        channel.slug,
        last_received_seq=1,
        queue_depth=0,
        caspar_health="healthy",
        slate_active=False,
        feed_state="ok",
        auto_return=False,
        auto_return_suspended=False,
    )
    assert AgentStatus.objects.get(channel=channel).auto_return is False


# ---- CM IN / CM 戻り (#7 O-B Commit3) ----


def _cm_bundle(name="bundleA", n=2, dur=15_000):
    from medialib.models import (
        Asset,
        AssetKind,
        CmBundle,
        CmBundleItem,
        CmCreative,
        CmGrid,
        NormalizeStatus,
    )

    bundle = CmBundle.objects.create(name=name)
    for i in range(n):
        a = Asset.objects.create(
            kind=AssetKind.CM,
            title=f"cm{i}",
            duration_ms=dur,
            r2_key=f"mezzanine/cm/{name}-{i}.mp4",
            normalize_status=NormalizeStatus.READY,
        )
        cr = CmCreative.objects.create(asset=a, advertiser="adv", grid=CmGrid.G15)
        CmBundleItem.objects.create(cm_bundle=bundle, seq=i, cm_asset=cr)
    return bundle


def test_cm_in_400_without_bundle_id(staff_client, channel):
    res = staff_client.post(_url(channel.slug, "cm-in/"))
    assert res.status_code == 400


def test_cm_in_inserts_bundle_with_clips_and_return_rtmp(staff_client, channel):
    PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=timezone.now() - timedelta(minutes=2),
        action=PlayoutAction.CUT_LIVE,
        params={"rtmp_url": "rtmp://127.0.0.1:1935/live/ch1"},
        status=PlayoutStatus.DONE,
    )
    bundle = _cm_bundle(n=2, dur=15_000)
    res = staff_client.post(_url(channel.slug, "cm-in/"), {"bundle_id": bundle.id})
    assert res.status_code == 200
    ev = PlayoutEvent.objects.get(channel=channel, action=PlayoutAction.PLAY_CM_BUNDLE)
    assert ev.params["interrupt"] is True
    # clips = "cm/<asset_id>:15000,cm/<asset_id>:15000"
    assert ev.params["clips"].count(",") == 1
    assert ":15000" in ev.params["clips"]
    # 戻り先 rtmp が直近 cut_live から引かれている (agent が reel 末尾に積む)
    assert ev.params["return_rtmp_url"] == "rtmp://127.0.0.1:1935/live/ch1"
    assert ev.cm_bundle_id == bundle.id


def test_cm_in_412_for_empty_bundle(staff_client, channel):
    from medialib.models import CmBundle

    empty = CmBundle.objects.create(name="empty")
    res = staff_client.post(_url(channel.slug, "cm-in/"), {"bundle_id": empty.id})
    assert res.status_code == 412


def test_cm_return_inserts_cut_live(staff_client, channel):
    PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=timezone.now() - timedelta(minutes=2),
        action=PlayoutAction.CUT_LIVE,
        params={"rtmp_url": "rtmp://127.0.0.1:1935/live/ch1"},
        status=PlayoutStatus.DONE,
    )
    res = staff_client.post(_url(channel.slug, "cm-return/"))
    assert res.status_code == 200
    new = (
        PlayoutEvent.objects.filter(channel=channel, action=PlayoutAction.CUT_LIVE)
        .order_by("-scheduled_at")
        .first()
    )
    assert new.params.get("interrupt") is True
    assert new.params.get("rtmp_url") == "rtmp://127.0.0.1:1935/live/ch1"
    assert "return_rtmp_url" not in new.params


# ---- 通知 3 層 (#7 O-B Commit4) ----


def _notification(channel=None, kind="feed_lost", message="feed 断", acked=False):
    from core.models import Notification, NotificationSeverity

    n = Notification.objects.create(
        channel=channel,
        severity=NotificationSeverity.CRIT,
        kind=kind,
        message=message,
    )
    if acked:
        n.acknowledged_at = timezone.now()
        n.save(update_fields=["acknowledged_at"])
    return n


def test_notifications_panel_shows_unacked(staff_client, channel):
    _notification(channel=channel, message="feed 断発生")
    _notification(channel=None, message="全体宛アラート")  # 全体宛も拾う
    res = staff_client.get(_url(channel.slug, "panel/notifications/"))
    assert res.status_code == 200
    body = res.content.decode("utf-8")
    assert "feed 断発生" in body
    assert "全体宛アラート" in body
    assert "未確認 2" in body


def test_notification_center_renders(staff_client, channel):
    _notification(channel=channel, message="履歴項目", acked=True)
    res = staff_client.get(_url(channel.slug, "notifications/"))
    assert res.status_code == 200
    assert "通知センター" in res.content.decode("utf-8")


def test_notifications_panel_requires_staff(http_client, channel):
    res = http_client.get(_url(channel.slug, "panel/notifications/"))
    assert res.status_code == 302


def test_ack_notification_marks_read(staff_client, channel, staff_user):
    n = _notification(channel=channel)
    res = staff_client.post(_url(channel.slug, f"notifications/{n.id}/ack/"))
    assert res.status_code == 200
    n.refresh_from_db()
    assert n.acknowledged_at is not None
    assert n.acknowledged_by_id == staff_user.id


def test_ack_notification_other_channel_404(staff_client, channel, db):
    from core.models import Channel

    other = Channel.objects.create(name="ch2", slug="ch2", enabled=True)
    n = _notification(channel=other)
    res = staff_client.post(_url(channel.slug, f"notifications/{n.id}/ack/"))
    assert res.status_code == 404


def test_ack_all_notifications(staff_client, channel):
    _notification(channel=channel, message="a")
    _notification(channel=None, message="b")
    res = staff_client.post(_url(channel.slug, "notifications/ack-all/"))
    assert res.status_code == 200
    from core.models import Notification

    assert not Notification.objects.filter(acknowledged_at__isnull=True).exists()
