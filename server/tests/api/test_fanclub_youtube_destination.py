# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""クリエイター個人 YouTube チャンネル宛シミュルキャストの窓制御 (#27 Part B)。

docs/fanclub.md §5.5・§6.5: SlotContract.youtube_destination=creator_channel を実体化する
fanclub.tasks.reconcile_creator_youtube_outputs の挙動と CreatorYoutubeOutput の制約を検証する。
実 Cloudflare API は core.cloudflare_api の関数を monkeypatch して隔離する。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.db import IntegrityError
from django.utils import timezone

from fanclub.models import (
    Creator,
    CreatorSeriesLink,
    CreatorYoutubeOutput,
    SlotContract,
    SlotContractStatus,
    SlotDestination,
)
from fanclub.tasks import reconcile_creator_youtube_outputs
from scheduling.models import ExposurePolicy, Program, Series

_STREAM_KEY = "creator-own-key"  # pragma: allowlist secret - test only


def _creator(slug="circle-a", **kw):
    kw.setdefault("youtube_destination_stream_key", _STREAM_KEY)
    return Creator.objects.create(name="サークルA", slug=slug, **kw)


def _series(channel, creator, title="番組A"):
    s = Series.objects.create(channel=channel, title=title)
    CreatorSeriesLink.objects.create(series=s, creator=creator)
    return s


def _contract(creator, *, destination=SlotDestination.CREATOR_CHANNEL, **kw):
    kw.setdefault("status", SlotContractStatus.ACTIVE)
    kw.setdefault("starts_on", timezone.localdate() - timedelta(days=1))
    return SlotContract.objects.create(
        creator=creator,
        title="レギュラー枠",
        monthly_fee_minor=100_000,
        youtube_destination=destination,
        **kw,
    )


def _program(channel, series, asset, *, start, end, exposure_policy="", fc_required_level=None):
    return Program.objects.create(
        channel=channel,
        series=series,
        type="recorded",
        title="回",
        asset=asset,
        start_at=start,
        end_at=end,
        exposure_policy=exposure_policy,
        fc_required_level=fc_required_level,
    )


def _second_channel():
    from core.models import Channel

    return Channel.objects.create(
        name="ICS-TV 2ch",
        slug="ch2",
        enabled=True,
        agent_token="test-agent-token-def456",
        cf_live_input_id="live-input-2",
    )


def test_reconcile_beat_schedule_bounds_the_gate_lag():
    """ビート周期がそのまま「ゲート対象へ切り替わってから中継が止まるまでの最大遅延」になる。

    番組境界の到来は DB 書き込みを伴わずシグナルで捕捉できないため、この周期を短く保つことが
    唯一の直接的な手段 (docs/fanclub.md §9)。expires は CF API が詰まったときに待機中の tick が
    積み上がらないための上限で、周期に対して過大にならないこと。
    """
    from django.conf import settings

    entry = settings.CELERY_BEAT_SCHEDULE["fanclub-reconcile-creator-youtube-outputs"]
    assert entry["task"] == "fanclub.tasks.reconcile_creator_youtube_outputs"
    assert entry["schedule"] <= 15
    assert 0 < entry["options"]["expires"] <= entry["schedule"] * 3


def test_creator_youtube_output_unique_creator_channel(db, channel):
    c = _creator()
    CreatorYoutubeOutput.objects.create(creator=c, channel=channel, cf_output_uid="out-1")
    with pytest.raises(IntegrityError):
        CreatorYoutubeOutput.objects.create(creator=c, channel=channel, cf_output_uid="out-2")


def test_reconcile_creates_and_enables_output_when_on_air(db, channel, monkeypatch, asset_ready):
    channel.cf_live_input_id = "live-input-1"
    channel.save(update_fields=["cf_live_input_id"])
    c = _creator()
    series = _series(channel, c)
    _contract(c)
    now = timezone.now()
    _program(
        channel,
        series,
        asset_ready,
        start=now - timedelta(minutes=5),
        end=now + timedelta(minutes=5),
    )

    calls = {"create": [], "update": []}
    monkeypatch.setattr(
        "fanclub.tasks.cloudflare_api.create_live_output",
        lambda ch, *, target_url, stream_key: (
            calls["create"].append((ch.id, target_url, stream_key)) or {"uid": "new-out"}
        ),
    )
    monkeypatch.setattr(
        "fanclub.tasks.cloudflare_api.update_live_output",
        lambda ch, uid, *, enabled: calls["update"].append((ch.id, uid, enabled)),
    )

    stats = reconcile_creator_youtube_outputs()

    assert stats == {"enabled": 1, "disabled": 0, "created": 1, "errors": 0, "skipped": 0}
    assert calls["create"] == [(channel.id, c.youtube_destination_ingest_url, _STREAM_KEY)]
    assert calls["update"] == []
    output = CreatorYoutubeOutput.objects.get(creator=c, channel=channel)
    assert output.cf_output_uid == "new-out"
    assert output.enabled is True


