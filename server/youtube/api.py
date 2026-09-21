# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""YouTube Data API v3 の薄いラッパー (docs/youtube.md)。

役割:
- YoutubeCredential から google.oauth2 Credentials を構築 (refresh token 自動回し)
- liveBroadcasts.insert / bind / transition
- liveStreams.list (status 確認)

エラー型は google-api-python-client の HttpError を素のまま投げる。タスク側で
backoff/alert を判断する。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import Resource, build

from core.models import Channel
from youtube.models import YoutubeCredential

logger = logging.getLogger(__name__)


def _credentials(channel: Channel) -> Credentials:
    """YoutubeCredential から google.oauth2 Credentials を構築。

    access_token は短期キャッシュ、必要なら refresh して updated DB に書き戻し。
    """
    cred = YoutubeCredential.objects.get(channel=channel)
    # expiry を渡さないと google-auth は「無期限」扱いで valid=True になり、失効後は毎 API 呼び出しが
    # 401→transport refresh になる (2026-09-02 監査 決定#7②)。expired 判定は naive UTC 比較なので
    # DB の aware datetime は naive UTC へ落として渡す。
    expiry = cred.token_expiry
    if expiry is not None and expiry.tzinfo is not None:
        expiry = expiry.astimezone(UTC).replace(tzinfo=None)
    google_cred = Credentials(
        token=cred.access_token,
        expiry=expiry,
        refresh_token=cred.refresh_token,
        client_id=cred.client_id,
        client_secret=cred.client_secret,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=cred.scopes.split(),
    )
    if not google_cred.valid:
        google_cred.refresh(Request())
        cred.access_token = google_cred.token
        # google-auth の expiry は naive UTC。素で保存すると USE_TZ 下で JST 解釈され 9h ずれる。
        cred.token_expiry = google_cred.expiry.replace(tzinfo=UTC) if google_cred.expiry else None
        cred.save(update_fields=["access_token", "token_expiry", "updated_at"])
    return google_cred


def yt(channel: Channel) -> Resource:
    return build("youtube", "v3", credentials=_credentials(channel), cache_discovery=False)


def watch_url(broadcast_id: str) -> str:
    """broadcast_id から視聴 (watch) URL。2h 枠は枠ごとに URL が変わる (次枠誘導の宛先)。"""
    return f"https://www.youtube.com/watch?v={broadcast_id}"


def post_live_chat_message(channel: Channel, broadcast_id: str, text: str) -> bool:
    """broadcast の YouTube ライブチャットに text を投稿する (次枠誘導)。

    liveBroadcasts.list(snippet) で liveChatId を引き、liveChatMessages.insert する。
    チャットが無効/未開設 (liveChatId 無し) や broadcast 不在なら何もせず False。投稿できたら True。
    クォータ: list=1 + insert=50。HttpError は呼び出し側 (タスク) で握る。
    """
    yc = yt(channel)
    items = yc.liveBroadcasts().list(part="snippet", id=broadcast_id).execute().get("items", [])
    if not items:
        return False
    live_chat_id = items[0].get("snippet", {}).get("liveChatId")
    if not live_chat_id:
        return False
    yc.liveChatMessages().insert(
        part="snippet",
        body={
            "snippet": {
                "liveChatId": live_chat_id,
                "type": "textMessageEvent",
                "textMessageDetails": {"messageText": text},
            }
        },
    ).execute()
    return True


def livestream_active(channel: Channel, livestream_id: str | None = None) -> bool:
    """永続 liveStream が active (RTMP 流入中) か。transition(live) 前の事前条件。

    livestream_id 省略時は rolling 枠の youtube_livestream_id を見る。番組専用枠 (#23) は
    2 本目の youtube_livestream_id_2 を渡して活性を確認する。
    """
    lid = livestream_id or channel.youtube_livestream_id
    if not lid:
        return False
    r = (
        yt(channel)
        .liveStreams()
        .list(
            part="status",
            id=lid,
        )
        .execute()
    )
    items = r.get("items", [])
    if not items:
        return False
    return items[0].get("status", {}).get("streamStatus") == "active"


