# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""YouTube 2h枠のタイトル/キャプション自動生成 (#7 枠メタ)。

編成(Program)からその窓のタイトル+説明を合成する _compose_slot_meta、
枠境界 _windows、既存枠への再同期 resync_slot_meta を検証。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from django.utils import timezone

from youtube.tasks import (
    _channel_windows,
    _compose_slot_meta,
    _windows,
    generate_slots,
    resync_slot_meta,
)

JST = ZoneInfo("Asia/Tokyo")
TITLE_TMPL = "ICS-TV {date} {start}-{end}"
DESC_TMPL = "ICS-TV {date} {start}-{end} (JST) の配信枠です。\n\n{programs}"


def _prog(channel, title, start, *, dur_min=30, public=True):
    from scheduling.models import Program

    return Program.objects.create(
        channel=channel,
        type="live",
        title=title,
        start_at=start,
        end_at=start + timedelta(minutes=dur_min),
        live_source=_live_source(),
        public_visible=public,
    )


def _live_source():
    from core.models import LiveSource

    src, _ = LiveSource.objects.get_or_create(
        name="studio", defaults={"rtmp_app": "live", "rtmp_key": "k"}
    )
    return src


def test_compose_slot_meta_from_programs(channel, db):
    w_start = datetime(2026, 6, 20, 19, 0, tzinfo=UTC)  # = 04:00 JST 6/21
    w_end = w_start + timedelta(hours=2)
    _prog(channel, "19時のニュース", w_start)
    _prog(channel, "ドラマ△△", w_start + timedelta(minutes=30))
    title, desc = _compose_slot_meta(channel, w_start, w_end, TITLE_TMPL, DESC_TMPL)
    assert "19時のニュース" in title  # 先頭番組
    assert "ほか1件" in title  # 残り件数
    assert len(title) <= 100  # YouTube 上限
    assert "ICS-TV のテスト枠です。" not in desc  # 既定テンプレ本文
    assert "ICS-TV 2026-06-21 04:00-06:00 (JST) の配信枠です。" in desc  # テンプレ本文 (JST)
    assert "04:00 19時のニュース" in desc and "04:30 ドラマ△△" in desc  # {programs} (JST)


def test_compose_slot_meta_no_programs_still_writes_template(channel, db):
    # 番組が無くてもテンプレ本文を必ず書き込む (YouTube デフォルト説明に頼らない)。
    w_start = datetime(2026, 6, 20, 3, 0, tzinfo=UTC)  # = 12:00 JST 6/20
    w_end = w_start + timedelta(hours=2)
    title, desc = _compose_slot_meta(channel, w_start, w_end, TITLE_TMPL, DESC_TMPL)
    assert title == "ICS-TV 2026-06-20 12:00-14:00"
    assert desc == "ICS-TV 2026-06-20 12:00-14:00 (JST) の配信枠です。"  # {programs} は空→末尾strip
    assert "{programs}" not in desc


def test_compose_slot_meta_title_includes_channel_name(channel, db):
    # {channel} プレースホルダが Channel.name に展開される (マルチch で枠タイトルにch名を含める)。
    w_start = datetime(2026, 6, 20, 3, 0, tzinfo=UTC)  # = 12:00 JST
    w_end = w_start + timedelta(hours=2)
    title, desc = _compose_slot_meta(
        channel, w_start, w_end, "{channel} {date} {start}-{end}", "{channel} の配信枠"
    )
    assert title == f"{channel.name} 2026-06-20 12:00-14:00"
    assert desc == f"{channel.name} の配信枠"


def test_compose_slot_meta_excludes_non_public(channel, db):
    w_start = datetime(2026, 6, 20, 22, 0, tzinfo=UTC)  # = 07:00 JST 6/21
    w_end = w_start + timedelta(hours=2)
    _prog(channel, "非公開番組", w_start, public=False)
    title, desc = _compose_slot_meta(channel, w_start, w_end, TITLE_TMPL, DESC_TMPL)
    assert "非公開番組" not in title and "非公開番組" not in desc
    assert desc.startswith("ICS-TV 2026-06-21 07:00-09:00 (JST)")


def test_compose_slot_meta_empty_description_template(channel, db):
    # description_template が空なら説明も空 (運用者が明示的に空指定)。
    w_start = datetime(2026, 6, 20, 3, 0, tzinfo=UTC)
    w_end = w_start + timedelta(hours=2)
    _, desc = _compose_slot_meta(channel, w_start, w_end, TITLE_TMPL, "")
    assert desc == ""