def test_reconcile_disables_output_when_off_air(db, channel, monkeypatch, asset_ready):
    c = _creator()
    series = _series(channel, c)
    _contract(c)
    now = timezone.now()
    # 番組はもう終わっている (on-air ではない)
    _program(
        channel, series, asset_ready, start=now - timedelta(hours=2), end=now - timedelta(hours=1)
    )
    CreatorYoutubeOutput.objects.create(
        creator=c, channel=channel, cf_output_uid="existing-out", enabled=True
    )

    calls = []
    monkeypatch.setattr(
        "fanclub.tasks.cloudflare_api.update_live_output",
        lambda ch, uid, *, enabled: calls.append((ch.id, uid, enabled)),
    )

    stats = reconcile_creator_youtube_outputs()

    assert stats == {"enabled": 0, "disabled": 1, "created": 0, "errors": 0, "skipped": 0}
    assert calls == [(channel.id, "existing-out", False)]
    output = CreatorYoutubeOutput.objects.get(creator=c, channel=channel)
    assert output.enabled is False


def test_reconcile_is_noop_when_state_already_matches(db, channel, monkeypatch, asset_ready):
    c = _creator()
    series = _series(channel, c)
    _contract(c)
    now = timezone.now()
    _program(
        channel,
        series,
        asset_ready,
        start=now - timedelta(minutes=5),
        end=now + timedelta(minutes=5),
    )
    CreatorYoutubeOutput.objects.create(
        creator=c, channel=channel, cf_output_uid="existing-out", enabled=True
    )

    monkeypatch.setattr(
        "fanclub.tasks.cloudflare_api.update_live_output",
        lambda *a, **kw: pytest.fail("update_live_output should not be called when state matches"),
    )
    monkeypatch.setattr(
        "fanclub.tasks.cloudflare_api.create_live_output",
        lambda *a, **kw: pytest.fail("create_live_output should not be called (output exists)"),
    )

    stats = reconcile_creator_youtube_outputs()
    assert stats == {"enabled": 0, "disabled": 0, "created": 0, "errors": 0, "skipped": 0}


def test_reconcile_skips_creator_without_stream_key(db, channel, monkeypatch, asset_ready):
    c = _creator(youtube_destination_stream_key="")
    series = _series(channel, c)
    _contract(c)
    now = timezone.now()
    _program(
        channel,
        series,
        asset_ready,
        start=now - timedelta(minutes=5),
        end=now + timedelta(minutes=5),
    )

    monkeypatch.setattr(
        "fanclub.tasks.cloudflare_api.create_live_output",
        lambda *a, **kw: pytest.fail("must not call CF API without a stream key"),
    )
    stats = reconcile_creator_youtube_outputs()
    assert stats == {"enabled": 0, "disabled": 0, "created": 0, "errors": 0, "skipped": 0}
    assert not CreatorYoutubeOutput.objects.filter(creator=c).exists()


def test_reconcile_skips_contract_with_operator_destination(db, channel, monkeypatch, asset_ready):
    c = _creator()
    series = _series(channel, c)
    _contract(c, destination=SlotDestination.OPERATOR_CHANNEL)
    now = timezone.now()
    _program(
        channel,
        series,
        asset_ready,
        start=now - timedelta(minutes=5),
        end=now + timedelta(minutes=5),
    )

    monkeypatch.setattr(
        "fanclub.tasks.cloudflare_api.create_live_output",
        lambda *a, **kw: pytest.fail("operator_channel destination must not simulcast"),
    )
    stats = reconcile_creator_youtube_outputs()
    assert stats["created"] == 0


