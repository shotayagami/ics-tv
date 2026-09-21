# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
from django.db import models

from core.fields import EncryptedTextField


class YtSlotStatus(models.TextChoices):
    CREATED = "created", "created"
    READY = "ready", "ready"
    TESTING = "testing", "testing"
    LIVE = "live", "live"
    COMPLETE = "complete", "complete"
    ERROR = "error", "error"


class YtPrivacy(models.TextChoices):
    PUBLIC = "public", "public"
    UNLISTED = "unlisted", "unlisted"
    PRIVATE = "private", "private"


class YtLatency(models.TextChoices):
    """liveBroadcasts.contentDetails.latencyPreference。"""

    NORMAL = "normal", "normal"
    LOW = "low", "low"
    ULTRA_LOW = "ultraLow", "ultraLow"


class YtLicense(models.TextChoices):
    """videos.update status.license。"""

    YOUTUBE = "youtube", "標準の YouTube ライセンス"
    CREATIVE_COMMON = "creativeCommon", "クリエイティブ・コモンズ"


# 手動チェックリストの既定項目 (#23)。Studio UI 専用で YouTube Data API では設定できないため、
# プリセットには「定義 + 既定値」を持ち、配信ごとのチェック状態は ProgramBroadcast.checklist_state に残す。
def default_manual_checklist() -> list[dict]:
    return [
        {"key": "paid_promotion", "label": "有料プロモーションの申告", "default": False},
        {"key": "ai_disclosure", "label": "AI 使用の開示", "default": False},
        {"key": "age_restriction", "label": "年齢制限 (18歳以上)", "default": False},
        {"key": "caption_certification", "label": "字幕の認定", "default": False},
        {"key": "auto_chapters", "label": "チャプターの自動生成", "default": True},
        {"key": "featured_places", "label": "注目の場所", "default": True},
        {"key": "auto_concepts", "label": "コンセプトの自動説明", "default": True},
        {"key": "chat_translation", "label": "チャットの翻訳", "default": True},
        {"key": "chat_summary", "label": "チャットの要約", "default": True},
        {"key": "chat_ranking", "label": "チャットのランキング", "default": True},
        {"key": "participant_mode", "label": "参加者モード", "default": False},
        {"key": "slow_mode", "label": "低速モード", "default": False},
        {"key": "redirect", "label": "リダイレクト設定", "default": False},
        {"key": "dual_stream", "label": "デュアルストリーム (縦型ショート)", "default": False},
    ]


class YoutubeSlot(models.Model):
    """YouTube 2h枠。永続 liveStream の上で rolling 生成・transition される liveBroadcast。"""

    channel = models.ForeignKey(
        "core.Channel",
        on_delete=models.CASCADE,
        related_name="youtube_slots",
    )
    window_start = models.DateTimeField()
    window_end = models.DateTimeField()  # = window_start + slot_minutes
    broadcast_id = models.CharField(max_length=64, blank=True, null=True)
    status = models.CharField(
        max_length=12,
        choices=YtSlotStatus.choices,
        default=YtSlotStatus.CREATED,
    )
    title = models.CharField(max_length=300, blank=True, null=True)
    description = models.TextField(blank=True, null=True)  # YouTube キャプション (#7 枠メタ)
    # True = タイトル/説明を人が編集した枠。自動生成で上書きしない目印 (生成は既存枠を skip するため実質保持)
    manual = models.BooleanField(default=False)
    # 次枠誘導 (この枠のライブチャット投稿 + 説明欄追記) を実施済みの目印。1 枠 1 回。
    # 1 分 beat の nudge_next_slot がチャットを重複投稿しないためのガード。
    next_nudged = models.BooleanField(default=False)
    # complete 遷移後に説明文を「終了しています」版 (nudge_ended_template) へ差し替え済みの目印。1 枠 1 回。
    ended_nudged = models.BooleanField(default=False)
    error = models.TextField(blank=True, null=True)

    class Meta:
        db_table = "youtube_slot"
        constraints = [
            models.UniqueConstraint(
                fields=["channel", "window_start"], name="uq_youtube_slot_window"
            ),
        ]
        ordering = ["channel", "window_start"]


