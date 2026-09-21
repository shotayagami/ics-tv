# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""studio 管理 SPA (#Phase2d) の E2E (Playwright headless chromium)。

staff ログイン → SPA シェルマウント → React Router のクライアントルーティング (リロード無し) →
admin API (/api/v1/admin/*) からの描画を検証する。実行手順は test_staff_flows.py / 島 e2e に準ずる
(frontend build → server/frontend_dist 配置 → pytest -m e2e)。
"""

from __future__ import annotations

import pytest
from django.core.management import call_command

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]


@pytest.fixture
def demo(db):
    call_command("seed_demo", force=True)  # テストは DEBUG=False。#sec L-12 ガードを force で通す


def _login(page, base_url: str) -> None:
    page.goto(f"{base_url}/admin/login/")
    page.fill("input[name=username]", "demo")
    page.fill("input[name=password]", "demo12345")
    page.click("input[type=submit]")
    page.wait_for_url("**/admin/")


def test_studio_spa_routes_and_renders(live_server, page, demo):
    _login(page, live_server.url)
    page.goto(f"{live_server.url}/studio/")
    # シェル (左サイドバー) + ダッシュボード
    page.wait_for_selector("#studio-root .st-sidebar", timeout=15000)
    assert "ICS-TV studio" in page.content()
    # クライアントルーティングで会員統計へ (フルリロードなし) → admin API から KPI 描画。
    # ダッシュボードのランチャにも同名リンクがあるため、サイドバーのナビに限定する。
    page.click("#studio-root .st-sidebar nav a:has-text('会員統計')")
    page.wait_for_url("**/studio/members/stats")
    page.wait_for_selector("#studio-root .st-kpi", timeout=8000)
    assert "会員数" in page.content()
    # 権利ダッシュボードへ → 期限切れ間近の配信権テーブル
    page.click("#studio-root .st-sidebar nav a:has-text('権利')")
    page.wait_for_url("**/studio/rights")
    # 直前の会員統計画面にも table があるため、権利画面に固有で API 取得成功後にだけ
    # 描かれる h2 (RightsDashboard.tsx) を待つ。h1 は取得前から出るので使わない。
    page.wait_for_selector("#studio-root h2:has-text('期限切れ間近の配信権')", timeout=8000)
    assert "期限切れ間近の配信権" in page.content()


def test_studio_requires_staff_login(live_server, page, db):
    # 未ログインで /studio/ → admin login へ redirect (staff_member_required)。
    page.goto(f"{live_server.url}/studio/")
    page.wait_for_url("**/admin/login/**", timeout=10000)
    assert "#studio-root" not in page.content() or "st-sidebar" not in page.content()


def test_studio_timeline_renders_and_drag_moves(live_server, page, demo, asset_ready):
    # #Phase2d-3 本丸: グリッド描画 + ドラッグ移動 (既存 move エンドポイント再利用)。
    # 専用 ch に1番組だけ置き、重複なしでドラッグ移動が成功することを確認。
    from datetime import timedelta

    from django.utils import timezone

    from core.models import Channel
    from scheduling.models import Program, ProgramType

    ch = Channel.objects.create(name="E2E編成ch", slug="e2e-sched", enabled=True)
    now = timezone.now()
    Program.objects.create(
        channel=ch,
        type=ProgramType.RECORDED,
        title="E2E編成番組",
        start_at=now + timedelta(minutes=30),
        end_at=now + timedelta(minutes=90),
        asset=asset_ready,
        public_visible=True,
    )
    _login(page, live_server.url)
    page.goto(f"{live_server.url}/studio/scheduling/{ch.slug}")
    page.wait_for_selector("#studio-root >> text=E2E編成番組", timeout=15000)
    assert "編成 タイムライン" in page.content()
    # ブロックを下へドラッグ → move エンドポイント → 成功メッセージ
    box = page.locator('#studio-root div[title*="E2E編成番組"]').first.bounding_box()
    cx = box["x"] + box["width"] / 2
    cy = box["y"] + box["height"] / 2
    page.mouse.move(cx, cy)
    page.mouse.down()
    page.mouse.move(cx, cy + 50, steps=6)
    page.mouse.up()
    page.wait_for_selector("#studio-root >> text=移動しました", timeout=8000)


def test_studio_timeline_create_drag(live_server, page, demo):
    # #Phase2d-4: 空き領域ドラッグ → 既存の作成フォームへ prefill (start/end) 付きで遷移。
    from core.models import Channel

    ch = Channel.objects.create(name="E2E空ch", slug="e2e-empty", enabled=True)
    _login(page, live_server.url)
    page.goto(f"{live_server.url}/studio/scheduling/{ch.slug}")
    track = page.locator("#studio-root [data-track]")
    track.wait_for(timeout=15000)
    box = track.bounding_box()
    x = box["x"] + box["width"] / 2
    y0 = box["y"] + 120
    page.mouse.move(x, y0)
    page.mouse.down()
    page.mouse.move(x, y0 + 90, steps=6)
    page.mouse.up()
    # 空き領域ドラッグ → SPA の番組作成フォーム (/program-form/:slug) へ start/end prefill 付きで遷移。
    page.wait_for_url("**/program-form/**", timeout=8000)
    assert "start=" in page.url and "end=" in page.url


def test_studio_timeline_add_cm_break(live_server, page, demo, asset_ready):
    # #Phase2d-4: 録画番組に CM+ で CM枠を追加 (既存 adbreak_create を再利用) → 成功メッセージ。
    from datetime import timedelta

    from django.utils import timezone

    from core.models import Channel
    from scheduling.models import Program, ProgramType

    ch = Channel.objects.create(name="E2E録画ch", slug="e2e-rec", enabled=True)
    now = timezone.now()
    Program.objects.create(
        channel=ch,
        type=ProgramType.RECORDED,
        title="E2E録画番組",
        start_at=now + timedelta(minutes=30),
        end_at=now + timedelta(minutes=90),
        asset=asset_ready,
        public_visible=True,
    )
    _login(page, live_server.url)
    page.goto(f"{live_server.url}/studio/scheduling/{ch.slug}")
    page.wait_for_selector("#studio-root >> text=E2E録画番組", timeout=15000)
    page.click("#studio-root button:has-text('CM+')")
    page.wait_for_selector("#studio-root >> text=CM枠を追加しました", timeout=8000)


def test_studio_channels_edit(live_server, page, demo):
    # #Phase2d-5: チャンネル名を編集 → 保存(POST) → 成功メッセージ。
    _login(page, live_server.url)
    page.goto(f"{live_server.url}/studio/channels")
    page.wait_for_selector("#studio-root tbody tr", timeout=15000)
    row = page.locator("#studio-root tbody tr").first
    row.locator("input").first.fill("E2E編集ch名")
    row.locator("button:has-text('保存')").click()
    page.wait_for_selector("#studio-root >> text=を保存しました", timeout=8000)


def test_studio_channel_settings_media(live_server, page, demo):
    # #Phase2e-4: チャンネル詳細設定 → 既定フィラーを割当 (既存 set_channel_media 再利用) → 反映。
    from core.models import Channel
    from medialib.models import FillerPlaylist

    ch = Channel.objects.order_by("slug").first()
    fp = FillerPlaylist.objects.create(name="E2E局ID")
    _login(page, live_server.url)
    page.goto(f"{live_server.url}/studio/channels/{ch.slug}/settings")
    page.wait_for_selector("#studio-root >> text=詳細設定", timeout=15000)
    card = page.locator("#studio-root .card").filter(has_text="既定メディア")
    card.locator("select").first.select_option(label="E2E局ID")
    card.locator("button:has-text('保存')").click()
    page.wait_for_selector("#studio-root >> text=既定メディアを保存しました", timeout=8000)
    ch.refresh_from_db()
    assert ch.default_filler_id == fp.id


def test_studio_channel_settings_exposure_policy_fillers(live_server, page, demo):
    """#27 exposure_policy: 公開ミラー案内フィラー/メンバーミラー待機画の割当が保存される。

    候補は非番組/非CM・正規化済 (slate_options と同一クエリ) なので slate 種別の Asset を使う。
    """
    from core.models import Channel
    from medialib.models import Asset, AssetKind, NormalizeStatus

    ch = Channel.objects.order_by("slug").first()
    guide = Asset.objects.create(
        kind=AssetKind.SLATE,
        title="e2e-guide",
        r2_key="slate/e2e-guide.mp4",
        normalize_status=NormalizeStatus.READY,
    )
    _login(page, live_server.url)
    page.goto(f"{live_server.url}/studio/channels/{ch.slug}/settings")
    page.wait_for_selector("#studio-root >> text=詳細設定", timeout=15000)
    card = page.locator("#studio-root .card").filter(has_text="既定メディア")
    card.locator("select").nth(2).select_option(label="e2e-guide")  # 公開ミラー案内フィラー
    card.locator("select").nth(3).select_option(label="e2e-guide")  # メンバーミラー待機画
    card.locator("button:has-text('保存')").click()
    page.wait_for_selector("#studio-root >> text=既定メディアを保存しました", timeout=8000)
    ch.refresh_from_db()
    assert ch.site_only_filler_id == guide.id
    assert ch.members_filler_id == guide.id


def test_studio_medialib_screening(live_server, page, demo):
    # #Phase2d-6: CM の考査OK (既存 screening エンドポイント再利用) → 成功メッセージ。
    from medialib.models import (
        Asset,
        AssetKind,
        CmCreative,
        CmGrid,
        NormalizeStatus,
        ScreeningStatus,
    )

    a = Asset.objects.create(
        kind=AssetKind.CM,
        title="E2E_CM素材",
        duration_ms=15000,
        r2_key="cm/e.mp4",
        normalize_status=NormalizeStatus.READY,
    )
    CmCreative.objects.create(
        asset=a, advertiser="E2E広告主", grid=CmGrid.G15, screening_status=ScreeningStatus.PENDING
    )
    _login(page, live_server.url)
    page.goto(f"{live_server.url}/studio/medialib")
    page.wait_for_selector("#studio-root >> text=E2E広告主", timeout=15000)
    page.locator("#studio-root tr", has_text="E2E広告主").locator("button:has-text('OK')").click()
    page.wait_for_selector("#studio-root >> text=考査OKにしました", timeout=8000)


def test_studio_series_expand(live_server, page, demo):
    # #Phase2d-7: 週間編成ページの展開(4週) → 既存 series_expand を再利用 → 成功メッセージ。
    _login(page, live_server.url)
    page.goto(f"{live_server.url}/studio/series")
    page.wait_for_selector("#studio-root >> text=週間編成", timeout=15000)
    page.click("#studio-root button:has-text('展開')")
    page.wait_for_selector("#studio-root >> text=展開しました", timeout=8000)


def test_studio_series_exposure_policy_default(live_server, page, demo):
    """#27: シリーズ編集の配信ポリシー既定セレクタで保存でき、会員限定(YT+サイト)選択時は GPU 警告が出る。"""
    from core.models import Channel
    from scheduling.models import Series

    ch = Channel.objects.order_by("slug").first()
    s = Series.objects.create(channel=ch, title="E2Eシリーズ配信ポリシー")
    _login(page, live_server.url)
    page.goto(f"{live_server.url}/studio/series/{ch.slug}/edit/{s.id}")
    page.wait_for_selector("#studio-root form", timeout=15000)
    page.locator("#studio-root form select").filter(has_text="会員限定(YT+サイト)").select_option(
        label="会員限定(YT+サイト)"
    )
    page.wait_for_selector("#studio-root >> text=専用 GPU が実質必須", timeout=5000)
    page.click("#studio-root form button:has-text('保存')")
    page.wait_for_selector("#studio-root >> text=保存しました", timeout=8000)
    s.refresh_from_db()
    assert s.exposure_policy_default == "members_yt_site"


# 放送当直 (運用 ops) は別ホスト ops.* の 🔴放送コンソールへ分離 (リファクタ Phase 1.4)。
# studio からは外れたため旧 test_studio_ops_ack_notification は削除 (ops.* 側の e2e は別途)。


def test_studio_sales_refill(live_server, page, demo):
    # #Phase2d-9: CM割付ページ → 自動補充 (既存 refill を再利用) → 成功メッセージ。
    _login(page, live_server.url)
    page.goto(f"{live_server.url}/studio/sales")
    page.wait_for_selector("#studio-root >> text=CM 割付", timeout=15000)
    page.click("#studio-root button:has-text('自動補充')")
    page.wait_for_selector("#studio-root >> text=再充填しました", timeout=8000)


def test_studio_sales_swap(live_server, page, demo):
    # #Phase2e-3: 未送出 CM枠 の ↻ ドロップダウンで CM を入替 (既存 op_swap_item 再利用) → 反映。
    from datetime import timedelta

    from django.utils import timezone

    from core.models import Channel
    from medialib.models import (
        Asset,
        AssetKind,
        CmCreative,
        CmGrid,
        NormalizeStatus,
        ScreeningStatus,
    )
    from scheduling.models import AdBreak, AdBreakItem, Program, ProgramType

    # 既存番組と重ならない専用チャンネルに番組+CM枠を用意 (PlayoutEvent 無 → 入替可)。
    ch = Channel.objects.create(name="入替E2E", slug="swap-e2e")
    now = timezone.now()
    prog_asset = Asset.objects.create(
        kind=AssetKind.PROGRAM,
        title="本編素材",
        duration_ms=3_600_000,
        r2_key="prog/swap.mp4",
        normalize_status=NormalizeStatus.READY,
    )
    prog = Program.objects.create(
        channel=ch,
        type=ProgramType.RECORDED,
        title="E2E入替番組",
        start_at=now,
        end_at=now + timedelta(hours=1),
        asset=prog_asset,
    )
    br = AdBreak.objects.create(
        program=prog, offset_ms=600_000, grid=CmGrid.G15, duration_ms=15_000
    )

    def mkcm(adv: str) -> CmCreative:
        a = Asset.objects.create(
            kind=AssetKind.CM,
            title=f"CM {adv}",
            duration_ms=15_000,
            normalize_status=NormalizeStatus.READY,
        )
        return CmCreative.objects.create(
            asset=a, advertiser=adv, grid=CmGrid.G15, screening_status=ScreeningStatus.APPROVED
        )

    old, new = mkcm("旧広告主"), mkcm("新広告主")
    item = AdBreakItem.objects.create(ad_break=br, seq=0, cm_asset=old)

    _login(page, live_server.url)
    page.goto(f"{live_server.url}/studio/sales?channel={ch.id}")
    page.wait_for_selector("#studio-root >> text=E2E入替番組", timeout=15000)
    sec = page.locator("#studio-root section").filter(has_text="E2E入替番組")
    sec.locator("select").first.select_option(label="新広告主")
    page.wait_for_selector("#studio-root >> text=CM を入替えました", timeout=8000)
    item.refresh_from_db()
    assert item.cm_asset_id == new.asset_id


def test_studio_cuesheet_add_content(live_server, page, demo):
    # #Phase2d-10 本丸: キューシートに本編を追加 (既存 cuepoint_add 再利用) → 成功メッセージ。
    from medialib.models import Asset, AssetKind, NormalizeStatus

    a = Asset.objects.create(
        kind=AssetKind.PROGRAM,
        title="E2E本編素材",
        duration_ms=3_600_000,
        r2_key="prog/e.mp4",
        normalize_status=NormalizeStatus.READY,
    )
    _login(page, live_server.url)
    page.goto(f"{live_server.url}/studio/cuesheet/{a.id}")
    page.wait_for_selector("#studio-root >> text=キューシート", timeout=15000)
    form = page.locator("#studio-root form").filter(has_text="本編を追加")
    form.locator("input[name=min]").fill("10")
    form.locator("button:has-text('＋本編')").click()
    page.wait_for_selector("#studio-root >> text=本編を追加しました", timeout=8000)


def test_studio_slots_render(live_server, page, demo):
    # #Phase2d-12: YouTube スロット dashboard が描画され、スロットが一覧に出る (メタ更新等の操作は
    # 実 YouTube API を叩くため e2e では検証せず・API テスト/既存 youtube テストの責務)。
    from datetime import timedelta

    from django.utils import timezone

    from core.models import Channel
    from youtube.models import YoutubeSlot, YtSlotStatus

    ch = Channel.objects.filter(enabled=True).order_by("slug").first()
    now = timezone.now()
    YoutubeSlot.objects.create(
        channel=ch,
        window_start=now,
        window_end=now + timedelta(hours=2),
        broadcast_id="e2e-yt",
        status=YtSlotStatus.READY,
        title="E2Eスロット",
    )
    _login(page, live_server.url)
    page.goto(f"{live_server.url}/studio/slots/{ch.slug}")
    page.wait_for_selector("#studio-root >> text=YouTube スロット", timeout=15000)
    assert "e2e-yt" in page.content()


def test_studio_graphic_cues_add(live_server, page, demo):
    # #Phase2e-1: series の自動グラフィックにテキストキューを追加 (既存 graphic_cue_add 再利用)。
    from core.models import Channel
    from scheduling.models import Series

    ch = Channel.objects.filter(enabled=True).order_by("slug").first()
    s = Series.objects.create(channel=ch, title="E2E_CGシリーズ")
    _login(page, live_server.url)
    page.goto(f"{live_server.url}/studio/graphic-cues/{ch.slug}/series/{s.id}")
    page.wait_for_selector("#studio-root >> text=自動グラフィック", timeout=15000)
    page.fill("#studio-root input[name=text]", "E2Eテロップ")
    page.click("#studio-root button:has-text('追加')")
    page.wait_for_selector("#studio-root >> text=キューを追加しました", timeout=8000)


def test_studio_program_form_create(live_server, page, demo, asset_ready):
    # #Phase2e-2: 番組作成フォーム (ソース選択 + サーバ検証) → 作成 → タイムラインへ戻る。
    from core.models import Channel

    ch = Channel.objects.create(name="E2Eフォームch", slug="e2e-pf", enabled=True)
    _login(page, live_server.url)
    page.goto(
        f"{live_server.url}/studio/program-form/{ch.slug}?start=2026-07-01T20:00:00&end=2026-07-01T21:00:00"
    )
    page.wait_for_selector("#studio-root >> text=番組を追加", timeout=15000)
    page.locator("#studio-root form input").first.fill("E2E新番組")
    page.locator("#studio-root form select").filter(has_text="素材を選択").select_option(
        label="ep1"
    )
    page.click("#studio-root button:has-text('追加')")
    page.wait_for_url("**/studio/scheduling/e2e-pf", timeout=8000)


def test_studio_program_form_exposure_policy(live_server, page, demo, asset_ready):
    """#27: 番組フォームの配信ポリシーセレクタで保存でき、会員限定(YT+サイト)選択時は GPU 警告が出る。"""
    from core.models import Channel
    from scheduling.models import Program

    ch = Channel.objects.create(name="E2E配信ポリシーch", slug="e2e-ep", enabled=True)
    _login(page, live_server.url)
    page.goto(
        f"{live_server.url}/studio/program-form/{ch.slug}?start=2026-07-01T20:00:00&end=2026-07-01T21:00:00"
    )
    page.wait_for_selector("#studio-root >> text=番組を追加", timeout=15000)
    page.locator("#studio-root form input").first.fill("E2E配信ポリシー番組")
    page.locator("#studio-root form select").filter(has_text="素材を選択").select_option(
        label="ep1"
    )
    page.locator("#studio-root form select").filter(has_text="会員限定(YT+サイト)").select_option(
        label="会員限定(YT+サイト)"
    )
    page.wait_for_selector("#studio-root >> text=専用 GPU が実質必須", timeout=5000)
    page.click("#studio-root button:has-text('追加')")
    page.wait_for_url("**/studio/scheduling/e2e-ep", timeout=8000)
    prog = Program.objects.get(channel=ch, title="E2E配信ポリシー番組")
    assert prog.exposure_policy == "members_yt_site"


def test_studio_billing_close_month(live_server, page, demo):
    # #Phase2d-2: 状態遷移の書き込み。月次締め → (警告 confirm は force 受諾) → 期間が締め済で出る。
    _login(page, live_server.url)
    page.goto(f"{live_server.url}/studio/billing")
    page.wait_for_selector("#studio-root input[name=year]", timeout=15000)
    page.on("dialog", lambda d: d.accept())  # 締め前確認は force で続行
    page.fill("#studio-root input[name=year]", "2027")
    page.fill("#studio-root input[name=month]", "1")
    page.click("#studio-root button:has-text('締める')")
    page.wait_for_selector("#studio-root td:has-text('2027-1')", timeout=8000)
    assert "2027-1" in page.content()


def test_studio_mobile_375(live_server, page, demo):
    # 出先での状況確認向けモバイル(375px): 上部バー + ハンバーガードロワー・水平溢れ無し・
    # ドロワーは nav タップで自動クローズ・テーブルページも横溢れしない(table 横スクロール)。
    page.set_viewport_size({"width": 375, "height": 812})
    _login(page, live_server.url)
    page.goto(f"{live_server.url}/studio/")
    page.wait_for_selector("#studio-root .st-topbar", timeout=15000)
    overflow = "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    sidebar_x = "() => document.querySelector('#studio-root .st-sidebar').getBoundingClientRect().x"
    # 上部バーのハンバーガー可視・サイドバーは off-canvas・ダッシュボードは水平溢れ無し
    assert page.is_visible("#studio-root .st-hamburger")
    assert page.evaluate(sidebar_x) < 0
    assert page.evaluate(overflow) <= 1
    # ハンバーガー → ドロワー開
    page.click("#studio-root .st-hamburger")
    page.wait_for_function(f"{sidebar_x} >= 0", timeout=3000)
    # ドロワーから会員統計へ → 遷移 + ドロワー自動クローズ + KPI/テーブルで水平溢れ無し
    page.click("#studio-root .st-sidebar nav a:has-text('会員統計')")
    page.wait_for_url("**/studio/members/stats")
    page.wait_for_selector("#studio-root .st-kpi", timeout=8000)
    page.wait_for_function(f"{sidebar_x} < 0", timeout=3000)  # nav タップでドロワー自動クローズ
    assert page.evaluate(overflow) <= 1


def test_studio_contracts_tab_youtube_destination(live_server, page, demo):
    """#27 Part B: 枠契約タブの YouTube宛先セレクタで保存できる。"""
    from fanclub.models import Creator, SlotContract

    creator = Creator.objects.create(name="E2Eサークル", slug="e2e-circle")
    contract = SlotContract.objects.create(
        creator=creator,
        title="レギュラー枠",
        monthly_fee_minor=50_000,
        starts_on="2026-08-01",
        status="active",
    )
    _login(page, live_server.url)
    page.goto(f"{live_server.url}/studio/creators/{creator.id}?tab=contracts")
    page.wait_for_selector("#studio-root >> text=レギュラー枠", timeout=15000)
    row = page.locator("#studio-root tbody tr").filter(has_text="レギュラー枠")
    row.locator("select").nth(1).select_option(label="クリエイター自チャンネル")
    page.wait_for_selector(
        "#studio-root >> text=同一映像の二重配信は収益化リスクがある", timeout=8000
    )
    contract.refresh_from_db()
    assert contract.youtube_destination == "creator_channel"


def test_studio_contracts_tab_stripe_billed_status_is_readonly(live_server, page, demo):
    """#27 Part 2: Stripe Billing連携済みの枠契約は状態セレクタでなくバッジ表示になる。"""
    from fanclub.models import Creator, SlotContract

    creator = Creator.objects.create(name="E2E枠課金サークル", slug="e2e-slot-billing")
    SlotContract.objects.create(
        creator=creator,
        title="Stripe連携枠",
        monthly_fee_minor=80_000,
        starts_on="2026-08-01",
        status="active",
        stripe_subscription_id="sub_e2e",
    )
    SlotContract.objects.create(
        creator=creator,
        title="手動枠",
        monthly_fee_minor=30_000,
        starts_on="2026-08-01",
        status="draft",
    )
    _login(page, live_server.url)
    page.goto(f"{live_server.url}/studio/creators/{creator.id}?tab=contracts")
    page.wait_for_selector("#studio-root >> text=Stripe連携枠", timeout=15000)

    # 状態列(4番目のtd)のみを見る。YouTube宛先列(5番目)は本テストと無関係に常にselectを持つ。
    stripe_row = page.locator("#studio-root tbody tr").filter(has_text="Stripe連携枠")
    assert stripe_row.locator("td:nth-child(4) select").count() == 0
    assert "Stripe連携" in stripe_row.inner_text()

    manual_row = page.locator("#studio-root tbody tr").filter(has_text="手動枠")
    assert manual_row.locator("td:nth-child(4) select").count() == 1


def test_studio_creator_settlements_tab(live_server, page, demo):
    """#27 Phase B: 分配元帳タブが累計サマリと明細を表示する (PR#70 移植分の回帰確認)。"""
    from fanclub.models import Creator, CreatorTier, FcSettlement

    creator = Creator.objects.create(name="E2E分配サークル", slug="e2e-settlement")
    tier = CreatorTier.objects.create(creator=creator, level=1, name="ベーシック", price_minor=500)
    FcSettlement.objects.create(
        creator=creator,
        tier=tier,
        stripe_invoice_id="in_e2e_1",
        gross_amount_minor=500,
        application_fee_minor=50,
        net_amount_minor=450,
    )
    _login(page, live_server.url)
    page.goto(f"{live_server.url}/studio/creators/{creator.id}?tab=settlements")
    page.wait_for_selector("#studio-root >> text=累計送金額", timeout=15000)
    assert "¥450" in page.content()
    assert "¥500" in page.content()
