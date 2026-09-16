# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""公開フロント デザイン刷新 (#7): ホーム / 番組表グリッド / now JSON / ジャンル・識別色。

ホーム/番組表/now JSON は公開 urlconf (config.urls_public) 専用ルート。テスト client の既定ホスト
"testserver" は ICSTV_ADMIN_HOSTS に含まれ管理 urlconf になるため、これらは
override_settings(ICSTV_ADMIN_HOSTS=[]) で公開 urlconf へ倒して検証する (middleware は毎回 settings 参照)。
"""

from __future__ import annotations

from datetime import timedelta

from django.test import override_settings

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])


def _make_current_program(channel, asset, *, title="いま放送中の番組", genre=""):
    from django.utils import timezone

    from scheduling.models import Program, ProgramType

    now = timezone.now()
    return Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title=title,
        genre=genre,
        start_at=now - timedelta(minutes=20),
        end_at=now + timedelta(minutes=40),
        asset=asset,
        public_visible=True,
    )


@_PUBLIC_HOST
def test_public_home_renders(http_client, channel, asset_ready):
    _make_current_program(channel, asset_ready, title="ホーム現在番組", genre="アニメ")
    # #Phase2b: ヒーロー+カードは React 島が /api/v1/home から描画。ジャンルチップは SSR。
    body = http_client.get("/").content.decode("utf-8")
    assert 'id="home-island"' in body
    assert "/static/web/home/home.js" in body
    assert "アニメ" in body  # ジャンルから探す (SSR)
    d = http_client.get("/api/v1/home").json()
    assert d["featured"]["name"] == channel.name
    assert d["cards"][0]["nowtitle"] == "ホーム現在番組"


@_PUBLIC_HOST
def test_public_home_no_channels(http_client, db):
    body = http_client.get("/").content.decode("utf-8")
    assert 'id="home-island"' in body  # 島はマウントされる (中身は API 空)
    assert http_client.get("/api/v1/home").json()["cards"] == []


@_PUBLIC_HOST
def test_public_guide_grid_renders(http_client, channel, asset_ready):
    _make_current_program(channel, asset_ready, title="番組表グリッド番組", genre="音楽")
    # #Phase2: グリッドは React 島が /api/v1/guide から描画。ページは島マウント、データは API。
    body = http_client.get("/guide/").content.decode("utf-8")
    assert 'id="guide-island"' in body
    assert "/static/web/guide/guide.js" in body
    d = http_client.get("/api/v1/guide").json()
    blocks = [b for c in d["cols"] for b in c["blocks"]]
    blk = next(b for b in blocks if b["title"] == "番組表グリッド番組")
    assert blk["is_live"] is True
    assert blk["color"] == "#1982c4"  # 音楽のジャンル色 (左ボーダー)


@_PUBLIC_HOST
def test_public_pages_dont_leak_template_comments(http_client, channel):
    # Django の {# #} は単一行専用。複数行コメントが {# ... #} のまま描画漏れする回帰を防ぐ (#Phase2)。
    for path in ["/", "/guide/", f"/ch/{channel.slug}/", "/search/", "/browse/", "/vod/"]:
        body = http_client.get(path).content.decode("utf-8")
        assert "{#" not in body, f"{path} がテンプレートコメントを描画漏れ"


@_PUBLIC_HOST
def test_public_guide_week_renders(http_client, channel, asset_ready):
    """週間番組表 (1ch × 7日)。今日の現在番組が ON AIR 付きで出る + 曜日ヘッダ + グリッド本体。"""
    _make_current_program(channel, asset_ready, title="週間グリッド番組", genre="音楽")
    res = http_client.get("/guide/week/")
    assert res.status_code == 200
    body = res.content.decode("utf-8")
    assert "週間番組表" in body
    assert channel.name in body
    assert "週間グリッド番組" in body
    assert "grid-inner" in body  # タイムライングリッド本体
    assert "ON AIR" in body  # 今日の現在番組
    assert any(wd in body for wd in ["月", "火", "水", "木", "金", "土", "日"])  # 曜日列ヘッダ
    # 縦ズーム UI + client 再計算用の素データ (生の分尺 / 既定密度) が出る。
    assert 'id="zoomMinus"' in body and 'id="zoomPlus"' in body
    assert "data-smin=" in body and "data-dmin=" in body
    assert "data-base-ppm=" in body


@_PUBLIC_HOST
def test_public_guide_week_selects_channel_param(http_client, channel, asset_ready):
    """?ch=<slug> でチャンネルを切替え、その ch の番組のみグリッドに出る (既定は先頭 ch)。"""
    from core.models import Channel

    ch2 = Channel.objects.create(name="2チャンネル", slug="ch2", enabled=True)
    _make_current_program(channel, asset_ready, title="CH1専用番組")
    _make_current_program(ch2, asset_ready, title="CH2専用番組")

    default = http_client.get("/guide/week/").content.decode("utf-8")
    assert "CH1専用番組" in default  # 既定 = slug 昇順先頭 (ch1)
    assert "CH2専用番組" not in default

    picked = http_client.get(f"/guide/week/?ch={ch2.slug}").content.decode("utf-8")
    assert "CH2専用番組" in picked
    assert "CH1専用番組" not in picked


@_PUBLIC_HOST
def test_public_guide_week_no_channels(http_client, db):
    res = http_client.get("/guide/week/")
    assert res.status_code == 200
    assert "公開中のチャンネルがありません" in res.content.decode("utf-8")


@_PUBLIC_HOST
def test_now_json_includes_design_fields(http_client, channel, asset_ready):
    _make_current_program(channel, asset_ready, title="now json 番組", genre="ニュース")
    res = http_client.get("/api/now")
    assert res.status_code == 200
    data = res.json()
    assert "live_count" in data
    assert data["cards"], "cards が空"
    card = next(c for c in data["cards"] if c["slug"] == channel.slug)
    for key in ("short", "tint", "genre", "genre_color", "cur_start_ts", "cur_end_ts"):
        assert key in card
    assert card["genre"] == "ニュース"
    assert card["genre_color"] == "#457b9d"


def test_channel_tint_short_defaults(db):
    """tint/short 未設定でも property が graceful default を返す。"""
    from core.models import Channel

    ch = Channel.objects.create(name="教育チャンネル", slug="edu", enabled=True)
    assert ch.short_name == "教育チャンネル"  # short 未設定 → name
    assert ch.tint_color.startswith("#") and len(ch.tint_color) == 7  # パレットから自動割当
    ch.short = "教育"
    ch.tint = "#1982c4"
    assert ch.short_name == "教育"
    assert ch.tint_color == "#1982c4"


def test_program_resolved_genre_falls_back_to_series(db, channel, asset_ready):
    """Program.genre 空なら series.genre にフォールバック。"""
    from scheduling.models import Program, ProgramType, Series

    s = Series.objects.create(channel=channel, title="科学シリーズ", genre="科学")
    p = Program.objects.create(
        channel=channel,
        series=s,
        type=ProgramType.RECORDED,
        title="第1回",
        start_at=channel.created_at,
        end_at=channel.created_at + timedelta(hours=1),
        asset=asset_ready,
    )
    assert p.resolved_genre == "科学"
    p.genre = "技術"
    assert p.resolved_genre == "技術"  # 番組固有が優先


def test_channel_update_saves_tint_short(staff_client, channel):
    """admin-ui のチャンネル編集で tint/short を保存できる。"""
    res = staff_client.post(
        f"/admin-ui/channels/{channel.slug}/update/",
        {"name": channel.name, "slug": channel.slug, "short": "総合", "tint": "#e63946"},
    )
    assert res.status_code == 302
    channel.refresh_from_db()
    assert channel.short == "総合"
    assert channel.tint == "#e63946"


def test_channel_update_rejects_bad_tint(staff_client, channel):
    res = staff_client.post(
        f"/admin-ui/channels/{channel.slug}/update/",
        {"name": channel.name, "slug": channel.slug, "short": "x", "tint": "red"},
    )
    assert res.status_code == 302  # エラーは messages で通知 → 一覧へ redirect
    channel.refresh_from_db()
    assert channel.tint == ""  # 不正値は保存しない