def test_reconcile_skips_ended_contract(db, channel, monkeypatch, asset_ready):
    c = _creator()
    series = _series(channel, c)
    _contract(
        c,
        starts_on=timezone.localdate() - timedelta(days=30),
        ends_on=timezone.localdate() - timedelta(days=1),
    )
    now = timezone.now()
    _program(
        channel,
        series,
        asset_ready,
        start=now - timedelta(minutes=5),
        end=now + timedelta(minutes=5),
    )

    monkeypatch.setattr(
        "fanclub.tasks.cloudflare_api.create_live_output",
        lambda *a, **kw: pytest.fail("expired contract must not simulcast"),
    )
    stats = reconcile_creator_youtube_outputs()
    assert stats["created"] == 0


def test_reconcile_guards_non_public_exposure_policy(db, channel, monkeypatch, asset_ready):
    """#27: 公開以外の exposure_policy (site_members 等) は防御的にシミュルキャスト対象から外す。"""
    c = _creator()
    series = _series(channel, c)
    _contract(c)
    now = timezone.now()
    _program(
        channel,
        series,
        asset_ready,
        start=now - timedelta(minutes=5),
        end=now + timedelta(minutes=5),
        exposure_policy=ExposurePolicy.SITE_MEMBERS,
    )

    monkeypatch.setattr(
        "fanclub.tasks.cloudflare_api.create_live_output",
        lambda *a, **kw: pytest.fail("site_members content must not simulcast publicly"),
    )
    stats = reconcile_creator_youtube_outputs()
    assert stats["created"] == 0


def test_reconcile_guards_fc_required_level(db, channel, monkeypatch, asset_ready):
    """#27 Phase B: ファンクラブ ティア限定の窓もシミュルキャスト対象から外す。"""
    c = _creator()
    series = _series(channel, c)
    _contract(c)
    now = timezone.now()
    _program(
        channel,
        series,
        asset_ready,
        start=now - timedelta(minutes=5),
        end=now + timedelta(minutes=5),
        fc_required_level=0,
    )

    monkeypatch.setattr(
        "fanclub.tasks.cloudflare_api.create_live_output",
        lambda *a, **kw: pytest.fail("fanclub-gated content must not simulcast publicly"),
    )
    stats = reconcile_creator_youtube_outputs()
    assert stats["created"] == 0
    assert not CreatorYoutubeOutput.objects.filter(creator=c).exists()


def test_reconcile_keeps_other_channel_when_one_channel_is_fc_gated(
    db, channel, monkeypatch, asset_ready
):
    """ゲート対象の窓は「その channel を候補から外す」だけで、他 channel を巻き添えにしない。

    docs/fanclub.md §9 の既知の限界 (creator 横断の .first() が1件だけ見ていたため、ゲート対象を
    引き当てると無関係な完全公開番組の中継まで止まっていた) の回帰テスト。
    """
    channel2 = _second_channel()
    c = _creator()
    gated_series = _series(channel, c, title="FC限定番組")
    public_series = _series(channel2, c, title="公開番組")
    _contract(c)
    now = timezone.now()
    # ゲート対象の方を先に始めて (start_at, channel_id) 順の先頭に置く
    _program(
        channel,
        gated_series,
        asset_ready,
        start=now - timedelta(minutes=10),
        end=now + timedelta(minutes=10),
        fc_required_level=1,
    )
    _program(
        channel2,
        public_series,
        asset_ready,
        start=now - timedelta(minutes=5),
        end=now + timedelta(minutes=5),
    )
    CreatorYoutubeOutput.objects.create(
        creator=c, channel=channel, cf_output_uid="out-ch1", enabled=True
    )
    CreatorYoutubeOutput.objects.create(
        creator=c, channel=channel2, cf_output_uid="out-ch2", enabled=False
    )

    calls = []
    monkeypatch.setattr(
        "fanclub.tasks.cloudflare_api.update_live_output",
        lambda ch, uid, *, enabled: calls.append((uid, enabled)),
    )

    stats = reconcile_creator_youtube_outputs()

    assert stats == {"enabled": 1, "disabled": 1, "created": 0, "errors": 0, "skipped": 0}
    assert sorted(calls) == [("out-ch1", False), ("out-ch2", True)]
    assert CreatorYoutubeOutput.objects.get(creator=c, channel=channel).enabled is False
    assert CreatorYoutubeOutput.objects.get(creator=c, channel=channel2).enabled is True


