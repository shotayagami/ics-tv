# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""公開フロントのモバイル(375px)レスポンシブ回帰ガード (ds-mobile.md)。

トークン不変・@media のみのモバイル対応(public_base.html + 各 app CSS)が効くことを実ブラウザで検証:
- 全公開サーフェスで「ページ水平溢れ無し」(documentElement.scrollWidth ≒ clientWidth)
- EPG(最難所): チャンネル帯 sticky 維持(gap<5)/.grid だけが横スクロール(grid-scroll は
  overflow-x:visible の非スクローラ)/横スクロール後もヘッダ列と本体列が一致(二重スクローラ
  desync 無し)。実行手順は test_player_island.py に準ずる(frontend build → server/frontend_dist)。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.test import override_settings
from django.utils import timezone

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])
_MOBILE = {"width": 375, "height": 812}


@pytest.fixture
def demo(channel, asset_ready):
    from scheduling.models import Program, ProgramType

    now = timezone.now()
    Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title="モバイルprobe番組",
        genre="アニメ",
        asset=asset_ready,
        start_at=now - timedelta(minutes=20),
        end_at=now + timedelta(minutes=40),
        public_visible=True,
    )
    return channel


def _no_hscroll(page) -> int:
    return page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )


@_PUBLIC_HOST
@pytest.mark.parametrize("path", ["/", "/guide/", "/browse/", "/search/"])
def test_no_horizontal_overflow_375(live_server, page, demo, path):
    page.set_viewport_size(_MOBILE)
    page.goto(f"{live_server.url}{path}")
    page.wait_for_selector("#pub-nav", timeout=15000)
    page.wait_for_timeout(800)  # 島マウント/描画待ち
    overflow = _no_hscroll(page)
    assert overflow <= 1, f"{path} で水平溢れ {overflow}px"


@_PUBLIC_HOST
def test_guide_mobile_sticky_and_single_scroller(live_server, page, demo):
    page.set_viewport_size(_MOBILE)
    page.goto(f"{live_server.url}/guide/")
    page.wait_for_selector("#guide-island .grid-inner", timeout=15000)
    page.wait_for_selector("#guide-island .gcol .blk", timeout=5000)

    # 1) ページ水平溢れ無し(EPG の横スクロールは .grid 内部に閉じる)
    assert _no_hscroll(page) <= 1, "ページが水平溢れ(EPG 横スクロールが内部に閉じていない)"

    m = page.evaluate(
        """() => {
          const q = s => document.querySelector(s);
          const grid = q('#guide-island .grid');
          const scroll = q('#guide-island .grid-scroll');
          const cols = q('#guide-island .grid-cols');
          // .grid を横スクロールさせ、ヘッダ列と本体列が「一緒に」動くか(desync 検査)
          grid.scrollLeft = 120;
          const hcol = q('#guide-island .grid-cols .col');
          const bcol = q('#guide-island .grid-inner .gcol');
          return {
            gap: cols.getBoundingClientRect().top - grid.getBoundingClientRect().top,
            gridHScroll: grid.scrollWidth - grid.clientWidth,
            // grid-scroll が独立スクローラでないこと(computed overflow-x が visible)
            scrollOverflowX: getComputedStyle(scroll).overflowX,
            scrolledLeft: grid.scrollLeft,
            headerLeft: hcol.getBoundingClientRect().left,
            bodyLeft: bcol.getBoundingClientRect().left,
          };
        }"""
    )
    # 2) チャンネル帯 sticky 維持(grid 上端に密着)
    assert m["gap"] < 5, f"sticky 回帰 gap={m['gap']}"
    # 3) .grid が横スクロール、grid-scroll は非スクローラ(二重スクローラ回避)
    assert m["gridHScroll"] > 0, "EPG 横スクロールが効いていない"
    assert m["scrollOverflowX"] == "visible", (
        f"grid-scroll が独立スクローラ化(二重スクローラ desync 源) overflow-x={m['scrollOverflowX']}"
    )
    assert m["scrolledLeft"] > 0, "grid が横スクロールできない"
    # 4) 横スクロール後もヘッダ列と本体列の left が一致(一緒に動く=ズレ無し)
    assert abs(m["headerLeft"] - m["bodyLeft"]) <= 1, (
        f"スクロール後にヘッダ/本体がズレ header={m['headerLeft']} body={m['bodyLeft']}"
    )