def test_compose_slot_meta_title_time_is_jst(channel, db):
    # 02:00 UTC = 11:00 JST。タイトルの時刻は JST 表記でなければならない (日本向けチャンネル)。
    w_start = datetime(2026, 6, 13, 2, 0, tzinfo=UTC)
    w_end = w_start + timedelta(hours=2)
    title, _ = _compose_slot_meta(channel, w_start, w_end, TITLE_TMPL, DESC_TMPL)
    assert title == "ICS-TV 2026-06-13 11:00-13:00"


def test_compose_slot_meta_title_date_rolls_over_in_jst(channel, db):
    # 20:00 UTC 6/12 = 05:00 JST 6/13。日付も JST 基準で繰り上がる。
    w_start = datetime(2026, 6, 12, 20, 0, tzinfo=UTC)
    w_end = w_start + timedelta(hours=2)
    title, _ = _compose_slot_meta(channel, w_start, w_end, TITLE_TMPL, DESC_TMPL)
    assert title == "ICS-TV 2026-06-13 05:00-07:00"


def test_windows_align_to_jst_even_hours():
    # generate_slots は localtime(JST) を _windows に渡す。枠は JST の偶数時 (2h 境界) に揃う。
    t0 = datetime(2026, 6, 13, 11, 37, tzinfo=JST)  # = 02:37 UTC
    t1 = t0 + timedelta(hours=6)
    starts = [s for s, _ in _windows(t0, t1, timedelta(minutes=120))]
    assert starts[0] == datetime(2026, 6, 13, 10, 0, tzinfo=JST)  # 直近の 2h 境界へ後方アライン
    assert [s.hour for s in starts] == [10, 12, 14, 16]  # JST 偶数時
    assert all(s.minute == 0 for s in starts)


def test_channel_windows_splits_at_broadcast_window_not_grid(channel, db):
    # broadcast_windows 06:00-10:00 は 00:00 起点 4h グリッド (04-08/08-12) の途中に来る。
    # _channel_windows は時間帯自体を起点に刻むので、分断されず 1 本の 06:00-10:00 枠になる。
    channel.broadcast_windows = [
        {"start": "06:00", "end": "10:00"},
        {"start": "16:00", "end": "24:00"},
    ]
    channel.save(update_fields=["broadcast_windows"])
    t0 = datetime(2026, 7, 2, 13, 38, tzinfo=JST)
    t1 = t0 + timedelta(hours=24)
    windows = list(_channel_windows(channel, t0, t1, timedelta(minutes=240)))
    assert [(s.strftime("%m-%d %H:%M"), e.strftime("%H:%M")) for s, e in windows] == [
        ("07-02 16:00", "20:00"),
        ("07-02 20:00", "00:00"),
        ("07-03 06:00", "10:00"),
    ]


def test_channel_windows_unrestricted_matches_plain_windows(channel, db):
    # broadcast_windows 未設定 (24h 運用) なら従来の _windows と完全に一致する (回帰なし)。
    t0 = datetime(2026, 7, 2, 13, 38, tzinfo=JST)
    t1 = t0 + timedelta(hours=24)
    step = timedelta(minutes=240)
    assert list(_channel_windows(channel, t0, t1, step)) == list(_windows(t0, t1, step))


def test_generate_slots_does_not_split_broadcast_window_at_grid_boundary(channel, db, monkeypatch):
    # 実運用で発覚した不具合の再現: 放送時間帯 06:00-10:00 に対し固定 4h グリッドのままだと
    # 04:00-08:00 と 08:00-12:00 の 2 本に分断され、08:00 で配信が途切れてしまっていた。
    from youtube.models import YoutubeConfig, YoutubeSlot

    channel.youtube_livestream_id = "ls-1"
    channel.broadcast_windows = [
        {"start": "06:00", "end": "10:00"},
        {"start": "16:00", "end": "24:00"},
    ]
    channel.save(update_fields=["youtube_livestream_id", "broadcast_windows"])
    YoutubeConfig.objects.create(
        channel=channel,
        title_template=TITLE_TMPL,
        description_template="ICS-TV の配信枠です。\n\n{programs}",
        rolling_hours=24,
        slot_minutes=240,
    )

    def fake_insert(ch, *, title, scheduled_start, privacy, enable_monitor, description):
        return f"bc-{scheduled_start.isoformat()}"

    monkeypatch.setattr("youtube.tasks.insert_broadcast", fake_insert)
    fixed_now = datetime(2026, 7, 2, 4, 38, tzinfo=UTC)  # = 2026-07-02 13:38 JST
    monkeypatch.setattr(timezone, "now", lambda: fixed_now)

    generate_slots(channel.id)

    windows = sorted(
        (timezone.localtime(s.window_start), timezone.localtime(s.window_end))
        for s in YoutubeSlot.objects.filter(channel=channel)
    )
    assert windows == [
        (datetime(2026, 7, 2, 16, 0, tzinfo=JST), datetime(2026, 7, 2, 20, 0, tzinfo=JST)),
        (datetime(2026, 7, 2, 20, 0, tzinfo=JST), datetime(2026, 7, 3, 0, 0, tzinfo=JST)),
        (datetime(2026, 7, 3, 6, 0, tzinfo=JST), datetime(2026, 7, 3, 10, 0, tzinfo=JST)),
    ]


