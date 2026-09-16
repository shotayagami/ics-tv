# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""admin UI (staff 専用) と YouTube OAuth フローの HTTP smoke。

実 OAuth (Google Token Endpoint) には到達しない error path テストのみ。
"""

from __future__ import annotations


def test_channel_settings_unauthenticated_redirects_to_login(http_client, channel):
    res = http_client.get(f"/admin-ui/ch/{channel.slug}/")
    # staff_member_required は /admin/login/ にリダイレクト
    assert res.status_code == 302
    assert "login" in res.url


def test_channel_settings_staff_renders_ok(staff_client, channel):
    res = staff_client.get(f"/admin-ui/ch/{channel.slug}/")
    assert res.status_code == 200
    body = res.content.decode("utf-8")
    assert channel.name in body
    assert "YouTube" in body


def test_channel_settings_shows_oauth_not_configured_warning(staff_client, channel, monkeypatch):
    monkeypatch.delenv("ICSTV_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("ICSTV_OAUTH_CLIENT_SECRET", raising=False)
    res = staff_client.get(f"/admin-ui/ch/{channel.slug}/")
    body = res.content.decode("utf-8")
    assert "未設定" in body


def test_channel_settings_shows_youtube_api_disclosure(staff_client, channel):
    """YouTube Data API 利用開示は API を実際に使う studio 側 (OAuth 連携カード) に掲示。

    視聴者は API に触れないため公開トップからは移設済み (test_host_split を参照)。
    開示はプライバシー/規約への導線 (公開ページ) を伴う。
    """
    res = staff_client.get(f"/admin-ui/ch/{channel.slug}/")
    body = res.content.decode("utf-8")
    assert "YouTube Data API" in body
    assert "プライバシーポリシー" in body and "利用規約" in body


# ---- YouTube 配信メニュー (#23 で「設定」から分離した統合入口) ----


def test_youtube_console_unauthenticated_redirects(http_client, channel):
    res = http_client.get(f"/admin-ui/ch/{channel.slug}/youtube/")
    assert res.status_code == 302
    assert "login" in res.url


def test_youtube_console_collects_subdashboards(staff_client, channel):
    """YouTube メニューは rolling 枠 / 配信プリセット / 番組専用枠 (#23) の入口を集約する。"""
    res = staff_client.get(f"/admin-ui/ch/{channel.slug}/youtube/")
    assert res.status_code == 200
    body = res.content.decode("utf-8")
    assert f"/admin-ui/ch/{channel.slug}/youtube/slots/" in body  # 枠ダッシュボード
    assert "/admin-ui/youtube/presets/" in body  # 配信プリセット
    assert f"/admin-ui/ch/{channel.slug}/youtube/dedicated/" in body  # 番組専用枠
    assert "番組専用枠" in body


def test_nav_exposes_youtube_menu(staff_client, channel):
    """上部ナビに YouTube メニューが出る (channel スコープ時)。"""
    res = staff_client.get(f"/admin-ui/ch/{channel.slug}/youtube/")
    body = res.content.decode("utf-8")
    assert f'href="/admin-ui/ch/{channel.slug}/youtube/"' in body


def test_channel_settings_delegates_youtube_to_menu(staff_client, channel):
    """設定からは枠/プリセット/専用枠ボタンを撤去し、YouTube メニューへの導線のみ残す。"""
    res = staff_client.get(f"/admin-ui/ch/{channel.slug}/")
    body = res.content.decode("utf-8")
    # 旧ボタン (rolling 枠ダッシュボード / 番組専用枠ダッシュボード) は設定本文から消える。
    assert f"/admin-ui/ch/{channel.slug}/youtube/slots/" not in body
    assert f"/admin-ui/ch/{channel.slug}/youtube/dedicated/" not in body
    # 代わりに YouTube メニューへのリンクが残る (ナビ + 案内カード)。
    assert f"/admin-ui/ch/{channel.slug}/youtube/" in body


def _filler_playlist(name="ch1 既定フィラー"):
    from medialib.models import FillerPlaylist

    return FillerPlaylist.objects.create(name=name)


def _slate_asset(title="myslate", ready=True):
    from medialib.models import Asset, AssetKind, NormalizeStatus

    return Asset.objects.create(
        kind=AssetKind.FILLER,
        title=title,
        duration_ms=10000,
        r2_key="mezzanine/slate/1.mp4",
        normalize_status=NormalizeStatus.READY if ready else NormalizeStatus.PENDING,
    )


def test_channel_settings_renders_media_selectors(staff_client, channel):
    pl = _filler_playlist()
    _slate_asset()
    res = staff_client.get(f"/admin-ui/ch/{channel.slug}/")
    body = res.content.decode("utf-8")
    assert "送出メディア" in body
    assert pl.name in body
    assert "myslate" in body


def test_channel_settings_slate_candidates_exclude_unnormalized(staff_client, channel):
    """スレート候補は正規化済 (READY) のみ (R2 先読み前提)。"""
    _slate_asset(title="ready_slate", ready=True)
    _slate_asset(title="pending_slate", ready=False)
    res = staff_client.get(f"/admin-ui/ch/{channel.slug}/")
    body = res.content.decode("utf-8")
    assert "ready_slate" in body
    assert "pending_slate" not in body


def test_set_channel_media_saves_filler_and_slate(staff_client, channel):
    pl = _filler_playlist()
    slate = _slate_asset()
    res = staff_client.post(
        f"/admin-ui/ch/{channel.slug}/media/",
        {"default_filler": str(pl.id), "slate_asset": str(slate.id)},
    )
    assert res.status_code == 302
    channel.refresh_from_db()
    assert channel.default_filler_id == pl.id
    assert channel.slate_asset_id == slate.id


def test_set_channel_media_empty_clears(staff_client, channel):
    pl = _filler_playlist()
    slate = _slate_asset()
    channel.default_filler = pl
    channel.slate_asset = slate
    channel.save(update_fields=["default_filler", "slate_asset"])
    res = staff_client.post(
        f"/admin-ui/ch/{channel.slug}/media/", {"default_filler": "", "slate_asset": ""}
    )
    assert res.status_code == 302
    channel.refresh_from_db()
    assert channel.default_filler_id is None
    assert channel.slate_asset_id is None


def test_set_channel_media_requires_staff(http_client, channel):
    res = http_client.post(f"/admin-ui/ch/{channel.slug}/media/", {})
    assert res.status_code == 302
    assert "login" in res.url


# ---- チャンネル名 / slug 編集 (全ch一覧の専用画面) ----


def test_channel_list_unauthenticated_redirects_to_login(http_client, channel):
    res = http_client.get("/admin-ui/channels/")
    assert res.status_code == 302
    assert "login" in res.url


def test_channel_list_staff_renders_name_and_slug(staff_client, channel):
    res = staff_client.get("/admin-ui/channels/")
    assert res.status_code == 200
    body = res.content.decode("utf-8")
    assert "チャンネル編集" in body
    assert channel.name in body
    assert f'value="{channel.slug}"' in body


def test_channel_update_changes_name(staff_client, channel):
    res = staff_client.post(
        f"/admin-ui/channels/{channel.slug}/update/",
        {"name": "総合", "slug": channel.slug},
    )
    assert res.status_code == 302
    channel.refresh_from_db()
    assert channel.name == "総合"
    assert channel.slug == "ch1"  # slug は据え置き


def test_channel_update_changes_slug_and_follows_session(staff_client, channel):
    # 現在 session で選択中の ch を rename → session も追従する
    session = staff_client.session
    session["current_ch"] = channel.slug
    session.save()
    res = staff_client.post(
        f"/admin-ui/channels/{channel.slug}/update/",
        {"name": "教育", "slug": "ch-edu"},
    )
    assert res.status_code == 302
    channel.refresh_from_db()
    assert channel.slug == "ch-edu"
    assert channel.name == "教育"
    assert staff_client.session.get("current_ch") == "ch-edu"


def test_channel_update_rejects_invalid_slug(staff_client, channel):
    res = staff_client.post(
        f"/admin-ui/channels/{channel.slug}/update/",
        {"name": "総合", "slug": "ch 1!"},  # 空白・記号は不可
    )
    assert res.status_code == 302  # エラーは messages で通知し一覧へ戻る
    channel.refresh_from_db()
    assert channel.slug == "ch1"  # 変更されない


def test_channel_update_rejects_duplicate_slug(staff_client, channel):
    from core.models import Channel

    Channel.objects.create(name="ICS-TV 2ch", slug="ch2", enabled=True)
    res = staff_client.post(
        f"/admin-ui/channels/{channel.slug}/update/",
        {"name": "総合", "slug": "ch2"},  # 既存と衝突
    )
    assert res.status_code == 302
    channel.refresh_from_db()
    assert channel.slug == "ch1"  # 変更されない


def test_channel_update_empty_name_rejected(staff_client, channel):
    res = staff_client.post(
        f"/admin-ui/channels/{channel.slug}/update/",
        {"name": "  ", "slug": channel.slug},
    )
    assert res.status_code == 302
    channel.refresh_from_db()
    assert channel.name == "ICS-TV 1ch"  # 変更されない


def test_channel_update_requires_post(staff_client, channel):
    res = staff_client.get(f"/admin-ui/channels/{channel.slug}/update/")
    assert res.status_code == 405


def test_channel_update_requires_staff(http_client, channel):
    res = http_client.post(
        f"/admin-ui/channels/{channel.slug}/update/",
        {"name": "総合", "slug": "ch1"},
    )
    assert res.status_code == 302
    assert "login" in res.url


def test_youtube_oauth_start_without_env_returns_503(staff_client, channel, monkeypatch):
    monkeypatch.delenv("ICSTV_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("ICSTV_OAUTH_CLIENT_SECRET", raising=False)
    res = staff_client.get(f"/admin-ui/ch/{channel.slug}/youtube/connect/")
    assert res.status_code == 503
    assert "OAuth" in res.content.decode("utf-8")


def test_youtube_oauth_start_redirects_to_google_when_configured(
    staff_client, channel, monkeypatch
):
    monkeypatch.setenv("ICSTV_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("ICSTV_OAUTH_CLIENT_SECRET", "test-client-secret")
    res = staff_client.get(f"/admin-ui/ch/{channel.slug}/youtube/connect/")
    assert res.status_code == 302
    assert "accounts.google.com" in res.url
    assert "scope=" in res.url
    # session に state が保存される
    assert staff_client.session.get("youtube_oauth_state")
    assert staff_client.session.get("youtube_oauth_channel_slug") == channel.slug
    # PKCE: code_verifier も保持される (callback の token 交換で必要。未保持だと invalid_grant 500)
    assert staff_client.session.get("youtube_oauth_code_verifier")


def test_youtube_oauth_callback_state_mismatch_400(staff_client):
    res = staff_client.get("/admin-ui/oauth/callback/?state=wrong&code=fake")
    assert res.status_code == 400
    assert "state" in res.content.decode("utf-8")


def test_youtube_oauth_callback_no_session_400(staff_client):
    res = staff_client.get("/admin-ui/oauth/callback/?state=any&code=fake")
    assert res.status_code == 400


# ---- 永続 liveStream 作成 ----


def test_create_persistent_stream_requires_post(staff_client, channel):
    res = staff_client.get(
        f"/admin-ui/ch/{channel.slug}/youtube/livestream/create/",
    )
    assert res.status_code == 405


def test_create_persistent_stream_unauthenticated_redirects(http_client, channel):
    res = http_client.post(
        f"/admin-ui/ch/{channel.slug}/youtube/livestream/create/",
    )
    assert res.status_code == 302


def test_create_persistent_stream_without_oauth_412(staff_client, channel):
    res = staff_client.post(
        f"/admin-ui/ch/{channel.slug}/youtube/livestream/create/",
    )
    assert res.status_code == 412
    assert "OAuth" in res.content.decode("utf-8")


def test_create_persistent_stream_existing_409(staff_client, channel):
    from youtube.models import YoutubeCredential

    YoutubeCredential.objects.create(
        channel=channel,
        client_id="cid",
        client_secret="csecret",  # pragma: allowlist secret - test only
        refresh_token="rtoken",  # pragma: allowlist secret - test only
    )
    channel.youtube_livestream_id = "existing-livestream-id"
    channel.save(update_fields=["youtube_livestream_id"])
    res = staff_client.post(
        f"/admin-ui/ch/{channel.slug}/youtube/livestream/create/",
    )
    assert res.status_code == 409


# ---- 枠ダッシュボード ----


def test_slot_dashboard_unauthenticated_redirects(http_client, channel):
    res = http_client.get(f"/admin-ui/ch/{channel.slug}/youtube/slots/")
    assert res.status_code == 302


def test_slot_dashboard_staff_renders_empty(staff_client, channel):
    res = staff_client.get(f"/admin-ui/ch/{channel.slug}/youtube/slots/")
    assert res.status_code == 200
    body = res.content.decode("utf-8")
    assert "枠ダッシュボード" in body
    assert "枠なし" in body


def test_slot_dashboard_lists_slots_in_recent_order(staff_client, channel):
    from datetime import timedelta

    from django.utils import timezone

    from youtube.models import YoutubeSlot, YtSlotStatus

    now = timezone.now()
    for i in range(3):
        YoutubeSlot.objects.create(
            channel=channel,
            window_start=now + timedelta(hours=2 * i),
            window_end=now + timedelta(hours=2 * (i + 1)),
            status=YtSlotStatus.READY if i else YtSlotStatus.LIVE,
            broadcast_id=f"YT-{i}",
            title=f"slot-{i}",
        )
    res = staff_client.get(f"/admin-ui/ch/{channel.slug}/youtube/slots/")
    body = res.content.decode("utf-8")
    assert "YT-0" in body
    assert "YT-1" in body
    assert "YT-2" in body


# ---- 手動 transition / 削除 ----


def _make_slot(channel, *, status, broadcast_id="YT-x"):
    from datetime import timedelta

    from django.utils import timezone

    from youtube.models import YoutubeSlot

    now = timezone.now()
    return YoutubeSlot.objects.create(
        channel=channel,
        window_start=now,
        window_end=now + timedelta(hours=2),
        status=status,
        broadcast_id=broadcast_id,
        title="t",
    )


def test_slot_transition_requires_post(staff_client, channel):
    slot = _make_slot(channel, status="ready")
    res = staff_client.get(f"/admin-ui/slot/{slot.id}/transition/")
    assert res.status_code == 405


def test_slot_transition_invalid_target_400(staff_client, channel):
    slot = _make_slot(channel, status="ready")
    res = staff_client.post(f"/admin-ui/slot/{slot.id}/transition/", data={"target": "garbage"})
    assert res.status_code == 400


def test_slot_transition_no_broadcast_id_412(staff_client, channel):
    slot = _make_slot(channel, status="ready", broadcast_id="")
    res = staff_client.post(f"/admin-ui/slot/{slot.id}/transition/", data={"target": "live"})
    assert res.status_code == 412


def test_slot_transition_unauthenticated_redirects(http_client, channel):
    slot = _make_slot(channel, status="ready")
    res = http_client.post(f"/admin-ui/slot/{slot.id}/transition/", data={"target": "live"})
    assert res.status_code == 302


def test_slot_transition_to_live_updates_status(staff_client, channel, monkeypatch):
    slot = _make_slot(channel, status="ready")
    called = {}

    def fake_transition(ch, bid, target):
        called["target"] = target
        called["bid"] = bid

    monkeypatch.setattr("youtube.api.transition_broadcast", fake_transition)
    res = staff_client.post(f"/admin-ui/slot/{slot.id}/transition/", data={"target": "live"})
    assert res.status_code == 302
    slot.refresh_from_db()
    assert slot.status == "live"
    assert called["target"] == "live"
    assert called["bid"] == "YT-x"


def test_slot_meta_update_pushes_to_youtube_and_sets_manual(staff_client, channel, monkeypatch):
    slot = _make_slot(channel, status="ready")
    called = {}

    def fake_update(ch, bid, *, title, scheduled_start, description=""):
        called.update(bid=bid, title=title, description=description)

    monkeypatch.setattr("youtube.api.update_broadcast", fake_update)
    res = staff_client.post(
        f"/admin-ui/slot/{slot.id}/meta/",
        data={"title": "19時のニュース ほか", "description": "19:00 ニュース\n20:00 ドラマ"},
    )
    assert res.status_code == 302
    slot.refresh_from_db()
    assert slot.title == "19時のニュース ほか" and slot.manual is True
    assert "ドラマ" in slot.description
    assert called["bid"] == "YT-x" and called["title"] == "19時のニュース ほか"


def test_slot_meta_update_empty_title_400(staff_client, channel):
    slot = _make_slot(channel, status="ready")
    res = staff_client.post(f"/admin-ui/slot/{slot.id}/meta/", data={"title": "  "})
    assert res.status_code == 400


def test_slot_meta_update_no_broadcast_skips_api(staff_client, channel, monkeypatch):
    slot = _make_slot(channel, status="created", broadcast_id="")

    def boom(*a, **k):
        raise AssertionError("broadcast 未化のとき API を呼んではいけない")

    monkeypatch.setattr("youtube.api.update_broadcast", boom)
    res = staff_client.post(f"/admin-ui/slot/{slot.id}/meta/", data={"title": "下書き"})
    assert res.status_code == 302
    slot.refresh_from_db()
    assert slot.title == "下書き" and slot.manual is True


def _youtube_config(channel, **kw):
    from youtube.models import YoutubeConfig

    return YoutubeConfig.objects.create(
        channel=channel,
        title_template=kw.pop("title_template", "ICS-TV {date} {start}-{end}"),
        description_template=kw.pop(
            "description_template", "ICS-TV {date} {start}-{end} の枠です。\n\n{programs}"
        ),
        **kw,
    )


def test_slot_apply_template_recomposes_and_pushes(staff_client, channel, monkeypatch):
    # テンプレートを反映: 手動編集を破棄しテンプレ由来へ戻し、broadcast 化済みなら YouTube へ反映。
    _youtube_config(channel)
    slot = _make_slot(channel, status="ready")
    slot.title = "手動タイトル"
    slot.manual = True
    slot.save(update_fields=["title", "manual"])
    called = {}

    def fake_update(ch, bid, *, title, scheduled_start, description=""):
        called.update(bid=bid, title=title, description=description)

    monkeypatch.setattr("youtube.api.update_broadcast", fake_update)
    res = staff_client.post(f"/admin-ui/slot/{slot.id}/apply-template/")
    assert res.status_code == 302
    slot.refresh_from_db()
    assert slot.manual is False  # テンプレ追従下に戻る
    assert slot.title.startswith("ICS-TV ")  # テンプレ由来へ再生成
    assert called["bid"] == "YT-x" and called["title"] == slot.title


def test_slot_apply_template_without_config_412(staff_client, channel):
    slot = _make_slot(channel, status="ready")
    res = staff_client.post(f"/admin-ui/slot/{slot.id}/apply-template/")
    assert res.status_code == 412


def test_slot_apply_template_no_broadcast_skips_api(staff_client, channel, monkeypatch):
    _youtube_config(channel)
    slot = _make_slot(channel, status="created", broadcast_id="")

    def boom(*a, **k):
        raise AssertionError("broadcast 未化のとき API を呼んではいけない")

    monkeypatch.setattr("youtube.api.update_broadcast", boom)
    res = staff_client.post(f"/admin-ui/slot/{slot.id}/apply-template/")
    assert res.status_code == 302
    slot.refresh_from_db()
    assert slot.title.startswith("ICS-TV ") and slot.manual is False


def test_slot_apply_template_requires_post(staff_client, channel):
    slot = _make_slot(channel, status="ready")
    res = staff_client.get(f"/admin-ui/slot/{slot.id}/apply-template/")
    assert res.status_code == 405


def test_slot_dashboard_edit_panel_is_full_width_row(staff_client, channel):
    # 編集パネルは枠をはみ出さないよう colspan=7 の全幅行で開く (id=edit-<slot>)。
    slot = _make_slot(channel, status="ready")
    res = staff_client.get(f"/admin-ui/ch/{channel.slug}/youtube/slots/")
    body = res.content.decode("utf-8")
    assert f'id="edit-{slot.id}"' in body
    assert "テンプレートを反映" in body


def test_slot_delete_removes_db_row_even_when_api_404(staff_client, channel, monkeypatch):
    from googleapiclient.errors import HttpError

    from youtube.models import YoutubeSlot

    slot = _make_slot(channel, status="complete")

    def fake_delete(ch, bid):
        # 既に削除済みを擬似 (404)
        resp = type("R", (), {"status": 404, "reason": "Not Found"})()
        raise HttpError(resp=resp, content=b"")

    monkeypatch.setattr("youtube.api.delete_broadcast", fake_delete)
    res = staff_client.post(f"/admin-ui/slot/{slot.id}/delete/")
    assert res.status_code == 302
    assert not YoutubeSlot.objects.filter(pk=slot.id).exists()