def insert_broadcast(
    channel: Channel,
    *,
    title: str,
    scheduled_start: datetime,
    privacy: str,
    enable_monitor: bool,
    description: str = "",
    made_for_kids: bool = False,
    enable_dvr: bool = True,
    enable_embed: bool = True,
    enable_auto_start: bool = False,
    enable_auto_stop: bool = False,
    latency_preference: str | None = None,
    enable_closed_captions: bool = False,
    closed_captions_type: str = "closedCaptionsHttpPost",
    record_from_start: bool | None = None,
    scheduled_end: datetime | None = None,
    stream_id: str | None = None,
) -> str:
    """liveBroadcasts.insert → bind して broadcast_id を返す。

    既定値は rolling 枠 (#3) の従来挙動を保つ (enableAutoStart/Stop=false・DVR/Embed=true・
    made-for-kids=false・latency/captions/recordFromStart 未指定)。番組専用枠 (#23) は
    プリセット由来の追加引数と stream_id (2 本目 liveStream) を渡して上書きする。
    """
    snippet: dict = {
        "title": title,
        "description": description or "",
        "scheduledStartTime": scheduled_start.isoformat(),
    }
    if scheduled_end is not None:
        snippet["scheduledEndTime"] = scheduled_end.isoformat()

    content_details: dict = {
        "enableAutoStart": enable_auto_start,
        "enableAutoStop": enable_auto_stop,
        "monitorStream": {"enableMonitorStream": enable_monitor},
        "enableDvr": enable_dvr,
        # 既定 true。これが無いと watch/oEmbed は通っても埋め込みプレーヤーが
        # 「エラー153 動画プレーヤーの設定エラー」になる (公開ビューア /ch/<slug>/ の YouTube 埋め込み)。
        "enableEmbed": enable_embed,
    }
    if latency_preference:
        content_details["latencyPreference"] = latency_preference
    if enable_closed_captions:
        content_details["enableClosedCaptions"] = True
        content_details["closedCaptionsType"] = closed_captions_type
    if record_from_start is not None:
        content_details["recordFromStart"] = record_from_start

    cfg_body = {
        "snippet": snippet,
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": made_for_kids,
        },
        "contentDetails": content_details,
    }
    b = (
        yt(channel)
        .liveBroadcasts()
        .insert(
            part="snippet,status,contentDetails",
            body=cfg_body,
        )
        .execute()
    )
    broadcast_id = b["id"]
    yt(channel).liveBroadcasts().bind(
        id=broadcast_id,
        part="id",
        streamId=stream_id or channel.youtube_livestream_id,
    ).execute()
    return broadcast_id


def update_broadcast(
    channel: Channel,
    broadcast_id: str,
    *,
    title: str,
    scheduled_start: datetime,
    description: str = "",
) -> None:
    """liveBroadcasts.update で枠の snippet (タイトル/説明) を更新。

    YouTube の update は part=snippet 指定時 title と scheduledStartTime が必須のため両方送る。
    """
    yt(channel).liveBroadcasts().update(
        part="snippet",
        body={
            "id": broadcast_id,
            "snippet": {
                "title": title,
                "description": description or "",
                "scheduledStartTime": scheduled_start.isoformat(),
            },
        },
    ).execute()


def set_broadcast_embed(channel: Channel, broadcast_id: str, *, enable: bool = True) -> bool:
    """既存 broadcast の contentDetails.enableEmbed を設定する (バックフィル用)。

    insert_broadcast に enableEmbed を入れる前に作られた枠は既定 false のままで、公開ビューアの
    埋め込みプレーヤーがエラー153 になる。list で現状の writable な contentDetails を取り、
    enableEmbed を上書きして update する (boundStreamId 等の read-only 項目は送らない)。
    既に希望値なら no-op で False、変更したら True を返す。
    """
    yc = yt(channel)
    items = (
        yc.liveBroadcasts().list(part="contentDetails", id=broadcast_id).execute().get("items", [])
    )
    if not items:
        raise ValueError(f"broadcast {broadcast_id} not found")
    cd = items[0].get("contentDetails", {})
    if cd.get("enableEmbed") == enable:
        return False
    monitor = cd.get("monitorStream", {})
    yc.liveBroadcasts().update(
        part="contentDetails",
        body={
            "id": broadcast_id,
            "contentDetails": {
                "enableEmbed": enable,
                "enableAutoStart": cd.get("enableAutoStart", False),
                "enableAutoStop": cd.get("enableAutoStop", False),
                "enableDvr": cd.get("enableDvr", True),
                "monitorStream": {"enableMonitorStream": monitor.get("enableMonitorStream", False)},
            },
        },
    ).execute()
    return True