def test_resync_slot_meta_updates_existing_slots(channel, db, monkeypatch):
    # テンプレ変更後、未終了の既存枠へ反映。manual=True は対象外、YouTube へも update。
    from youtube.models import YoutubeConfig, YoutubeSlot, YtSlotStatus

    YoutubeConfig.objects.create(
        channel=channel,
        title_template=TITLE_TMPL,
        description_template="ICS-TV のテスト枠です。\n\n{programs}",
    )
    ws = timezone.now() + timedelta(hours=1)
    auto = YoutubeSlot.objects.create(
        channel=channel,
        window_start=ws,
        window_end=ws + timedelta(hours=2),
        broadcast_id="bc-auto",
        status=YtSlotStatus.READY,
        manual=False,
        title="old",
        description="old",
    )
    manual = YoutubeSlot.objects.create(
        channel=channel,
        window_start=ws + timedelta(hours=2),
        window_end=ws + timedelta(hours=4),
        broadcast_id="bc-manual",
        status=YtSlotStatus.READY,
        manual=True,
        title="手動",
        description="手動説明",
    )

    calls = []
    monkeypatch.setattr(
        "youtube.tasks.update_broadcast",
        lambda ch, bid, *, title, scheduled_start, description: calls.append((bid, description)),
    )

    stats = resync_slot_meta(channel.id)

    auto.refresh_from_db()
    manual.refresh_from_db()
    assert auto.description.startswith("ICS-TV のテスト枠です。")  # テンプレ反映
    assert manual.description == "手動説明"  # manual は据え置き
    assert stats["updated"] == 1 and stats["synced"] == 1
    assert [bid for bid, _ in calls] == ["bc-auto"]  # auto のみ YouTube へ


def test_generate_slots_creates_even_hour_jst_with_description(channel, db, monkeypatch):
    # 枠削除後の再作成相当: generate_slots は JST 偶数時に揃え、説明はテンプレ本文を必ず書く。
    from youtube.models import YoutubeConfig, YoutubeSlot

    channel.youtube_livestream_id = "ls-1"
    channel.save(update_fields=["youtube_livestream_id"])
    YoutubeConfig.objects.create(
        channel=channel,
        title_template=TITLE_TMPL,
        description_template="ICS-TV の配信枠です。\n\n{programs}",
        rolling_hours=6,
        slot_minutes=120,
    )

    seen = []

    def fake_insert(ch, *, title, scheduled_start, privacy, enable_monitor, description):
        seen.append((scheduled_start, description))
        return f"bc-{len(seen)}"

    monkeypatch.setattr("youtube.tasks.insert_broadcast", fake_insert)

    stats = generate_slots(channel.id)

    assert stats["created"] >= 1
    slots = list(YoutubeSlot.objects.filter(channel=channel))
    assert len(slots) == stats["created"]
    for s in slots:
        local = timezone.localtime(s.window_start)
        assert local.minute == 0 and local.hour % 2 == 0  # JST 偶数時境界
        assert s.description.startswith("ICS-TV の配信枠です。")  # テンプレ本文
    # YouTube へ渡す scheduledStartTime は +09:00 offset (= 正しい瞬間)、説明も非空
    assert all(start.utcoffset() == timedelta(hours=9) for start, _ in seen)
    assert all(desc for _, desc in seen)


def test_resync_slot_meta_include_manual(channel, db, monkeypatch):
    # --include-manual 相当: manual=True も上書きする。
    from youtube.models import YoutubeConfig, YoutubeSlot, YtSlotStatus

    YoutubeConfig.objects.create(
        channel=channel, title_template=TITLE_TMPL, description_template="本文\n\n{programs}"
    )
    ws = timezone.now() + timedelta(hours=1)
    slot = YoutubeSlot.objects.create(
        channel=channel,
        window_start=ws,
        window_end=ws + timedelta(hours=2),
        broadcast_id="bc-1",
        status=YtSlotStatus.READY,
        manual=True,
        description="手動説明",
    )
    monkeypatch.setattr(
        "youtube.tasks.update_broadcast",
        lambda *a, **k: None,
    )
    stats = resync_slot_meta(channel.id, include_manual=True)
    slot.refresh_from_db()
    assert slot.description == "本文" and stats["updated"] == 1
