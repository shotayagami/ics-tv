# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""公開プレイヤー島 (#Phase1) の E2E (Playwright headless chromium)。

Django(public_base シェル) + React 島 (/static/web/player) の統合を実ブラウザで検証する:
島マウント / API 駆動の描画(プレイヤー scaffold・タブ・本日の番組・コメント欄) /
未ログインのコメントゲート / 確認済み会員のコメント投稿→表示 (CSRF cookie 経路込み)。

前提: フロントを build して server/frontend_dist へ配置済み (live_server の StaticFilesHandler が
/static/web/player/player.js を finder から配信する)。実映像のデコードは bundled chromium に
H.264/AAC が無いため検証しない (UI/データ層のみ)。WS リアルタイムは WSGI live_server では
張れないが、投稿は楽観追加で即表示されるため本テストは WS 非依存。

実行 (dev):
  cd frontend && npm run build
  rm -rf ../server/frontend_dist \
    && cp -r apps/web/dist ../server/frontend_dist \
    && cp -r apps/player/dist ../server/frontend_dist/player
  cd ../server && pytest -m e2e tests/e2e/test_player_island.py
"""

from __future__ import annotations

import pytest
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]

# 会員ログイン (/members/) は公開ホスト限定 (urls_public)。live_server の localhost を
# 公開ホスト扱いにして /members/ + /ch/ + /api/v1/ を public urlconf で解決させる。
_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])

_PW = "Tv9!kd83mfar"  # pragma: allowlist secret - test only


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args):
    # autoplay を gesture 無しで許可 (島の自動再生経路を踏ませる)。
    return {**browser_type_launch_args, "args": ["--autoplay-policy=no-user-gesture-required"]}


@pytest.fixture
def player_demo(db):
    from core.models import Channel

    call_command("seed_demo", force=True)  # テストは DEBUG=False。#sec L-12 ガードを force で通す
    ch = Channel.objects.get(slug="demo1")
    # 実在 URL 不要: 島が <video>+コントロールを描画することの確認 (デコードは検証しない)。
    ch.cf_playback_hls_url = "https://example.invalid/hls/master.m3u8"
    ch.save(update_fields=["cf_playback_hls_url"])
    return ch


def _make_member(email="e2e@example.com", nickname="E2E視聴者"):
    from members.models import Member

    m = Member(
        email=email,
        nickname=nickname,
        birth_year=1990,
        birth_month=4,
        postal_code="1000001",
        email_verified_at=timezone.now(),
    )
    m.set_password(_PW)
    m.save()
    return m


def test_island_mounts_and_renders(live_server, page, player_demo):
    page.goto(f"{live_server.url}/public/ch/demo1/")
    # 島がマウントしプレイヤー scaffold (video + コントロール) を描画する
    page.wait_for_selector("#player-island .player video", timeout=15000)
    assert page.locator("#player-island .ctrl button[aria-label='全画面']").count() == 1
    # タイムシフト (#PLAYER-03): シークバー + ライブ復帰ボタンを描画する
    assert page.locator("#player-island .ctrl input.seek").count() == 1
    assert page.locator("#player-island .ctrl button[aria-label='ライブに戻る']").count() == 1
    # API 駆動: チャンネルタブ + 本日の番組 + コメント欄
    assert page.locator("#player-island .tabs a.tab").count() >= 1
    assert "本日の番組" in page.content()
    assert page.locator("#player-island #comments").count() == 1


def test_anon_sees_comment_login_gate(live_server, page, player_demo):
    page.goto(f"{live_server.url}/public/ch/demo1/")
    gate = page.locator("#player-island .comment-gate")
    gate.wait_for(timeout=10000)
    assert "ログイン" in gate.inner_text()


def test_anon_sees_exposure_policy_login_gate_instead_of_video(live_server, page, db):
    """exposure_policy=site_members の現在番組は hls_url を隠し、<video> でなく導線を描画する

    (#27、docs/site-only-broadcast.md §4.7: プレイヤー UI を隠すだけでなく hls_url 自体を
    非会員へ渡さないことの回帰確認)。player_demo (demo1) は seed_demo が現在時刻近辺に既存番組を
    作るため program_no_overlap_per_channel と衝突しうる → 専用チャンネルを新規作成して隔離する。
    """
    from datetime import timedelta

    from core.models import Channel
    from medialib.models import Asset, AssetKind, NormalizeStatus
    from scheduling.models import Program

    channel = Channel.objects.create(
        name="E2E配信ポリシーch", slug="e2e-exposure-gate", enabled=True
    )
    channel.cf_playback_hls_url = "https://example.invalid/hls/master.m3u8"
    channel.save(update_fields=["cf_playback_hls_url"])
    asset = Asset.objects.create(
        kind=AssetKind.PROGRAM,
        title="ep-gated",
        duration_ms=3_600_000,
        r2_key="mezzanine/program/ep-gated.mp4",
        normalize_status=NormalizeStatus.READY,
        width=1920,
        height=1080,
        fps=60.0,
        vcodec="h264",
        acodec="aac",
    )
    now = timezone.now()
    Program.objects.create(
        channel=channel,
        title="サイト会員限定番組",
        type="recorded",
        asset=asset,
        start_at=now - timedelta(minutes=10),
        end_at=now + timedelta(minutes=20),
        public_visible=True,
        exposure_policy="site_members",
    )

    page.goto(f"{live_server.url}/public/ch/{channel.slug}/")
    # 初回は PlayerPage の「読み込み中…」placeholder が先に出るため、データ反映後の
    # ゲート文言が出るまで明示的に待つ (同じ .placeholder セレクタが2状態で再利用される)。
    page.wait_for_selector(
        "#player-island .player .placeholder:has-text('ログインして視聴')", timeout=15000
    )
    assert page.locator("#player-island .player video").count() == 0


def test_anon_sees_login_gate_for_fanclub_tier_program(live_server, page, db):
    """fc_required_level が設定された現在番組は、未ログインには従来と同じ 'login' 導線を出し

    (サイト会員限定と語彙上共有、#27 Phase B)、hls_url は隠す (video を描画しない)。
    """
    from datetime import timedelta

    from core.models import Channel
    from fanclub import services as fc_services
    from fanclub.models import Creator, CreatorSeriesLink
    from medialib.models import Asset, AssetKind, NormalizeStatus
    from scheduling.models import Program, Series

    channel = Channel.objects.create(name="E2E FCゲートch", slug="e2e-fc-gate", enabled=True)
    channel.cf_playback_hls_url = "https://example.invalid/hls/master.m3u8"
    channel.save(update_fields=["cf_playback_hls_url"])
    creator = Creator.objects.create(name="E2Eサークル", slug="e2e-fc-gate-circle")
    fc_services.ensure_free_tier(creator)
    series = Series.objects.create(channel=channel, title="FC限定番組シリーズ")
    CreatorSeriesLink.objects.create(series=series, creator=creator)
    asset = Asset.objects.create(
        kind=AssetKind.PROGRAM,
        title="ep-fc-gated",
        duration_ms=3_600_000,
        r2_key="mezzanine/program/ep-fc-gated.mp4",
        normalize_status=NormalizeStatus.READY,
        width=1920,
        height=1080,
        fps=60.0,
        vcodec="h264",
        acodec="aac",
    )
    now = timezone.now()
    Program.objects.create(
        channel=channel,
        series=series,
        title="ファンクラブ限定番組",
        type="recorded",
        asset=asset,
        start_at=now - timedelta(minutes=10),
        end_at=now + timedelta(minutes=20),
        public_visible=True,
        fc_required_level=0,
    )

    page.goto(f"{live_server.url}/public/ch/{channel.slug}/")
    page.wait_for_selector(
        "#player-island .player .placeholder:has-text('ログインして視聴')", timeout=15000
    )
    assert page.locator("#player-island .player video").count() == 0


@_PUBLIC_HOST
def test_logged_in_non_member_sees_fanclub_join_gate_instead_of_video(live_server, page, db):
    """ログイン済みだがその創作者のファンクラブに未加入の会員には 'fc_join' 導線を出す

    (#27 Phase B の新設分岐。'login'/'subscribe' しか無かったサイト会員限定の語彙に、
    ファンクラブ ティア軸を追加したことの回帰確認。「共有 Live Input がゲートされていない問題」
    の回帰確認も兼ねる: 過去は home/poster がこのゲートを素通りしていた)。
    """
    from datetime import timedelta

    from core.models import Channel
    from fanclub import services as fc_services
    from fanclub.models import Creator, CreatorSeriesLink
    from medialib.models import Asset, AssetKind, NormalizeStatus
    from scheduling.models import Program, Series

    channel = Channel.objects.create(
        name="E2E FC加入ゲートch", slug="e2e-fc-join-gate", enabled=True
    )
    channel.cf_playback_hls_url = "https://example.invalid/hls/master.m3u8"
    channel.save(update_fields=["cf_playback_hls_url"])
    creator = Creator.objects.create(name="E2Eサークル2", slug="e2e-fc-join-gate-circle")
    fc_services.ensure_free_tier(creator)
    series = Series.objects.create(channel=channel, title="FC加入限定番組シリーズ")
    CreatorSeriesLink.objects.create(series=series, creator=creator)
    asset = Asset.objects.create(
        kind=AssetKind.PROGRAM,
        title="ep-fc-join-gated",
        duration_ms=3_600_000,
        r2_key="mezzanine/program/ep-fc-join-gated.mp4",
        normalize_status=NormalizeStatus.READY,
        width=1920,
        height=1080,
        fps=60.0,
        vcodec="h264",
        acodec="aac",
    )
    now = timezone.now()
    Program.objects.create(
        channel=channel,
        series=series,
        title="ファンクラブ加入限定番組",
        type="recorded",
        asset=asset,
        start_at=now - timedelta(minutes=10),
        end_at=now + timedelta(minutes=20),
        public_visible=True,
        fc_required_level=0,
    )
    _make_member()  # このクリエイターのファンクラブには未加入のまま

    page.goto(f"{live_server.url}/members/login/")
    page.fill("input[name=email]", "e2e@example.com")
    page.fill("input[name=password]", _PW)
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")

    page.goto(f"{live_server.url}/public/ch/{channel.slug}/")
    page.wait_for_selector(
        "#player-island .player .placeholder:has-text('ファンクラブ限定')", timeout=15000
    )
    assert page.locator("#player-island .player video").count() == 0
    assert page.locator("#player-island .player .placeholder a").count() == 1


@_PUBLIC_HOST
def test_verified_member_posts_comment(live_server, page, player_demo):
    _make_member()
    # ブラウザでログイン (session cookie をコンテキストに保持)。会員系は公開ホストのみ。
    page.goto(f"{live_server.url}/members/login/")
    page.fill("input[name=email]", "e2e@example.com")
    page.fill("input[name=password]", _PW)
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")
    # プレイヤーへ → コメント投稿 (ninja API + csrftoken cookie + 楽観追加で即表示)
    page.goto(f"{live_server.url}/ch/demo1/")
    page.wait_for_selector("#player-island .comment-form textarea", timeout=10000)
    page.fill("#player-island .comment-form textarea", "E2Eからこんにちは")
    with page.expect_response(
        lambda r: r.url.endswith("/comments") and r.request.method == "POST"
    ) as resp_info:
        page.click("#player-island .comment-form button[type=submit]")
    resp = resp_info.value
    assert resp.status == 200, f"POST {resp.status}: {resp.text()}"
    # 楽観追加でコメントが一覧に出る
    page.wait_for_selector("#player-island .comments-list .c-body", timeout=10000)

    from members.models import Comment

    assert Comment.objects.filter(body="E2Eからこんにちは").exists()
