# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""studio 管理 SPA (#Phase2d-7): 週間編成 series の admin API (一覧/スロット概観)。

staff_auth ゲート + series + 繰り返しスロット (曜日/時刻/尺/ソース)。展開/削除の操作は既存
scheduling エンドポイント (redirect 系) を SPA から form-POST 再利用するため、その業務ロジックは
既存テスト (tests/api/test_series*) の責務。ここは GET の JSON 化を検証する。
"""

from __future__ import annotations

import json
from datetime import date, time

from scheduling.models import (
    Episode,
    EpisodeStatus,
    ProgramType,
    Series,
    SeriesSlot,
)

_URL = "/api/v1/admin/scheduling/{slug}/series"
_BASE = "/api/v1/admin/scheduling/{slug}"


def _json(client, method, url, body=None):
    fn = getattr(client, method)
    if body is None:
        return fn(url)
    return fn(url, data=json.dumps(body), content_type="application/json")


def test_series_requires_auth(http_client, channel, db):
    assert http_client.get(_URL.format(slug=channel.slug)).status_code == 401


def test_series_unknown_channel_404(staff_client, db):
    assert staff_client.get(_URL.format(slug="nope")).status_code == 404


def test_series_lists_with_slots(staff_client, channel, asset_ready, db):
    s = Series.objects.create(channel=channel, title="毎週ドラマ", genre="ドラマ")
    SeriesSlot.objects.create(
        series=s,
        dow=0,  # 月
        program_type=ProgramType.RECORDED,
        start_time=time(20, 0),
        duration_ms=3_600_000,
        default_asset=asset_ready,
        effective_from=date(2020, 1, 1),
    )
    d = staff_client.get(_URL.format(slug=channel.slug)).json()
    assert d["channel"]["slug"] == channel.slug
    assert d["new_url"].endswith(f"/ch/{channel.slug}/series/")
    row = next(x for x in d["series"] if x["id"] == s.id)
    assert row["title"] == "毎週ドラマ" and row["genre"] == "ドラマ" and row["n_slots"] == 1
    assert row["is_active"] is True and row["edit_url"].endswith(f"/series/{s.id}/")
    slot = row["slots"][0]
    assert slot["start_time"] == "20:00" and slot["duration"] == "60分"
    assert slot["program_type"] == "recorded" and slot["source"] == asset_ready.title
    assert "月" in slot["recurrence"]


def test_series_empty(staff_client, channel, db):
    d = staff_client.get(_URL.format(slug=channel.slug)).json()
    assert d["series"] == [] and any(c["slug"] == channel.slug for c in d["channels"])


# --------------------------------------------------------------------------- #
#  series 作成/編集フォーム (旧画面の SPA 化)                                     #
# --------------------------------------------------------------------------- #


def test_series_form_requires_auth(http_client, channel, db):
    assert http_client.get(f"{_BASE.format(slug=channel.slug)}/series-form").status_code == 401


def test_series_form_new_has_choices(staff_client, channel, db):
    d = staff_client.get(f"{_BASE.format(slug=channel.slug)}/series-form").json()
    assert d["initial"]["is_active"] is True and d["initial"].get("id") is None
    assert {c["value"] for c in d["rating_choices"]} == {"pg12", "r15", "r18"}
    assert any(c["value"] == "アニメ" for c in d["genre_choices"])
    assert d["thumbnail_post_url"] == "" and d["delete_url"] == ""


def test_series_form_edit_populates(staff_client, channel, db):
    s = Series.objects.create(channel=channel, title="朝番組", slug="morning", rating="r15")
    d = staff_client.get(f"{_BASE.format(slug=channel.slug)}/series-form?series_id={s.id}").json()
    assert d["initial"]["id"] == s.id and d["initial"]["slug"] == "morning"
    assert d["initial"]["rating"] == "r15"
    assert d["thumbnail_post_url"].endswith(f"/series/{s.id}/thumbnail")
    assert d["delete_url"].endswith(f"/series/{s.id}/delete/")


def test_series_full_create(staff_client, channel, db):
    body = {
        "title": "新番組",
        "slug": "new-show",
        "genre": "アニメ",
        "rating": "r15",
        "description": "あらすじ",
        "cast": "出演者",
        "x_handle": "icsTV",
        "x_hashtag": "ICS_TV",
        "is_active": True,
        "youtube_dedicated": False,
    }
    r = _json(staff_client, "post", f"{_BASE.format(slug=channel.slug)}/series-full", body)
    assert r.status_code == 200
    s = Series.objects.get(pk=r.json()["id"])
    assert s.slug == "new-show" and s.rating == "r15" and s.genre == "アニメ"
    assert s.channel_id == channel.id and s.x_handle == "icsTV"


def test_series_create_rejects_digit_slug_422(staff_client, channel, db):
    body = {"title": "x", "slug": "999"}
    r = _json(staff_client, "post", f"{_BASE.format(slug=channel.slug)}/series-full", body)
    assert r.status_code == 422


def test_series_update(staff_client, channel, db):
    s = Series.objects.create(channel=channel, title="旧題", rating="")
    body = {"title": "新題", "rating": "r18", "is_active": False}
    r = _json(staff_client, "post", f"{_BASE.format(slug=channel.slug)}/series/{s.id}", body)
    assert r.status_code == 200
    s.refresh_from_db()
    assert s.title == "新題" and s.rating == "r18" and s.is_active is False


# --------------------------------------------------------------------------- #
#  スロット CRUD + 展開プレビュー                                                #
# --------------------------------------------------------------------------- #


def _slot_body(**over):
    body = {
        "dow": 0,
        "start_time": "20:00",
        "duration_min": 60,
        "program_type": "recorded",
        "effective_from": "2020-01-01",
        "recurrence_kind": "weekly",
    }
    body.update(over)
    return body


def test_slot_create_recorded_requires_asset_422(staff_client, channel, db):
    s = Series.objects.create(channel=channel, title="s")
    r = _json(
        staff_client, "post", f"{_BASE.format(slug=channel.slug)}/series/{s.id}/slots", _slot_body()
    )
    assert r.status_code == 422  # 録画は default_asset 必須


def test_slot_create_live_requires_source_422(staff_client, channel, db):
    s = Series.objects.create(channel=channel, title="s")
    body = _slot_body(program_type="live")
    r = _json(staff_client, "post", f"{_BASE.format(slug=channel.slug)}/series/{s.id}/slots", body)
    assert r.status_code == 422  # 生は live_source 必須


def test_slot_create_recorded_ok(staff_client, channel, asset_ready, db):
    s = Series.objects.create(channel=channel, title="s")
    body = _slot_body(default_asset_id=asset_ready.id)
    r = _json(staff_client, "post", f"{_BASE.format(slug=channel.slug)}/series/{s.id}/slots", body)
    assert r.status_code == 200
    sl = SeriesSlot.objects.get(series=s)
    assert sl.dow == 0 and sl.duration_ms == 3_600_000 and sl.default_asset_id == asset_ready.id


def test_slot_create_monthly_nth_dow_csv(staff_client, channel, asset_ready, db):
    s = Series.objects.create(channel=channel, title="s")
    body = _slot_body(
        default_asset_id=asset_ready.id, recurrence_kind="monthly_nth_dow", weeks_csv="2,4"
    )
    r = _json(staff_client, "post", f"{_BASE.format(slug=channel.slug)}/series/{s.id}/slots", body)
    assert r.status_code == 200
    sl = SeriesSlot.objects.get(series=s)
    assert sl.recurrence_kind == "monthly_nth_dow" and sl.recurrence_param == {"weeks": [2, 4]}


def test_slot_update_and_delete(staff_client, channel, asset_ready, db):
    s = Series.objects.create(channel=channel, title="s")
    sl = SeriesSlot.objects.create(
        series=s,
        dow=0,
        program_type=ProgramType.RECORDED,
        start_time=time(20, 0),
        duration_ms=3_600_000,
        default_asset=asset_ready,
        effective_from=date(2020, 1, 1),
    )
    body = _slot_body(default_asset_id=asset_ready.id, dow=2, duration_min=30)
    r = _json(staff_client, "post", f"{_BASE.format(slug=channel.slug)}/slots/{sl.id}", body)
    assert r.status_code == 200
    sl.refresh_from_db()
    assert sl.dow == 2 and sl.duration_ms == 1_800_000
    r = staff_client.delete(f"{_BASE.format(slug=channel.slug)}/slots/{sl.id}")
    assert r.status_code == 200 and not SeriesSlot.objects.filter(pk=sl.id).exists()


def test_slot_preview_weekly_returns_mondays(staff_client, channel, asset_ready, db):
    s = Series.objects.create(channel=channel, title="s")
    body = _slot_body(default_asset_id=asset_ready.id, dow=0)  # 月曜
    r = _json(
        staff_client,
        "post",
        f"{_BASE.format(slug=channel.slug)}/series/{s.id}/slots/preview",
        body,
    )
    assert r.status_code == 200
    dates = r.json()
    assert len(dates) > 0
    assert all(date.fromisoformat(d).weekday() == 0 for d in dates)


# --------------------------------------------------------------------------- #
#  サムネ アップロード                                                           #
# --------------------------------------------------------------------------- #

# store() は content_type/サイズのみ検査し画像バイトは解析しない (r2 はモック) ため中身は任意。
_PNG = b"\x89PNG\r\n\x1a\n test image bytes"


def test_thumbnail_upload(staff_client, channel, monkeypatch, db):
    from django.core.files.uploadedfile import SimpleUploadedFile

    from core import r2

    monkeypatch.setattr(r2, "put_object", lambda *a, **k: None)
    s = Series.objects.create(channel=channel, title="s")
    f = SimpleUploadedFile("t.png", _PNG, content_type="image/png")
    r = staff_client.post(
        f"{_BASE.format(slug=channel.slug)}/series/{s.id}/thumbnail", data={"file": f}
    )
    assert r.status_code == 200
    url = r.json()["thumbnail_url"]
    assert url.startswith("/t/thumbnails/")
    s.refresh_from_db()
    assert s.thumbnail_url == url


def test_thumbnail_rejects_non_image_400(staff_client, channel, monkeypatch, db):
    from django.core.files.uploadedfile import SimpleUploadedFile

    from core import r2

    monkeypatch.setattr(r2, "put_object", lambda *a, **k: None)
    s = Series.objects.create(channel=channel, title="s")
    f = SimpleUploadedFile("t.txt", b"hello", content_type="text/plain")
    r = staff_client.post(
        f"{_BASE.format(slug=channel.slug)}/series/{s.id}/thumbnail", data={"file": f}
    )
    assert r.status_code == 400


# --------------------------------------------------------------------------- #
#  回 (Episode) パネル                                                          #
# --------------------------------------------------------------------------- #


def test_episodes_list(staff_client, channel, db):
    s = Series.objects.create(channel=channel, title="s")
    Episode.objects.create(series=s, episode_no=1, title="第1回", status=EpisodeStatus.PLANNED)
    d = staff_client.get(f"{_BASE.format(slug=channel.slug)}/series/{s.id}/episodes").json()
    assert len(d) == 1 and d[0]["episode_no"] == 1 and d[0]["status_label"] == "予定"
    assert d[0]["asset_id"] is None


def test_episode_create_and_update(staff_client, channel, db):
    s = Series.objects.create(channel=channel, title="s")
    body = {"episode_no": 2, "air_date": "2026-07-01", "title": "第2回", "status": "planned"}
    r = _json(
        staff_client, "post", f"{_BASE.format(slug=channel.slug)}/series/{s.id}/episodes", body
    )
    assert r.status_code == 200
    ep_id = r.json()["id"]
    r = _json(
        staff_client,
        "post",
        f"{_BASE.format(slug=channel.slug)}/episodes/{ep_id}",
        {"episode_no": 2, "air_date": "2026-07-01", "title": "第2回(改)", "status": "aired"},
    )
    assert r.status_code == 200
    ep = Episode.objects.get(pk=ep_id)
    assert ep.title == "第2回(改)" and ep.status == "aired" and str(ep.air_date) == "2026-07-01"


def test_episode_duplicate_no_409(staff_client, channel, db):
    s = Series.objects.create(channel=channel, title="s")
    Episode.objects.create(series=s, episode_no=1)
    body = {"episode_no": 1, "status": "planned"}
    r = _json(
        staff_client, "post", f"{_BASE.format(slug=channel.slug)}/series/{s.id}/episodes", body
    )
    assert r.status_code == 409


def test_episode_delete_planned_ok_confirmed_guarded(staff_client, channel, asset_ready, db):
    s = Series.objects.create(channel=channel, title="s")
    planned = Episode.objects.create(series=s, episode_no=1, status=EpisodeStatus.PLANNED)
    confirmed = Episode.objects.create(
        series=s, episode_no=2, asset=asset_ready, status=EpisodeStatus.CONFIRMED
    )
    r = staff_client.delete(f"{_BASE.format(slug=channel.slug)}/episodes/{planned.id}")
    assert r.status_code == 200 and not Episode.objects.filter(pk=planned.id).exists()
    r = staff_client.delete(f"{_BASE.format(slug=channel.slug)}/episodes/{confirmed.id}")
    assert r.status_code == 409 and Episode.objects.filter(pk=confirmed.id).exists()