def test_reconcile_keeps_currently_enabled_channel_when_multiple_are_on_air(
    db, channel, monkeypatch, asset_ready
):
    """宛先ストリームキーは creator 単位で1本なので、同時 on-air でも有効化するのは高々1 channel。

    いま有効な channel が候補に残っている限りそこへ留まる (番組境界ごとに宛先が入れ替わって
    YouTube 側の配信が切れるのを避ける)。
    """
    channel2 = _second_channel()
    c = _creator()
    series1 = _series(channel, c, title="番組1")
    series2 = _series(channel2, c, title="番組2")
    _contract(c)
    now = timezone.now()
    _program(
        channel,
        series1,
        asset_ready,
        start=now - timedelta(minutes=10),
        end=now + timedelta(minutes=10),
    )
    _program(
        channel2,
        series2,
        asset_ready,
        start=now - timedelta(minutes=5),
        end=now + timedelta(minutes=5),
    )
    # 先に始まったのは ch1 だが、いま有効なのは ch2
    CreatorYoutubeOutput.objects.create(
        creator=c, channel=channel, cf_output_uid="out-ch1", enabled=False
    )
    CreatorYoutubeOutput.objects.create(
        creator=c, channel=channel2, cf_output_uid="out-ch2", enabled=True
    )

    monkeypatch.setattr(
        "fanclub.tasks.cloudflare_api.update_live_output",
        lambda *a, **kw: pytest.fail("must not switch destination while the current one is on air"),
    )

    stats = reconcile_creator_youtube_outputs()

    assert stats == {"enabled": 0, "disabled": 0, "created": 0, "errors": 0, "skipped": 0}
    assert CreatorYoutubeOutput.objects.get(creator=c, channel=channel2).enabled is True


def test_reconcile_picks_earliest_program_when_nothing_is_enabled(
    db, channel, monkeypatch, asset_ready
):
    """有効な Output が無いときの選択は (start_at, channel_id) 順で決定的 (DB の行順序に依存しない)。"""
    channel2 = _second_channel()
    c = _creator()
    series1 = _series(channel, c, title="番組1")
    series2 = _series(channel2, c, title="番組2")
    _contract(c)
    now = timezone.now()
    _program(
        channel,
        series1,
        asset_ready,
        start=now - timedelta(minutes=10),
        end=now + timedelta(minutes=10),
    )
    _program(
        channel2,
        series2,
        asset_ready,
        start=now - timedelta(minutes=5),
        end=now + timedelta(minutes=5),
    )
    CreatorYoutubeOutput.objects.create(
        creator=c, channel=channel, cf_output_uid="out-ch1", enabled=False
    )
    CreatorYoutubeOutput.objects.create(
        creator=c, channel=channel2, cf_output_uid="out-ch2", enabled=False
    )

    calls = []
    monkeypatch.setattr(
        "fanclub.tasks.cloudflare_api.update_live_output",
        lambda ch, uid, *, enabled: calls.append((uid, enabled)),
    )

    stats = reconcile_creator_youtube_outputs()

    assert stats == {"enabled": 1, "disabled": 0, "created": 0, "errors": 0, "skipped": 0}
    assert calls == [("out-ch1", True)]


def test_reconcile_marks_error_and_keeps_state_on_cf_failure(db, channel, monkeypatch, asset_ready):
    c = _creator()
    series = _series(channel, c)
    _contract(c)
    now = timezone.now()
    _program(
        channel,
        series,
        asset_ready,
        start=now - timedelta(minutes=5),
        end=now + timedelta(minutes=5),
    )
    output = CreatorYoutubeOutput.objects.create(
        creator=c, channel=channel, cf_output_uid="existing-out", enabled=False
    )

    import httpx

    monkeypatch.setattr(
        "fanclub.tasks.cloudflare_api.update_live_output",
        lambda ch, uid, *, enabled: (_ for _ in ()).throw(httpx.ConnectError("boom")),
    )
    stats = reconcile_creator_youtube_outputs()
    assert stats == {"enabled": 0, "disabled": 0, "created": 0, "errors": 1, "skipped": 0}
    output.refresh_from_db()
    assert output.enabled is False  # 更新失敗時はローカル state を書き換えない (次周期で再試行)