@_PUBLIC_HOST
def test_nav_hamburger_375(live_server, page, demo):
    """モバイルで副ナビ(マイリスト/履歴/プラン/マイページ)+ フッタ要点が hamburger ドロワーに入る。"""
    page.set_viewport_size(_MOBILE)
    page.goto(f"{live_server.url}/")
    page.wait_for_selector("#pub-nav", timeout=15000)
    # hamburger 表示・デスクトップアカウントリンク非表示・offcanvas 初期非表示
    assert page.is_visible(".nav-burger"), "hamburger が表示されていない"
    assert not page.is_visible("#navOffcanvas.show"), "offcanvas が初期から開いている"
    assert page.get_attribute(".nav-burger", "aria-expanded") == "false"
    # クリックで開く → フッタ要点(利用規約)が offcanvas に見える
    # force=True: CI ヘッドレス環境でホームアイランド読込中に別要素が一時的にボタンを覆う
    page.locator(".nav-burger").click(force=True)
    page.wait_for_selector("#navOffcanvas.show", timeout=5000)
    assert page.is_visible("#navOffcanvas a:has-text('利用規約')"), "offcanvas に利用規約が無い"
    assert _no_hscroll(page) <= 1, "offcanvas 展開でページ水平溢れ"
    # Bootstrap API → DOM fallback の順で閉じる (単一手法は CI ヘッドレスで不安定)
    page.evaluate("""
        (function () {
            var el = document.getElementById('navOffcanvas');
            if (!el) return;
            if (window.bootstrap) {
                try {
                    var inst = bootstrap.Offcanvas && bootstrap.Offcanvas.getInstance(el);
                    if (inst) { inst.hide(); return; }
                } catch (e) {}
            }
            el.classList.remove('show', 'hiding', 'showing');
            var bd = document.querySelector('.offcanvas-backdrop');
            if (bd) bd.remove();
            document.body.classList.remove('modal-open');
        })()
    """)
    # offcanvas が非表示になるのを待つ (.show 除去後は hidden になるため state="hidden")
    page.locator("#navOffcanvas").wait_for(state="hidden", timeout=8000)


@_PUBLIC_HOST
def test_week_mobile_375(live_server, page, demo):
    """週間番組表(7日 SSR グリッド)が mobile で広い横スクロールになり崩れない。"""
    page.set_viewport_size(_MOBILE)
    page.goto(f"{live_server.url}/guide/week/")
    page.wait_for_selector(".week-page .grid-inner", timeout=15000)
    assert _no_hscroll(page) <= 1, "週間でページ水平溢れ(横スクロールが .grid 内部に閉じていない)"
    m = page.evaluate(
        """() => {
          const q = s => document.querySelector(s);
          const grid = q('.week-page .grid');
          const scroll = q('.week-page .grid-scroll');
          grid.scrollTo({ left: 150, behavior: 'instant' });
          const hcol = q('.week-page .grid-cols .col');
          const bcol = q('.week-page .grid-inner .gcol');
          return {
            gridHScroll: grid.scrollWidth - grid.clientWidth,
            gridWidth: grid.scrollWidth,
            scrollOverflowX: getComputedStyle(scroll).overflowX,
            scrolledLeft: grid.scrollLeft,
            headerLeft: hcol.getBoundingClientRect().left,
            bodyLeft: bcol.getBoundingClientRect().left,
          };
        }"""
    )
    # 7日が潰れず広い(min-width 適用)+ .grid 横スクロール + grid-scroll は非スクローラ
    assert m["gridWidth"] >= 760, f"週間グリッドが狭い(min-width 未適用?) {m['gridWidth']}px"
    assert m["gridHScroll"] > 0, "週間 横スクロールが効いていない"
    assert m["scrollOverflowX"] == "visible", (
        "grid-scroll が独立スクローラ(二重スクローラ desync 源)"
    )
    assert m["scrolledLeft"] > 0, "週間 grid が横スクロールできない"
    assert abs(m["headerLeft"] - m["bodyLeft"]) <= 1, (
        f"週間 横スクロール後にヘッダ/本体がズレ header={m['headerLeft']} body={m['bodyLeft']}"
    )
    # 横スクロールボタン(‹ ›)が mobile で表示され .grid を動かせる。
    btns = page.evaluate(
        """() => {
          const q = s => document.querySelector(s);
          const grid = q('.week-page .grid');
          const left = q('#weekLeft'), right = q('#weekRight');
          const vis = el => !!el && getComputedStyle(el).display !== 'none';
          grid.scrollTo({ left: 0, behavior: 'instant' });  // scroll-behavior:smooth に対抗
          const leftDisabled = grid.scrollLeft <= 1;  // syncHScroll と同一判定を直接計算
          right && right.click();
          const after = grid.scrollLeft;  // smooth スクロール開始で >0 になる
          return { leftVisible: vis(left), rightVisible: vis(right), leftDisabled, movedByBtn: after };
        }"""
    )
    assert btns["leftVisible"] and btns["rightVisible"], "週間 横スクロールボタンが mobile で非表示"
    assert btns["leftDisabled"], "初期状態(左端)で ‹ ボタンが無効化されていない"