def set_broadcast_privacy(channel: Channel, broadcast_id: str, *, privacy: str) -> bool:
    """既存 broadcast の status.privacyStatus を設定する (バックフィル用)。

    insert_broadcast は YoutubeConfig.privacy で枠を作るが、過去に unlisted/private で作られた枠を
    後から公開 (public) へ揃えるためのもの。list で現状の privacyStatus を取り、希望値と違えば
    update する。selfDeclaredMadeForKids は insert と同じ False を明示し made-for-kids 指定を保つ
    (status part 更新で未指定だと既定へ戻されうるため)。
    既に希望値なら no-op で False、変更したら True を返す。
    """
    yc = yt(channel)
    items = yc.liveBroadcasts().list(part="status", id=broadcast_id).execute().get("items", [])
    if not items:
        raise ValueError(f"broadcast {broadcast_id} not found")
    if items[0].get("status", {}).get("privacyStatus") == privacy:
        return False
    yc.liveBroadcasts().update(
        part="status",
        body={
            "id": broadcast_id,
            "status": {
                "privacyStatus": privacy,
                "selfDeclaredMadeForKids": False,
            },
        },
    ).execute()
    return True


def transition_broadcast(channel: Channel, broadcast_id: str, target: str) -> None:
    """target = 'testing' | 'live' | 'complete'."""
    yt(channel).liveBroadcasts().transition(
        broadcastStatus=target,
        id=broadcast_id,
        part="status",
    ).execute()


def broadcast_lifecycle(channel: Channel, broadcast_id: str) -> str | None:
    """liveBroadcasts.list で lifeCycleStatus を返す。削除済み等で見つからなければ None。

    transition が invalidTransition で拒否されたとき、実状態から追認できるかの判定に使う。
    """
    r = yt(channel).liveBroadcasts().list(part="status", id=broadcast_id).execute()
    items = r.get("items", [])
    if not items:
        return None
    return items[0].get("status", {}).get("lifeCycleStatus")


def delete_broadcast(channel: Channel, broadcast_id: str) -> None:
    """liveBroadcasts.delete。既に削除済み (404) のときは呼び出し側で握り潰す。"""
    yt(channel).liveBroadcasts().delete(id=broadcast_id).execute()


def create_persistent_stream(channel: Channel) -> dict:
    """docs/youtube.md シーケンス1: liveStreams.insert (isReusable=True) → channel に保存。

    生成された liveStream の id / ingestionAddress / streamName を Channel に保存し、
    最新の API レスポンスを dict で返す (画面で feedback 表示する用)。

    youtube_stream_key は EncryptedTextField で DB at-rest 暗号化される (#3)。
    """
    if channel.youtube_livestream_id:
        raise ValueError(
            f"channel {channel.slug} already has youtube_livestream_id={channel.youtube_livestream_id}"
        )
    body = {
        "snippet": {"title": f"ICS-TV {channel.slug} ingest"},
        "cdn": {
            "ingestionType": "rtmp",
            # variable: 取込解像度/FPS を encoder 出力に追従させる。固定値 (旧 1080p60) だと
            # 実出力 (720p60@4.5M) が宣言値を下回り YouTube が videoIngestionStarved
            # (healthStatus=bad → 視聴者バッファリング) と誤判定する。cdn.resolution は
            # 作成後変更不可なので最初から variable にしておく。
            "resolution": "variable",
            "frameRate": "variable",
        },
        "contentDetails": {"isReusable": True},
    }
    r = yt(channel).liveStreams().insert(part="snippet,cdn,contentDetails", body=body).execute()
    ingest = r.get("cdn", {}).get("ingestionInfo", {})
    channel.youtube_livestream_id = r["id"]
    channel.youtube_ingest_url = ingest.get("ingestionAddress", "")
    channel.youtube_stream_key = ingest.get("streamName", "")
    channel.save(
        update_fields=[
            "youtube_livestream_id",
            "youtube_ingest_url",
            "youtube_stream_key",
        ],
    )
    return r


def create_dedicated_stream(channel: Channel) -> dict:
    """#23 番組専用枠用の 2 本目永続 liveStream を作成し Channel の *_2 列に保存。

    rolling 枠 (create_persistent_stream) と同型 (isReusable=True / cdn variable)。encoder は
    この 2 本目キーへ同一内容を tee で push し、専用枠を rolling 枠と並行配信する。beat が未作成
    なら自動プロビジョンする (docs/youtube.md #23)。youtube_stream_key_2 は at-rest 暗号化 (#3)。
    """
    if channel.youtube_livestream_id_2:
        raise ValueError(
            f"channel {channel.slug} already has youtube_livestream_id_2={channel.youtube_livestream_id_2}"
        )
    body = {
        "snippet": {"title": f"ICS-TV {channel.slug} dedicated ingest"},
        "cdn": {
            "ingestionType": "rtmp",
            "resolution": "variable",
            "frameRate": "variable",
        },
        "contentDetails": {"isReusable": True},
    }
    r = yt(channel).liveStreams().insert(part="snippet,cdn,contentDetails", body=body).execute()
    ingest = r.get("cdn", {}).get("ingestionInfo", {})
    channel.youtube_livestream_id_2 = r["id"]
    channel.youtube_ingest_url_2 = ingest.get("ingestionAddress", "")
    channel.youtube_stream_key_2 = ingest.get("streamName", "")
    channel.save(
        update_fields=[
            "youtube_livestream_id_2",
            "youtube_ingest_url_2",
            "youtube_stream_key_2",
        ],
    )
    return r


