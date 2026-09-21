# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""YouTube 次枠誘導 (nudge_next_slot)。

2h 枠は watch URL が枠ごとに変わるため、現枠の終了 nudge_lead_minutes 分前に次枠の watch URL を
ライブチャットへ投稿 + 説明欄へ追記して直接視聴者を次枠へ送り届ける。lead window / 冪等 / 次枠不在 /
無効化 / チャット未開設のフォールバックを検証する。
"""

from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

from django.utils import timezone

from youtube.models import YoutubeConfig, YoutubeSlot, YtSlotStatus
from youtube.tasks import _render_nudge, nudge_next_slot

JST = ZoneInfo("Asia/Tokyo")


def _cfg(channel, **kw):
    return YoutubeConfig.objects.create(channel=channel, **kw)


def _slot(channel, w_start, w_end, *, status, broadcast_id, **kw):
    return YoutubeSlot.objects.create(
        channel=channel,
        window_start=w_start,
        window_end=w_end,
        status=status,
        broadcast_id=broadcast_id,
        **kw,
    )


def _patch_yt(monkeypatch):
    """update_broadcast / post_live_chat_message の呼び出しを記録する。"""
    desc_calls: list[tuple] = []
    chat_calls: list[tuple] = []
    monkeypatch.setattr(
        "youtube.tasks.update_broadcast",
        lambda ch, bid, *, title, scheduled_start, description: desc_calls.append(
            (bid, description)
        ),
    )
    monkeypatch.setattr(
        "youtube.tasks.post_live_chat_message",
        lambda ch, bid, text: chat_calls.append((bid, text)) or True,
    )
    return desc_calls, chat_calls


def test_render_nudge_fills_url_and_jst():
    w_start = timezone.datetime(2026, 6, 13, 5, 0, tzinfo=JST)
    w_end = w_start + timedelta(hours=2)
    out = _render_nudge("続きは {start}-{end} ▶ {url}", "https://yt/watch?v=x", w_start, w_end)
    assert out == "続きは 05:00-07:00 ▶ https://yt/watch?v=x"


def test_nudge_posts_chat_and_prepends_description(channel, db, monkeypatch):
    _cfg(channel)  # 既定 lead=10, 既定テンプレ
    desc_calls, chat_calls = _patch_yt(monkeypatch)
    now = timezone.now()
    cur_end = now + timedelta(minutes=8)  # lead(10分) window 内
    cur = _slot(
        channel,
        cur_end - timedelta(hours=2),
        cur_end,
        status=YtSlotStatus.LIVE,
        broadcast_id="bc-cur",
        title="現枠",
        description="既存の説明",
    )
    _slot(
        channel,
        cur_end,
        cur_end + timedelta(hours=2),
        status=YtSlotStatus.READY,
        broadcast_id="bc-next",
    )

    stats = nudge_next_slot(channel.id)

    cur.refresh_from_db()
    assert stats == {"nudged": 1, "ended": 0, "skipped": 0, "errors": 0}
    assert cur.next_nudged is True
    # チャット投稿: 現枠の broadcast、本文に次枠 watch URL
    assert chat_calls == [("bc-cur", chat_calls[0][1])]
    assert "https://www.youtube.com/watch?v=bc-next" in chat_calls[0][1]
    # 説明欄: 現枠の broadcast、先頭に誘導文 + 既存説明を温存
    assert desc_calls[0][0] == "bc-cur"
    assert desc_calls[0][1].startswith("まもなくこの配信は終了します")
    assert "https://www.youtube.com/watch?v=bc-next" in desc_calls[0][1]
    assert desc_calls[0][1].endswith("既存の説明")


def test_nudge_idempotent_when_already_nudged(channel, db, monkeypatch):
    _cfg(channel)
    desc_calls, chat_calls = _patch_yt(monkeypatch)
    now = timezone.now()
    cur_end = now + timedelta(minutes=5)
    _slot(
        channel,
        cur_end - timedelta(hours=2),
        cur_end,
        status=YtSlotStatus.LIVE,
        broadcast_id="bc-cur",
        next_nudged=True,  # 既に誘導済み
    )
    _slot(
        channel,
        cur_end,
        cur_end + timedelta(hours=2),
        status=YtSlotStatus.READY,
        broadcast_id="bc-next",
    )

    stats = nudge_next_slot(channel.id)

    assert stats["nudged"] == 0
    assert chat_calls == [] and desc_calls == []  # 再投稿しない


def test_nudge_skips_outside_lead_window(channel, db, monkeypatch):
    _cfg(channel)  # lead=10
    desc_calls, chat_calls = _patch_yt(monkeypatch)
    now = timezone.now()
    cur_end = now + timedelta(minutes=30)  # まだ 10 分前ではない
    _slot(
        channel,
        cur_end - timedelta(hours=2),
        cur_end,
        status=YtSlotStatus.LIVE,
        broadcast_id="bc-cur",
    )
    _slot(
        channel,
        cur_end,
        cur_end + timedelta(hours=2),
        status=YtSlotStatus.READY,
        broadcast_id="bc-next",
    )

    stats = nudge_next_slot(channel.id)

    assert stats["nudged"] == 0 and chat_calls == [] and desc_calls == []


def test_nudge_skips_when_no_next_slot(channel, db, monkeypatch):
    _cfg(channel)
    desc_calls, chat_calls = _patch_yt(monkeypatch)
    now = timezone.now()
    cur_end = now + timedelta(minutes=7)
    cur = _slot(
        channel,
        cur_end - timedelta(hours=2),
        cur_end,
        status=YtSlotStatus.LIVE,
        broadcast_id="bc-cur",
    )
    # 次枠なし (window_start >= cur_end の枠が存在しない)

    stats = nudge_next_slot(channel.id)

    cur.refresh_from_db()
    assert stats["skipped"] == 1 and stats["nudged"] == 0
    assert cur.next_nudged is False  # 出せていないので未誘導のまま
    assert chat_calls == [] and desc_calls == []


def test_nudge_ignores_complete_or_error_next_slot(channel, db, monkeypatch):
    _cfg(channel)
    _, chat_calls = _patch_yt(monkeypatch)
    now = timezone.now()
    cur_end = now + timedelta(minutes=6)
    _slot(
        channel,
        cur_end - timedelta(hours=2),
        cur_end,
        status=YtSlotStatus.LIVE,
        broadcast_id="bc-cur",
    )
    # 直後の枠は ERROR → 誘導対象外、その先の READY 枠を選ぶ
    _slot(
        channel,
        cur_end,
        cur_end + timedelta(hours=2),
        status=YtSlotStatus.ERROR,
        broadcast_id="bc-err",
    )
    _slot(
        channel,
        cur_end + timedelta(hours=2),
        cur_end + timedelta(hours=4),
        status=YtSlotStatus.READY,
        broadcast_id="bc-far",
    )

    stats = nudge_next_slot(channel.id)

    assert stats["nudged"] == 1
    assert "watch?v=bc-far" in chat_calls[0][1]


def test_nudge_disabled_when_lead_zero(channel, db, monkeypatch):
    _cfg(channel, nudge_lead_minutes=0)
    desc_calls, chat_calls = _patch_yt(monkeypatch)
    now = timezone.now()
    cur_end = now + timedelta(minutes=3)
    _slot(
        channel,
        cur_end - timedelta(hours=2),
        cur_end,
        status=YtSlotStatus.LIVE,
        broadcast_id="bc-cur",
    )
    _slot(
        channel,
        cur_end,
        cur_end + timedelta(hours=2),
        status=YtSlotStatus.READY,
        broadcast_id="bc-next",
    )

    stats = nudge_next_slot(channel.id)

    assert stats == {"nudged": 0, "ended": 0, "skipped": 0, "errors": 0}
    assert chat_calls == [] and desc_calls == []


def test_nudge_noop_without_config(channel, db, monkeypatch):
    # youtube_config 未設定なら何もしない (誘導テンプレ/lead が無いため)。
    desc_calls, chat_calls = _patch_yt(monkeypatch)
    now = timezone.now()
    cur_end = now + timedelta(minutes=5)
    _slot(
        channel,
        cur_end - timedelta(hours=2),
        cur_end,
        status=YtSlotStatus.LIVE,
        broadcast_id="bc-cur",
    )
    _slot(
        channel,
        cur_end,
        cur_end + timedelta(hours=2),
        status=YtSlotStatus.READY,
        broadcast_id="bc-next",
    )

    stats = nudge_next_slot(channel.id)

    assert stats == {"nudged": 0, "ended": 0, "skipped": 0, "errors": 0}
    assert chat_calls == [] and desc_calls == []


def test_nudge_sets_flag_even_when_chat_unavailable(channel, db, monkeypatch):
    # ライブチャット未開設 (post が False) でも説明欄は更新し、再試行ループを避けるためフラグを立てる。
    _cfg(channel)
    desc_calls: list = []
    monkeypatch.setattr(
        "youtube.tasks.update_broadcast",
        lambda ch, bid, *, title, scheduled_start, description: desc_calls.append(
            (bid, description)
        ),
    )
    monkeypatch.setattr("youtube.tasks.post_live_chat_message", lambda ch, bid, text: False)
    now = timezone.now()
    cur_end = now + timedelta(minutes=9)
    cur = _slot(
        channel,
        cur_end - timedelta(hours=2),
        cur_end,
        status=YtSlotStatus.LIVE,
        broadcast_id="bc-cur",
    )
    _slot(
        channel,
        cur_end,
        cur_end + timedelta(hours=2),
        status=YtSlotStatus.READY,
        broadcast_id="bc-next",
    )

    stats = nudge_next_slot(channel.id)

    cur.refresh_from_db()
    assert stats["nudged"] == 1 and cur.next_nudged is True
    assert len(desc_calls) == 1  # 説明欄は更新済み


def test_nudge_retries_on_chat_http_error(channel, db, monkeypatch):
    # チャット投稿が HttpError なら flag を立てず、次 beat で再試行できるようにする。
    from googleapiclient.errors import HttpError

    _cfg(channel)
    monkeypatch.setattr(
        "youtube.tasks.update_broadcast",
        lambda *a, **k: None,
    )

    def _boom(ch, bid, text):
        raise HttpError(resp=type("R", (), {"status": 403, "reason": "quota"})(), content=b"quota")

    monkeypatch.setattr("youtube.tasks.post_live_chat_message", _boom)
    now = timezone.now()
    cur_end = now + timedelta(minutes=4)
    cur = _slot(
        channel,
        cur_end - timedelta(hours=2),
        cur_end,
        status=YtSlotStatus.LIVE,
        broadcast_id="bc-cur",
    )
    _slot(
        channel,
        cur_end,
        cur_end + timedelta(hours=2),
        status=YtSlotStatus.READY,
        broadcast_id="bc-next",
    )

    stats = nudge_next_slot(channel.id)

    cur.refresh_from_db()
    assert stats["errors"] == 1 and stats["nudged"] == 0
    assert cur.next_nudged is False  # 再試行できるよう未誘導のまま


# ---- 移動後 (complete 遷移後の説明文差し替え) ----


def _completed_nudged(channel, *, broadcast_id="bc-cur", description="既存の説明", ended=False):
    """予告済みで complete に遷移した枠 (移動後フェーズの対象) を作る。"""
    now = timezone.now()
    end = now - timedelta(minutes=5)  # 既に終了
    return _slot(
        channel,
        end - timedelta(hours=2),
        end,
        status=YtSlotStatus.COMPLETE,
        broadcast_id=broadcast_id,
        title="現枠",
        description=description,
        next_nudged=True,
        ended_nudged=ended,
    ), end


def test_ended_flips_description_after_complete(channel, db, monkeypatch):
    _cfg(channel)
    desc_calls, chat_calls = _patch_yt(monkeypatch)
    cur, end = _completed_nudged(channel)
    _slot(channel, end, end + timedelta(hours=2), status=YtSlotStatus.READY, broadcast_id="bc-next")

    stats = nudge_next_slot(channel.id)

    cur.refresh_from_db()
    assert stats["ended"] == 1 and cur.ended_nudged is True
    assert chat_calls == []  # 移動後はチャット投稿しない (complete でチャットは閉じる)
    assert desc_calls[0][0] == "bc-cur"
    assert desc_calls[0][1].startswith("この配信は終了しています")  # 「終了しています」版へ差し替え
    assert "https://www.youtube.com/watch?v=bc-next" in desc_calls[0][1]
    assert desc_calls[0][1].endswith("既存の説明")  # base 説明は温存


def test_ended_idempotent_when_already_ended(channel, db, monkeypatch):
    _cfg(channel)
    desc_calls, chat_calls = _patch_yt(monkeypatch)
    _, end = _completed_nudged(channel, ended=True)  # 既に差し替え済み
    _slot(channel, end, end + timedelta(hours=2), status=YtSlotStatus.READY, broadcast_id="bc-next")

    stats = nudge_next_slot(channel.id)

    assert stats["ended"] == 0 and desc_calls == [] and chat_calls == []


def test_ended_ignores_complete_not_nudged(channel, db, monkeypatch):
    # 予告していない (next_nudged=False) complete 枠は差し替えない。
    _cfg(channel)
    desc_calls, _ = _patch_yt(monkeypatch)
    now = timezone.now()
    end = now - timedelta(minutes=5)
    cur = _slot(
        channel,
        end - timedelta(hours=2),
        end,
        status=YtSlotStatus.COMPLETE,
        broadcast_id="bc-cur",
        next_nudged=False,
    )
    _slot(channel, end, end + timedelta(hours=2), status=YtSlotStatus.READY, broadcast_id="bc-next")

    stats = nudge_next_slot(channel.id)

    cur.refresh_from_db()
    assert stats["ended"] == 0 and desc_calls == [] and cur.ended_nudged is False


def test_ended_marks_flag_when_no_next_slot(channel, db, monkeypatch):
    # 次枠が無ければ差し替えるものが無い → 更新せず flag だけ立て再走を避ける。
    _cfg(channel)
    desc_calls, _ = _patch_yt(monkeypatch)
    cur, _ = _completed_nudged(channel)
    # 次枠なし

    stats = nudge_next_slot(channel.id)

    cur.refresh_from_db()
    assert stats["ended"] == 0 and desc_calls == []
    assert cur.ended_nudged is True  # 再走しないよう済み扱い


def test_ended_skips_when_ended_template_empty(channel, db, monkeypatch):
    # nudge_ended_template が空なら移動後の差し替えをしない (予告文のまま残す)。
    _cfg(channel, nudge_ended_template="")
    desc_calls, _ = _patch_yt(monkeypatch)
    cur, end = _completed_nudged(channel)
    _slot(channel, end, end + timedelta(hours=2), status=YtSlotStatus.READY, broadcast_id="bc-next")

    stats = nudge_next_slot(channel.id)

    cur.refresh_from_db()
    assert stats["ended"] == 0 and desc_calls == [] and cur.ended_nudged is True


def test_ended_retries_on_http_error(channel, db, monkeypatch):
    from googleapiclient.errors import HttpError

    _cfg(channel)
    monkeypatch.setattr("youtube.tasks.post_live_chat_message", lambda *a, **k: True)

    def _boom(ch, bid, *, title, scheduled_start, description):
        raise HttpError(resp=type("R", (), {"status": 500, "reason": "err"})(), content=b"err")

    monkeypatch.setattr("youtube.tasks.update_broadcast", _boom)
    cur, end = _completed_nudged(channel)
    _slot(channel, end, end + timedelta(hours=2), status=YtSlotStatus.READY, broadcast_id="bc-next")

    stats = nudge_next_slot(channel.id)

    cur.refresh_from_db()
    assert stats["errors"] == 1 and stats["ended"] == 0
    assert cur.ended_nudged is False  # 再試行できるよう未差し替えのまま