class YoutubeCredential(models.Model):
    """OAuth 認証情報 (チャンネル所有者の同意; 書き込み API に必須)。"""

    channel = models.OneToOneField(
        "core.Channel",
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="youtube_credential",
    )
    client_id = models.CharField(max_length=300)
    client_secret = EncryptedTextField()  # #3 DB at-rest 暗号化
    refresh_token = EncryptedTextField()  # #3 DB at-rest 暗号化
    scopes = models.CharField(
        max_length=300,
        default="https://www.googleapis.com/auth/youtube.force-ssl",
    )
    access_token = models.CharField(max_length=1000, blank=True, null=True)
    token_expiry = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "youtube_credential"


class YoutubeConfig(models.Model):
    """枠生成テンプレート/設定 (WebUI で編集)。"""

    channel = models.OneToOneField(
        "core.Channel",
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="youtube_config",
    )
    # 枠タイトル。{channel}=チャンネル名(Channel.name)、{date}/{start}/{end} は JST。マルチch でch名を含める。
    title_template = models.CharField(max_length=300, default="{channel} {date} {start}-{end}")
    # 枠の説明欄テンプレート。{channel}=ch名、{date}/{start}/{end} は JST、{programs} はその窓の番組リスト (無ければ空)。
    # 空欄なら説明なし。番組が無い枠でも本文を必ず書き込み YouTube デフォルト説明に頼らない (#7)。
    description_template = models.TextField(
        blank=True,
        default="ICS-TV {date} {start}-{end} (JST) の配信枠です。\n\n{programs}",
    )
    privacy = models.CharField(max_length=12, choices=YtPrivacy.choices, default=YtPrivacy.PUBLIC)
    enable_monitor = models.BooleanField(default=False)  # False=created→live 直行
    rolling_hours = models.IntegerField(default=24)
    slot_minutes = models.IntegerField(default=240)  # 1枠=4h
    # 次枠誘導: live 枠の終了 nudge_lead_minutes 分前に、その枠のライブチャットへ次枠 watch URL を投稿し
    # 説明欄先頭にも同文を追記する。2h 枠は watch URL が枠ごとに変わる (YouTube 仕様。docs/youtube.md)
    # ため、直接視聴している視聴者を次枠へ送り届ける。0 分なら誘導を無効化。
    nudge_lead_minutes = models.IntegerField(default=10)
    # 予告中 (live・終了 lead 分前) の誘導文。{url}=次枠 watch URL、{start}/{end}=次枠の開始/終了 (JST HH:MM)。
    nudge_template = models.TextField(
        default="まもなくこの配信は終了します。続きは次の配信でご覧ください ▶ {url}",
    )
    # 移動後 (枠が complete に遷移した後) の誘導文。アーカイブ視聴者向けに説明欄を差し替える。
    # 空なら移動後の差し替えをしない (予告文のまま残す)。プレースホルダは nudge_template と同じ。
    nudge_ended_template = models.TextField(
        default="この配信は終了しています。続きは次の配信でご覧ください ▶ {url}",
    )
    default_thumb = models.ForeignKey(
        "medialib.Asset",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        db_table = "youtube_config"


class YoutubeBroadcastPreset(models.Model):
    """再利用可能な配信プリセット (#23)。Studio の配信設定を ICS-TV に登録し番組専用枠で流用する。

    YouTube Data API で反映できる項目 (下記フィールド) と、API では設定できず Studio UI でしか
    操作できない項目 (manual_checklist の定義) に分けて持つ。後者は配信ごとに ProgramBroadcast へ
    チェック状態を残す運用 (docs/youtube.md #23)。"""

    name = models.CharField(max_length=100)
    # null = 全チャンネル共通プリセット。特定 ch 専用にする場合のみ紐付ける。
    channel = models.ForeignKey(
        "core.Channel",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="broadcast_presets",
    )

    # ---- API 反映群 (liveBroadcasts.insert/update + videos.update + thumbnails.set + playlistItems.insert) ----
    # title/description テンプレ。{program}/{date}/{start}/{end}/{channel} を展開 (apply 時に整形)。
    title_template = models.CharField(max_length=300, blank=True, default="")
    description_template = models.TextField(blank=True, default="")
    category_id = models.IntegerField(null=True, blank=True)  # videos.update snippet.categoryId
    tags = models.JSONField(default=list, blank=True)  # snippet.tags (list[str])
    privacy = models.CharField(max_length=12, choices=YtPrivacy.choices, default=YtPrivacy.PUBLIC)
    made_for_kids = models.BooleanField(default=False)  # selfDeclaredMadeForKids
    default_language = models.CharField(
        max_length=10, blank=True, default=""
    )  # snippet.defaultLanguage
    default_audio_language = models.CharField(max_length=10, blank=True, default="")
    latency = models.CharField(max_length=10, choices=YtLatency.choices, default=YtLatency.LOW)
    enable_dvr = models.BooleanField(default=True)
    enable_embed = models.BooleanField(default=True)
    enable_auto_start = models.BooleanField(default=False)
    enable_auto_stop = models.BooleanField(default=False)
    record_from_start = models.BooleanField(default=True)
    license = models.CharField(max_length=16, choices=YtLicense.choices, default=YtLicense.YOUTUBE)
    public_stats_viewable = models.BooleanField(default=True)
    thumbnail = models.ForeignKey(
        "medialib.Asset",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    playlist_id = models.CharField(max_length=64, blank=True, default="")  # 追加先 再生リスト

    # ---- API 不可分群 (Studio 専用 → 手動チェックリストの定義 + 既定値) ----
    manual_checklist = models.JSONField(default=default_manual_checklist, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "youtube_broadcast_preset"
        ordering = ["channel", "name"]

    def __str__(self) -> str:
        return self.name

    def initial_checklist_state(self) -> dict:
        """manual_checklist の既定値から、配信ごとに残すチェック状態 {key: bool} を作る。"""
        return {c["key"]: bool(c.get("default")) for c in (self.manual_checklist or [])}


class YoutubeDescriptionTemplate(models.Model):
    """preset の title/description テンプレートをあらかじめ登録するマスタ (チャンネル非依存)。"""

    name = models.CharField(max_length=100)
    title_template = models.CharField(max_length=300, blank=True, default="{program} | {channel}")
    description_template = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "youtube_description_template"
        ordering = ["name"]

    def __str__(self):
        return self.name


class ProgramBroadcast(models.Model):
    """#23 番組単位の専用 liveBroadcast。rolling の YoutubeSlot とは別系統。

    本番稼働中の rolling 経路 (YoutubeSlot / generate_slots / rotate_slots) には手を入れず、
    api.py の primitive (insert_broadcast / transition / apply_broadcast_preset) だけ共有して隔離する。
    1 番組 1 専用枠 (program は OneToOne)。status は YtSlotStatus を流用。"""

    program = models.OneToOneField(
        "scheduling.Program",
        on_delete=models.CASCADE,
        related_name="youtube_broadcast",
    )
    preset = models.ForeignKey(
        YoutubeBroadcastPreset,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    broadcast_id = models.CharField(max_length=64, blank=True, null=True)
    status = models.CharField(
        max_length=12,
        choices=YtSlotStatus.choices,
        default=YtSlotStatus.CREATED,
    )
    # 配信ごとの手動チェック状態 {key: bool}。preset.manual_checklist の既定値から初期化し、
    # studio で配信ごとに上書きする (API では設定できない Studio 専用項目の運用記録)。
    checklist_state = models.JSONField(default=dict, blank=True)
    manual = models.BooleanField(default=False)  # 手動ボタンで作成した枠
    error = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "program_broadcast"
        ordering = ["-created_at"]