def set_broadcast_thumbnail(
    channel: Channel,
    broadcast_id: str,
    *,
    image_bytes: bytes,
    mime_type: str = "image/jpeg",
) -> None:
    """thumbnails.set で broadcast (= video) のカスタムサムネを差し替える。"""
    from googleapiclient.http import MediaInMemoryUpload

    media = MediaInMemoryUpload(image_bytes, mimetype=mime_type, resumable=False)
    yt(channel).thumbnails().set(videoId=broadcast_id, media_body=media).execute()


def add_to_playlist(channel: Channel, playlist_id: str, video_id: str) -> None:
    """playlistItems.insert で broadcast (= video) を再生リストへ追加。"""
    yt(channel).playlistItems().insert(
        part="snippet",
        body={
            "snippet": {
                "playlistId": playlist_id,
                "resourceId": {"kind": "youtube#video", "videoId": video_id},
            }
        },
    ).execute()


def apply_broadcast_preset(channel: Channel, broadcast_id: str, preset, *, title: str) -> None:
    """#23 プリセットの API 反映項目を broadcast (= video) へ適用する。

    insert/bind 後に呼ぶ。videos.update で snippet (categoryId/tags/言語) と status
    (license/embeddable/publicStatsViewable/madeForKids/privacy) を設定し、サムネ・再生リストを
    best-effort で追加する。サムネ/再生リストの失敗は枠自体を壊さないよう握って log に残す。

    ⚠️ videos.update の snippet 更新は categoryId 必須。preset.category_id が無いときは
    現在の categoryId を videos.list で補完し、それも無ければ snippet 更新を諦め status のみ反映する。
    """
    yc = yt(channel)

    snippet_wanted = bool(
        preset.category_id
        or preset.tags
        or preset.default_language
        or preset.default_audio_language
    )
    category_id = preset.category_id
    cur_title = title
    if snippet_wanted and category_id is None:
        items = yc.videos().list(part="snippet", id=broadcast_id).execute().get("items", [])
        if items:
            cur = items[0].get("snippet", {})
            category_id = cur.get("categoryId")
            cur_title = title or cur.get("title", "")

    body: dict = {"id": broadcast_id}
    parts = ["status"]
    body["status"] = {
        "privacyStatus": preset.privacy,
        "license": preset.license,
        "embeddable": preset.enable_embed,
        "publicStatsViewable": preset.public_stats_viewable,
        "selfDeclaredMadeForKids": preset.made_for_kids,
    }
    if snippet_wanted and category_id is not None:
        snippet: dict = {"title": cur_title, "categoryId": str(category_id)}
        if preset.tags:
            snippet["tags"] = preset.tags
        if preset.default_language:
            snippet["defaultLanguage"] = preset.default_language
        if preset.default_audio_language:
            snippet["defaultAudioLanguage"] = preset.default_audio_language
        body["snippet"] = snippet
        parts.insert(0, "snippet")
    elif snippet_wanted:
        logger.warning(
            "apply_broadcast_preset %s: categoryId 不明のため snippet 更新を skip", broadcast_id
        )

    yc.videos().update(part=",".join(parts), body=body).execute()

    if preset.thumbnail_id:
        try:
            from core import r2

            asset = preset.thumbnail
            image_bytes, mime = r2.get_object(asset.r2_key)
            set_broadcast_thumbnail(channel, broadcast_id, image_bytes=image_bytes, mime_type=mime)
        except Exception:
            logger.warning(
                "apply_broadcast_preset %s: サムネ適用に失敗", broadcast_id, exc_info=True
            )

    if preset.playlist_id:
        try:
            add_to_playlist(channel, preset.playlist_id, broadcast_id)
        except Exception:
            logger.warning(
                "apply_broadcast_preset %s: 再生リスト追加に失敗", broadcast_id, exc_info=True
            )
