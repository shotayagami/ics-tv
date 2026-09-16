# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""公開 番組詳細 操作バー島 (#Phase2c) の E2E (Playwright headless chromium)。

/program/{id}/ の本文は SSR、操作バー (状態別CTA + お気に入り/リマインド/共有) は React 島が
/api/v1/program/{id} から描画する。匿名 + 放送予定番組で島マウント・ログイン導線・共有・
テンプレコメント非漏洩を検証する。実行手順は test_player_island.py に準ずる。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.test import override_settings
from django.utils import timezone

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])


@pytest.fixture
def upcoming_program(channel, asset_ready):
    from scheduling.models import Program, ProgramType

    now = timezone.now()
    return Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title="詳細E2E 放送予定番組",
        asset=asset_ready,
        start_at=now + timedelta(days=1),
        end_at=now + timedelta(days=1, hours=1),
        public_visible=True,
    )


@_PUBLIC_HOST
def test_program_actions_anonymous(live_server, page, upcoming_program):
    page.goto(f"{live_server.url}/program/{upcoming_program.id}/")
    # 本文 (タイトル) は SSR で即時、操作バーは島が API 取得後に描画。
    assert "詳細E2E 放送予定番組" in page.content()
    page.wait_for_selector("#program-actions-island .btn-share", timeout=15000)
    body = page.content()
    assert "放送予定" in body  # planpill
    assert "あとで見る" in body  # 匿名 → ログイン導線 (お気に入り)
    assert "開始を通知" in body  # 匿名 → ログイン導線 (リマインド)
    # ログイン未済の操作はトグルでなくログインページへのリンク。
    href = page.get_attribute("#program-actions-island a.btn-pin:has-text('あとで見る')", "href")
    assert href and "/members/login/" in href


@_PUBLIC_HOST
def test_program_detail_no_comment_leak(live_server, page, upcoming_program):
    # Django {# #} は単一行専用。島マウント化した program.html が複数行コメントを漏らさない。
    page.goto(f"{live_server.url}/program/{upcoming_program.id}/")
    page.wait_for_selector("#program-actions-island .btn-share", timeout=15000)
    assert "{#" not in page.content()
