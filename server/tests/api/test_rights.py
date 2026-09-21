# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""権利・コンプライアンス (#RIGHTS-02): 配信権による VOD ゲート。"""

from __future__ import annotations

from datetime import timedelta

from django.test import override_settings
from django.utils import timezone

from rights.models import DistributionRight
from rights.services import filter_vod_allowed, vod_allowed
from scheduling.models import Program, ProgramType, VodVisibility
from scheduling.vod import available_vod_qs

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])


def _program(
    channel, asset, *, ended_ago=timedelta(hours=2), vod=VodVisibility.PUBLIC, title="番組"
):
    end = timezone.now() - ended_ago
    return Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title=title,
        start_at=end - timedelta(hours=1),
        end_at=end,
        asset=asset,
        public_visible=True,
        vod_visibility=vod,
    )


# ---- RIGHTS-02 VOD ゲート ----


def test_no_rights_record_allows_vod(channel, asset_ready, db):
    p = _program(channel, asset_ready)
    assert vod_allowed(p) is True
    assert list(available_vod_qs()) == [p]


def test_active_vod_right_allows(channel, asset_ready, db):
    p = _program(channel, asset_ready)
    DistributionRight.objects.create(program=p, holder="権利者A", allow_vod=True)
    assert vod_allowed(p) is True
    assert list(available_vod_qs()) == [p]


def test_expired_vod_right_blocks(channel, asset_ready, db):
    p = _program(channel, asset_ready)
    DistributionRight.objects.create(
        program=p,
        holder="権利者A",
        allow_vod=True,
        available_until=timezone.now() - timedelta(days=1),  # 期限切れ
    )
    assert vod_allowed(p) is False
    assert list(available_vod_qs()) == []  # ゲートで除外


def test_vod_forbidden_right_blocks(channel, asset_ready, db):
    p = _program(channel, asset_ready)
    DistributionRight.objects.create(program=p, holder="権利者A", allow_vod=False)
    assert vod_allowed(p) is False
    assert list(available_vod_qs()) == []


def test_future_window_blocks(channel, asset_ready, db):
    p = _program(channel, asset_ready)
    DistributionRight.objects.create(
        program=p,
        holder="権利者A",
        allow_vod=True,
        available_from=timezone.now() + timedelta(days=1),  # まだ開始前
    )
    assert list(available_vod_qs()) == []


def test_one_active_among_many_allows(channel, asset_ready, db):
    p = _program(channel, asset_ready)
    DistributionRight.objects.create(program=p, holder="旧", allow_vod=False)
    DistributionRight.objects.create(program=p, holder="新", allow_vod=True)  # 1件でも有効なら可
    assert list(available_vod_qs()) == [p]


def test_filter_vod_allowed_keeps_unconstrained(channel, asset_ready, db):
    p1 = _program(channel, asset_ready, title="権利なし")
    qs = filter_vod_allowed(Program.objects.filter(pk=p1.pk))
    assert list(qs) == [p1]


# ---- 権利ダッシュボード ----


def test_rights_dashboard_requires_staff(staff_client, db):
    # admin ホスト (既定 testserver) で staff のみアクセス可 (staff_member_required)
    res = staff_client.get("/rights/")
    assert res.status_code == 200


def test_rights_dashboard_lists_only_window_rows(staff_client, channel, asset_ready, db):
    # 表示対象は allow_vod=True かつ available_until が今から 30 日以内の行だけ。
    # 30 日の境界ちょうどはビューとテストで now の取得時刻が違い不安定なので使わない。
    # 番組の時間帯は排他制約 (program_no_overlap_per_channel) に当たるため ended_ago をずらす。
    now = timezone.now()
    p_in = _program(channel, asset_ready, ended_ago=timedelta(hours=2), title="タイトルアルファ")
    p_far = _program(channel, asset_ready, ended_ago=timedelta(hours=5), title="タイトルブラボー")
    p_novod = _program(
        channel, asset_ready, ended_ago=timedelta(hours=8), title="タイトルチャーリー"
    )
    DistributionRight.objects.create(
        program=p_in,
        holder="ホルダーアルファ",
        allow_vod=True,
        available_until=now + timedelta(days=10),
    )
    DistributionRight.objects.create(
        program=p_far,
        holder="ホルダーブラボー",
        allow_vod=True,
        available_until=now + timedelta(days=40),  # 30 日超 → 出ない
    )
    DistributionRight.objects.create(
        program=p_novod,
        holder="ホルダーチャーリー",
        allow_vod=False,  # VOD 不許可 → 窓内でも出ない
        available_until=now + timedelta(days=12),
    )
    res = staff_client.get("/rights/")
    assert res.status_code == 200
    body = res.content.decode("utf-8")
    assert "タイトルアルファ" in body
    assert "ホルダーアルファ" in body
    assert "タイトルブラボー" not in body
    assert "ホルダーブラボー" not in body
    assert "タイトルチャーリー" not in body
    assert "ホルダーチャーリー" not in body
    assert "楽曲" not in body
    assert "music-usage" not in body


@_PUBLIC_HOST
def test_program_detail_hides_vod_cta_when_rights_block(http_client, channel, asset_ready, db):
    # #Phase2c: 視聴CTAは操作バー島が /api/v1/program/{id} から描画。配信権で VOD 不可なら
    # available_vod_qs(filter_vod_allowed) が除外 → vod CTA を出さない (放送済み+正規化済みでも)。
    p = _program(channel, asset_ready)
    DistributionRight.objects.create(program=p, holder="A", allow_vod=False)
    d = http_client.get(f"/api/v1/program/{p.id}").json()
    assert d["cta"]["kind"] != "vod"  # 権利ブロック → 見逃し再生 CTA 出ない
