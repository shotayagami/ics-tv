# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import models

from core.fields import EncryptedTextField


def _parse_hhmm(s: str | None) -> time | None:
    """ "HH:MM" を datetime.time に。不正/None は None (壊れた設定で送出を止めない)。"""
    try:
        h, m = str(s).split(":")
        return time(int(h), int(m))
    except (ValueError, AttributeError):
        return None


def _parse_hhmm_broadcast_end(s: str | None) -> tuple[time | None, bool]:
    """放送窓の終端 "HH:MM" → (time, is_next_midnight)。

    "24:00" は翌日の 00:00 を意味し is_next_midnight=True を返す。
    """
    try:
        parts = str(s).split(":")
        h_int, m_int = int(parts[0]), int(parts[1])
        if h_int == 24 and m_int == 0:
            return time(0, 0), True
        return time(h_int, m_int), False
    except (ValueError, AttributeError, IndexError):
        return None, False


class Channel(models.Model):
    """配信チャンネル(最大4)。YouTube/CF Live Input/送出ノードの紐付け。"""

    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=100, unique=True)
    enabled = models.BooleanField(default=True)
    # 公開フロント (#7 デザイン刷新) 用の見た目メタ。
    # short=略称 (例「総合」)、tint=識別色 hex (例 "#e63946")。未設定時は property が graceful default を返す。
    short = models.CharField(max_length=20, blank=True, default="")
    tint = models.CharField(max_length=7, blank=True, default="")
    # 配信先
    cf_live_input_id = models.CharField(max_length=200, blank=True, null=True)
    # 視聴者向け CF Stream HLS 再生 URL (#7 Phase 2 プレイヤー)。CF ダッシュボード/API の値を設定。
    cf_playback_hls_url = models.CharField(max_length=500, blank=True, null=True)
    youtube_channel_id = models.CharField(max_length=200, blank=True, null=True)
    youtube_stream_key = EncryptedTextField(blank=True, null=True)  # #3 DB at-rest 暗号化
    youtube_livestream_id = models.CharField(max_length=200, blank=True, null=True)
    youtube_ingest_url = models.CharField(max_length=500, blank=True, null=True)
    # #23 番組専用枠用の 2 本目永続 liveStream。rolling 枠 (上記) と並行配信するため、encoder が
    # 同一内容を tee で push する別 RTMP キー。専用枠は時間で重ならないので全番組でこの 1 本を共有。
    youtube_livestream_id_2 = models.CharField(max_length=200, blank=True, null=True)
    youtube_ingest_url_2 = models.CharField(max_length=500, blank=True, null=True)
    youtube_stream_key_2 = EncryptedTextField(blank=True, null=True)  # #3 DB at-rest 暗号化
    # agent (クラウド送出ノード) からの gRPC Bearer。secrets.token_urlsafe(32) で生成想定。
    # #3 暗号化保管。暗号文は非決定的なので WHERE 照合不可 → 認証は slug で引いて復号比較
    # (grpc_service._verify_token)。
    agent_token = EncryptedTextField(blank=True, null=True, unique=True)
    default_filler = models.ForeignKey(
        "medialib.FillerPlaylist",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    # チャンネル別スレート (緊急退避/feed 断時に layer90 へ出す素材)。正規化済 Asset を指す。
    # agent は prefetch manifest 経由でこれを pre-cache+pin し、未設定時はノードローカルの
    # slate/please_wait にフォールバックする (config.slate_clip)。
    slate_asset = models.ForeignKey(
        "medialib.Asset",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    # exposure_policy(#27, docs/site-only-broadcast.md §4.2/§4.5): YTミラー用の案内フィラー素材。
    # site_only_filler=公開ミラー用(「本編は会員限定/本サイトで配信中」の案内)、
    # members_filler=メンバーミラー用(「次のメンバー限定番組まで」の待機画)。
    site_only_filler = models.ForeignKey(
        "medialib.Asset",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    members_filler = models.ForeignKey(
        "medialib.Asset",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    # 朝・夕の左上時計オーバーレイ (daypart corner clock)。日本のTVの朝夕の時計表示。
    # enabled で有効化 (opt-in。本番配信中に不意に出ないよう既定 False)。windows は表示時間帯
    # [{"start":"04:30","end":"08:00"}, …] (JST)。空なら settings.ICSTV_CLOCK_WINDOWS を使う。
    clock_overlay_enabled = models.BooleanField(default=False)
    clock_windows = models.JSONField(default=list, blank=True)
    # 時計オーバーレイのスタイル設定 (フォント/色/エフェクト/アニメーション等)。
    # {} = 全デフォルト。各キーは corner.html の applyStyle() が解釈する。
    clock_style = models.JSONField(default=dict, blank=True)
    # 放送時間帯 (休止機能)。[{"start":"HH:MM","end":"HH:MM"}, …] (JST)。
    # 未設定 ([]) = 24時間放送。end="24:00" は翌日 00:00 (深夜終了)。
    # resolver がこの窓外を PLAY_SLATE (休止スレート) で埋め、窓内を通常編成で解決する。
    # フィラーは窓を跨いでも同一プレイリストの続きから再開する (off-air 時間は経過カウント外)。
    broadcast_windows = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "channel"

    def __str__(self):
        return f"{self.name} ({self.slug})"

    # 公開フロントが未設定 ch でも色/略称を持てるよう graceful default を返す (#7 デザイン刷新)。
    _TINT_PALETTE = (
        "#e63946",  # 赤 (総合)
        "#1982c4",  # 青 (教育)
        "#2a9d8f",  # 緑
        "#f4a261",  # 橙
        "#7209b7",  # 紫
        "#457b9d",  # 青灰
    )

    @property
    def tint_color(self) -> str:
        """識別色。未設定なら slug の安定ハッシュでパレットから割り当てる。"""
        if self.tint:
            return self.tint
        idx = sum(ord(c) for c in self.slug) % len(self._TINT_PALETTE)
        return self._TINT_PALETTE[idx]

    @property
    def short_name(self) -> str:
        """略称。未設定なら表示名で代替。"""
        return self.short or self.name

    @property
    def effective_clock_windows(self) -> list[tuple[time, time]]:
        """有効な時計表示時間帯 (JST) の (start, end) ペア列。

        clock_windows 未設定なら settings.ICSTV_CLOCK_WINDOWS にフォールバック。不正な要素は除外。
        end<=start の窓は呼び出し側 (resolver) が翌日跨ぎとして扱う。
        """
        return [(s, e) for s, e, _ in self.effective_clock_windows_with_style]

    @property
    def effective_broadcast_windows(self) -> list[tuple[time, time, bool]]:
        """有効な放送時間帯 (JST) の (start, end, end_is_next_midnight) タプル列。

        空なら 24 時間放送 (未設定)。end_is_next_midnight=True は end="24:00" を意味する。
        """
        out: list[tuple[time, time, bool]] = []
        for w in self.broadcast_windows or []:
            if not isinstance(w, dict):
                continue
            start = _parse_hhmm(w.get("start"))
            end_t, is_midnight = _parse_hhmm_broadcast_end(w.get("end"))
            if start is not None and end_t is not None:
                out.append((start, end_t, is_midnight))
        return out

    def _bw_day_intervals(self, dt: datetime) -> list[tuple[datetime, datetime]]:
        """dt の日付における broadcast_windows を datetime ペアに展開。内部ヘルパー。"""
        tz = ZoneInfo(settings.TIME_ZONE)
        date_val = dt.astimezone(tz).date()
        result = []
        for w_start, w_end, is_midnight in self.effective_broadcast_windows:
            on_s = datetime.combine(date_val, w_start, tzinfo=tz)
            on_e = (
                datetime.combine(date_val + timedelta(days=1), w_end, tzinfo=tz)
                if is_midnight
                else datetime.combine(date_val, w_end, tzinfo=tz)
            )
            result.append((on_s, on_e))
        return result

    def is_on_air(self, dt: datetime) -> bool:
        """dt が放送時間帯内かどうか。broadcast_windows 未設定なら常に True。"""
        if not self.effective_broadcast_windows:
            return True
        return any(on_s <= dt < on_e for on_s, on_e in self._bw_day_intervals(dt))

    def slot_has_on_air(self, w_start: datetime, w_end: datetime) -> bool:
        """スロット [w_start, w_end) が放送時間帯と重なるかどうか。重なり 0 は False。"""
        windows = self.effective_broadcast_windows
        if not windows:
            return True
        tz = ZoneInfo(settings.TIME_ZONE)
        start_date = w_start.astimezone(tz).date()
        end_date = (w_end.astimezone(tz) + timedelta(days=1)).date()
        d = start_date
        while d <= end_date:
            for w_s, w_e, is_midnight in windows:
                on_s = datetime.combine(d, w_s, tzinfo=tz)
                on_e = (
                    datetime.combine(d + timedelta(days=1), w_e, tzinfo=tz)
                    if is_midnight
                    else datetime.combine(d, w_e, tzinfo=tz)
                )
                if on_s < w_end and on_e > w_start:
                    return True
            d += timedelta(days=1)
        return False

    def broadcast_intervals(self, t0: datetime, t1: datetime) -> list[tuple[datetime, datetime]]:
        """[t0, t1) と重なる放送時間帯を (on_s, on_e) の実時刻区間として時刻順に返す。

        未設定 (24h) なら空リスト。generate_slots が固定 step グリッドでなく実際の放送
        時間帯を起点に枠を刻めるようにする (grid 境界が時間帯途中を横切って 1 本の放送が
        分断されるのを防ぐ)。
        """
        windows = self.effective_broadcast_windows
        if not windows:
            return []
        tz = ZoneInfo(settings.TIME_ZONE)
        start_date = t0.astimezone(tz).date()
        end_date = t1.astimezone(tz).date()
        out: list[tuple[datetime, datetime]] = []
        d = start_date
        while d <= end_date:
            for w_s, w_e, is_midnight in windows:
                on_s = datetime.combine(d, w_s, tzinfo=tz)
                on_e = (
                    datetime.combine(d + timedelta(days=1), w_e, tzinfo=tz)
                    if is_midnight
                    else datetime.combine(d, w_e, tzinfo=tz)
                )
                if on_s < t1 and on_e > t0:
                    out.append((on_s, on_e))
            d += timedelta(days=1)
        out.sort()
        return out

    def next_on_air(self, dt: datetime) -> datetime | None:
        """dt 以降で最初に放送が始まる時刻。broadcast_windows 未設定 / dt が放送中なら None。

        休止 (窓外) の公開表示で「次回◯時から放送」を出すために使う。dt 自体が窓の谷 (休止中)
        にある前提。翌々日までの窓を走査し、開始が dt より後の最初の窓先頭を返す。
        """
        if not self.effective_broadcast_windows or self.is_on_air(dt):
            return None
        for on_s, _on_e in self.broadcast_intervals(dt, dt + timedelta(days=2)):
            if on_s > dt:
                return on_s
        return None

    @property
    def effective_clock_windows_with_style(self) -> list[tuple[time, time, dict | None]]:
        """有効な時計表示時間帯 + 時間帯スタイル上書き の 3 要素タプル列。

        各要素: (start, end, style_or_none)。style は time window 単位の clock_style 上書き
        (None = 上書きなし = チャンネルデフォルト使用)。
        """
        raw = self.clock_windows or getattr(settings, "ICSTV_CLOCK_WINDOWS", [])
        out: list[tuple[time, time, dict | None]] = []
        for w in raw or []:
            if not isinstance(w, dict):
                continue
            start = _parse_hhmm(w.get("start"))
            end = _parse_hhmm(w.get("end"))
            if start is not None and end is not None:
                style = w.get("style") if isinstance(w.get("style"), dict) else None
                out.append((start, end, style))
        return out


class ChimeCategory(models.TextChoices):
    """速報チャイムの種別 (速報テロップ layer40 と同時に layer41 で鳴らす効果音)。"""

    EEW = "eew", "EEW (緊急)"
    WEATHER = "weather", "気象"
    GENERAL = "general", "その他"
    # 生 CM 入りに鳴らす任意チャイム (タイムキープ Phase3 D4 §5.3)。fire_cm_bundle/fire_cm_dynamic
    # が category="cm_in" で鳴らす。この選択肢が無いと studio で音源を割り当てられず恒久無音になる。
    CM_IN = "cm_in", "CM入り"


class ChimeSound(models.Model):
    """速報チャイム音源ライブラリ (局共通)。Web からアップロードして貯めておく音源プール。

    カテゴリ別の選択 (ChannelChime) や手動速報のその場選択がここから音源を指す。Asset/normalize は
    通さず、アップロードした音声を **そのまま R2 に保存** する (短い効果音に 1080p 動画正規化は不要)。
    r2_key は uuid 付き content-addressed で、clip 名 (= 拡張子を除いた r2_key) も一意になるため、
    送出ノードの MediaCache が古い音を使い続ける問題 (ensure は既存ファイルがあれば再DLしない) を
    避けられる。配布は standing prefetch manifest 経由でライブラリ全体を agent が pin+DL する
    (grpc_service._build_prefetch_manifest)。送出は core.views.fire_chime が clip を解決して PLAY 1-41。
    """

    name = models.CharField(max_length=120)
    r2_key = models.CharField(max_length=255)  # chime/lib/<uuid>.<ext>
    original_filename = models.CharField(max_length=255, blank=True, default="")
    content_type = models.CharField(max_length=64, blank=True, default="")
    size_bytes = models.BigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "chime_sound"
        ordering = ["name", "id"]

    def __str__(self):
        return self.name

    @property
    def clip(self) -> str:
        """CasparCG の clip 名 (媒体フォルダ相対・拡張子なし)。r2_key と 1:1。
        agent は media_dir/<clip><ext> に DL し、CasparCG は basename で解決する。"""
        return self.r2_key.rsplit(".", 1)[0]


class ChannelChime(models.Model):
    """カテゴリ別チャイム選択 (per-channel)。ライブラリ ChimeSound のどれを使うかを指す。

    速報テロップ (外部/手動) は発火時のカテゴリ (eew|weather|general) からこの選択を引き、選ばれた
    ChimeSound の clip を layer41 で PLAY する。差し替え = ライブラリから別の音源を選び直すだけ
    (再アップロード不要)。sound 未設定なら settings.CHIME_CLIPS の既定 clip にフォールバックする。
    """

    channel = models.ForeignKey(Channel, on_delete=models.CASCADE, related_name="chimes")
    category = models.CharField(max_length=16, choices=ChimeCategory.choices)
    sound = models.ForeignKey(
        ChimeSound, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "channel_chime"
        constraints = [
            models.UniqueConstraint(
                fields=["channel", "category"], name="uniq_channel_chime_category"
            )
        ]

    def __str__(self):
        return f"{self.channel.slug}:{self.category}"


class LiveSource(models.Model):
    """生入力ソース(送出ノード = Proxmox の LXC コンテナ上の MediaMTX でローカル終端)。

    外部(現場 OBS)→ Cloudflare One(cloudflared private network + WARP)→ 送出ノード
    MediaMTX へ SRT/RTMP push(overview.md 決定#21)。agent は MediaMTX が終端した
    rtmp://127.0.0.1:1935/<rtmp_app>/<rtmp_key> をローカル参照するだけで(agent/CasparCG
    無改修)、srt_* は現場へ渡す外部 ingest URL の生成にのみ使う。
    """

    name = models.CharField(max_length=200)
    rtmp_app = models.CharField(max_length=200)
    rtmp_key = models.CharField(max_length=200)
    # SRT ingest(決定#21)。現場へ渡す ingest URL の passphrase/latency。MediaMTX は
    # streamid=publish:<rtmp_app>/<rtmp_key> で RTMP と同一 path に終端するため app/key を流用。
    # passphrase は秘密値なので #3 at-rest 暗号化(EncryptedTextField)。
    srt_passphrase = EncryptedTextField(blank=True, null=True)
    srt_latency_ms = models.PositiveIntegerField(default=2000)
    note = models.TextField(blank=True, null=True)

    class Meta:
        db_table = "live_source"

    def __str__(self):
        return self.name

    @property
    def mediamtx_path(self) -> str:
        """MediaMTX path 名(= SRT streamid publish:<path> / ローカル参照 path)。"""
        return f"{self.rtmp_app}/{self.rtmp_key}"

    def srt_ingest_url(self, host: str = "<送出ノード private IP>") -> str:
        """現場 OBS に渡す SRT push URL。host は WARP 経由で到達する送出ノードの private IP。

        streamid/passphrase は SRT クライアント(OBS/ffmpeg)がそのまま解釈できるよう
        percent-encode せず literal で組む(`publish:<app>/<key>`)。
        """
        parts = [f"streamid=publish:{self.mediamtx_path}", f"latency={self.srt_latency_ms}"]
        if self.srt_passphrase:
            parts.append(f"passphrase={self.srt_passphrase}")
        return f"srt://{host}:8890?" + "&".join(parts)

    def rtmp_ingest_url(self, host: str = "<送出ノード private IP>") -> str:
        """RTMP 直 push URL(LAN/WG 内サブ卓向け。現場 last-mile は SRT 推奨)。"""
        return f"rtmp://{host}:1935/{self.mediamtx_path}"


class NotificationSeverity(models.TextChoices):
    CRIT = "crit", "crit"
    WARN = "warn", "warn"
    INFO = "info", "info"


class Notification(models.Model):
    """障害/運用アラート (docs/operations.md・ui.md 通知パネルのデータ源)。

    notifier (core.notify) が INSERT し、登録バックエンド (webhook 等) へ配送する。
    feed_lost/slate_on/feed_restored/auto_return_suspended/agent_offline/agent_recovered/
    playout_failed/cm_stock_low/yt_transition_failed などを kind に持つ (#7 O3)。
    """

    channel = models.ForeignKey(
        Channel,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="notifications",
    )
    severity = models.CharField(max_length=4, choices=NotificationSeverity.choices)
    kind = models.CharField(max_length=40)
    message = models.TextField()
    link_url = models.CharField(max_length=500, blank=True, null=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "notification"
        indexes = [
            models.Index(
                fields=["created_at"],
                condition=models.Q(acknowledged_at__isnull=True),
                name="idx_notification_unack",
            ),
        ]

    def __str__(self):
        return f"[{self.severity}] {self.kind}"


class ClockStylePreset(models.Model):
    """時計スタイルプリセット。チャンネル共通の再利用可能な時計外観定義。"""

    name = models.CharField(max_length=100, verbose_name="プリセット名")
    style = models.JSONField(default=dict, verbose_name="スタイル辞書")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        db_table = "clock_style_preset"
        verbose_name = "時計スタイルプリセット"
        verbose_name_plural = "時計スタイルプリセット"

    def __str__(self) -> str:
        return self.name
