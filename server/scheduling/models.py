# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
from typing import TYPE_CHECKING

from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateTimeRangeField, RangeOperators
from django.db import models
from django.db.models import F, Func, Q

from medialib.models import CmGrid


class ProgramType(models.TextChoices):
    RECORDED = "recorded", "録画"
    LIVE = "live", "生"


class VodVisibility(models.TextChoices):
    """見逃し配信(VOD)の公開範囲 (#VOD-01)。既定 off=権利配慮の opt-in。

    放送済み録画番組の正規化メザニンをオンデマンド再生する際の出し分け軸。
    subscribers は会員限定コンテンツ(feat_exclusive)の実体。
    fanclub は #27 ファンクラブのティア軸 (Program.fc_required_level と併用、
    NULL=完全公開/0=無料会員以上/n=有料ティアn以上。判定は fanclub.services 側)。
    """

    OFF = "off", "公開しない"
    PUBLIC = "public", "全員"
    MEMBERS = "members", "会員限定"
    SUBSCRIBERS = "subscribers", "サブスク限定"
    FANCLUB = "fanclub", "ファンクラブ限定"


class ExposurePolicy(models.TextChoices):
    """生放送の配信ポリシー (#27、docs/site-only-broadcast.md §1.2)。番組単位で3面の挙動を導出する
    プリセット1択(チェックボックス群にしない=無意味な組合せを排除)。

    | プリセット | YouTube本線(公開) | YouTubeメンバー限定 | サイトHLS |
    |---|---|---|---|
    | public (既定) | 本編 | ― | 公開 |
    | site_public | フィラー | ― | 公開(誰でも) |
    | site_members | フィラー | ― | 会員限定 |
    | members_yt_site | フィラー | 本編 | 会員限定 |

    「サイト会員」は subscriptions.MemberSubscription (Stripeサイト全体課金)であり、
    fanclub の Creator ティアとは別軸(§1.1)。解錠はプラットフォーム別、横断連携はしない。
    """

    PUBLIC = "public", "全公開"
    SITE_PUBLIC = "site_public", "サイト限定・公開"
    SITE_MEMBERS = "site_members", "サイト会員限定"
    MEMBERS_YT_SITE = "members_yt_site", "会員限定(YT+サイト)"


class Genre(models.TextChoices):
    """番組ジャンル (#7 公開フロント刷新)。値=表示文字列で公開フロントの色マップ (core.genre) のキーと一致。

    未設定 (空) を許容し、その場合は公開側でチップ非表示・枠線は ch 識別色へフォールバックする。
    """

    NEWS = "ニュース", "ニュース"
    INFO = "情報", "情報"
    ANIME = "アニメ", "アニメ"
    MOVIE = "映画", "映画"
    MUSIC = "音楽", "音楽"
    GAME = "ゲーム", "ゲーム"
    VARIETY = "バラエティ", "バラエティ"
    CULTURE = "教養", "教養"
    ARTS = "文化", "文化"
    SCIENCE = "科学", "科学"
    TECH = "技術", "技術"
    LANG = "語学", "語学"
    DOCUMENTARY = "ドキュメンタリー", "ドキュメンタリー"


class ContentRating(models.TextChoices):
    """視聴年齢制限 (#BILL-02)。空(未設定)=全年齢。R15/R18 はハードゲート、PG12 は助言表示のみ。

    会員登録で収集済みの birth_year/birth_month から満年齢を出し、min_age 以上のみ視聴可。
    リニア(共有配信)は視聴者別ブロック不可のため、ハードゲートは自前再生制御が効く
    VOD(scheduling.vod)に効く。番組詳細等はバッジ表示で補う (scheduling.ratings)。
    """

    PG12 = "pg12", "PG12"
    R15 = "r15", "R15+"
    R18 = "r18", "R18+"


# rating → 最低視聴年齢(歳)。空/PG12 は 0 = ハードゲート無し(PG12 はバッジ表示のみ)。
# str キー注釈: resolved_rating(str) でルックアップする (TextChoices は str と等価)。
RATING_MIN_AGE: dict[str, int] = {ContentRating.R15: 15, ContentRating.R18: 18}


class TsTzRange(Func):
    """tstzrange(start, end) — 既定境界 '[)' なので隣接(終端=次の開始)は重複扱いにならない。"""

    function = "TSTZRANGE"
    output_field = DateTimeRangeField()


class Series(models.Model):
    """レギュラー番組(番組マスタ)。タイム契約/指定番組割付の対象 (#6 sales)。

    Phase A は series マスタ + program.series のみ。週間パターン(series_slot)と自動展開は Phase C。
    series.channel と program.channel の一致は Form/clean で検証する。
    """

    channel = models.ForeignKey("core.Channel", on_delete=models.PROTECT, related_name="series")
    title = models.CharField(max_length=300)
    # 番組固有の公開 URL スラッグ (任意)。空=従来の /series/<id>/ で動作 (後方互換)。
    # チャンネル内で一意 (空は重複可)。ASCII 英数 (-_) のみ。作成時に title から自動採番を試みる。
    slug = models.SlugField(max_length=80, blank=True, default="")
    # 公式 X (Twitter) アカウント。先頭 @ は表示時に付与 (保存値は @ 有無どちらでも可)。
    x_handle = models.CharField(max_length=40, blank=True, default="")
    # 番組ハッシュタグ。先頭 # は表示時に付与。リスナー投稿/シェア用 (hicbc 参考)。
    x_hashtag = models.CharField(max_length=80, blank=True, default="")
    description = models.TextField(blank=True, null=True)
    # ジャンル (#7 公開フロント)。展開 Program の既定ジャンルになる。空=未設定。
    genre = models.CharField(max_length=20, choices=Genre.choices, blank=True, default="")
    # 視聴年齢制限 (#BILL-02)。展開 Program の既定レーティングになる。空=全年齢。
    rating = models.CharField(max_length=8, choices=ContentRating.choices, blank=True, default="")
    is_active = models.BooleanField(default=True)
    # 番組の固定サムネ画像 URL (任意。#7 Phase 2 ①)。展開した Program のサムネ既定にもなる
    thumbnail_url = models.CharField(max_length=500, blank=True, null=True)
    # 出演者 (#EPG-01)。カンマ/改行区切りの自由記述。展開 Program の既定出演者にもなる。
    cast = models.TextField(blank=True, null=True)
    # 朝・夕の左上時計 (daypart) を、時間帯内でもこのシリーズの全回で出さない。映画等の opt-out。
    clock_hidden = models.BooleanField(default=False, verbose_name="朝・夕の時計を表示しない")
    # 時計スタイル上書き。null = チャンネルデフォルト使用。{} = チャンネルの全デフォルト値で明示上書き。
    # {key: val} = 指定キーのみ上書き (他はチャンネルデフォルトにマージ)。
    clock_style_override = models.JSONField(null=True, blank=True, default=None)
    # Lバー (下L字) をこのシリーズの全回で出さない。天気予報等、画面が占有されるコンテンツ向け。
    lbar_hidden = models.BooleanField(default=False, verbose_name="L字を表示しない")
    # #23 YouTube 番組専用枠。True なら展開 Program も既定で rolling 枠と並行の専用配信を立てる。
    # Program 側で個別指定が無いとき youtube_preset がフォールバックに使われる。
    youtube_dedicated = models.BooleanField(default=False, verbose_name="YouTube 専用枠を立てる")
    youtube_preset = models.ForeignKey(
        "youtube.YoutubeBroadcastPreset",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    # 生放送の配信ポリシー既定値 (#27、docs/site-only-broadcast.md)。展開 Program の既定になる
    # (genre/cast と同じ継承の流儀)。SeriesSlot 側で個別上書きも可。
    exposure_policy_default = models.CharField(
        max_length=20, choices=ExposurePolicy.choices, default=ExposurePolicy.PUBLIC
    )

    class Meta:
        db_table = "series"
        constraints = [
            # 空 slug は重複許容 (condition)。slug 設定済みはチャンネル内で一意。
            models.UniqueConstraint(
                fields=["channel", "slug"],
                condition=Q(slug__gt=""),
                name="uniq_series_slug_per_channel",
            ),
        ]

    def __str__(self):
        return self.title

    @property
    def public_url(self) -> str:
        """slug があれば slug URL、無ければ ID URL (どちらも有効)。"""
        return f"/series/{self.slug}/" if self.slug else f"/series/{self.id}/"

    @property
    def x_handle_clean(self) -> str:
        return (self.x_handle or "").lstrip("@").strip()

    @property
    def x_handle_display(self) -> str:
        h = self.x_handle_clean
        return f"@{h}" if h else ""

    @property
    def x_url(self) -> str:
        h = self.x_handle_clean
        return f"https://x.com/{h}" if h else ""

    @property
    def hashtag_clean(self) -> str:
        return (self.x_hashtag or "").lstrip("#").strip()

    @property
    def x_hashtag_display(self) -> str:
        t = self.hashtag_clean
        return f"#{t}" if t else ""

    @property
    def hashtag_url(self) -> str:
        from urllib.parse import quote

        t = self.hashtag_clean
        return f"https://x.com/hashtag/{quote(t)}" if t else ""


class RecurrenceKind(models.TextChoices):
    """編成スロットの繰り返しパターン (#7 Phase 2 変則編成)。param は recurrence_param(JSON)。"""

    WEEKLY = "weekly", "毎週(曜日)"
    MONTHLY_NTH_DOW = "monthly_nth_dow", "第N曜"  # param {"weeks":[1..5]} + dow
    DAYS_OF_MONTH = "days_of_month", "毎月の指定日"  # param {"days":[1..31]}
    DAYS_ENDING = "days_ending", "末尾が指定の日"  # param {"ending":[0..9]}
    DAILY = "daily", "毎日"  # dow 非依存・毎日該当 (#22 天気予報枠)


_DOW_JA = "月火水木金土日"


class SeriesSlot(models.Model):
    """週間基本編成スロット (#6 Phase C)。expand_series_slots が N 週先まで Program へ展開する。

    recorded は default_asset (汎用/再放送素材) で枠を確保し、当該回の納品で編成 UI 差替の運用。
    effective_from/to で改編期の版管理。recurrence_kind で変則パターン (第N曜/指定日/末尾日) に対応。"""

    series = models.ForeignKey(Series, on_delete=models.CASCADE, related_name="slots")
    dow = models.IntegerField()  # 0=月..6=日 (weekly / monthly_nth_dow で使用)
    # 繰り返しパターン。既定 weekly (既存行は dow 毎週=後方互換)。param は kind 別の付加情報。
    recurrence_kind = models.CharField(
        max_length=20, choices=RecurrenceKind.choices, default=RecurrenceKind.WEEKLY
    )
    recurrence_param = models.JSONField(default=dict, blank=True)
    start_time = models.TimeField()
    duration_ms = models.BigIntegerField()
    program_type = models.CharField(max_length=10, choices=ProgramType.choices)
    live_source = models.ForeignKey(
        "core.LiveSource", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    default_asset = models.ForeignKey(
        "medialib.Asset", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    # 配信ポリシーの個別上書き (#27)。空文字 = series.exposure_policy_default を継承 (genre/cast の
    # 継承と同流儀)。Phase C の週次展開 (expand_series_slots) が未配線でも先にフィールドだけ用意する。
    exposure_policy = models.CharField(
        max_length=20, choices=ExposurePolicy.choices, blank=True, default=""
    )

    class Meta:
        db_table = "series_slot"
        constraints = [
            models.CheckConstraint(name="chk_slot_dow", condition=Q(dow__gte=0) & Q(dow__lte=6)),
            models.CheckConstraint(
                name="chk_slot_source",
                condition=(
                    Q(program_type="recorded", default_asset__isnull=False)
                    | Q(program_type="live", live_source__isnull=False)
                ),
            ),
        ]

    def recurrence_label(self) -> str:
        """繰り返しパターンの人間可読表記 (例 '毎週月' / '第2・第4 月' / '毎月1,15日' / '末尾5の日')。"""
        p = self.recurrence_param or {}
        dow = _DOW_JA[self.dow] if 0 <= self.dow < 7 else "?"
        if self.recurrence_kind == RecurrenceKind.DAILY:
            return "毎日"
        if self.recurrence_kind == RecurrenceKind.MONTHLY_NTH_DOW:
            weeks = "・".join(f"第{w}" for w in p.get("weeks", []))
            return f"{weeks} {dow}" if weeks else f"第? {dow}"
        if self.recurrence_kind == RecurrenceKind.DAYS_OF_MONTH:
            return "毎月 " + "・".join(f"{d}日" for d in p.get("days", []))
        if self.recurrence_kind == RecurrenceKind.DAYS_ENDING:
            return "末尾" + "・".join(str(e) for e in p.get("ending", [])) + "の日"
        return f"毎週{dow}"

    def matches(self, d) -> bool:
        """日付 d がこのスロットの繰り返しパターンに該当するか (effective 窓は別判定)。"""
        p = self.recurrence_param or {}
        kind = self.recurrence_kind
        if kind == RecurrenceKind.DAILY:
            return True
        if kind == RecurrenceKind.MONTHLY_NTH_DOW:
            return d.weekday() == self.dow and ((d.day - 1) // 7 + 1) in (p.get("weeks") or [])
        if kind == RecurrenceKind.DAYS_OF_MONTH:
            return d.day in (p.get("days") or [])
        if kind == RecurrenceKind.DAYS_ENDING:
            return (d.day % 10) in (p.get("ending") or [])
        return d.weekday() == self.dow  # WEEKLY (既定)

    def upcoming_dates(self, n: int = 6, start=None, horizon_days: int = 120) -> list:
        """effective 窓内で、このパターンが今後該当する日付を最大 n 件 (展開プレビュー用)。"""
        from datetime import timedelta

        from django.utils import timezone

        d = start or timezone.localdate()
        end = d + timedelta(days=horizon_days)
        out: list = []
        while d < end and len(out) < n:
            if (
                d >= self.effective_from
                and (self.effective_to is None or d <= self.effective_to)
                and self.matches(d)
            ):
                out.append(d)
            d += timedelta(days=1)
        return out


class EpisodeStatus(models.TextChoices):
    """Episode (回) の確定段階 (#5 納品ターゲティング)。"""

    PLANNED = "planned", "予定"  # 回は確保したが素材未確定
    CONFIRMED = "confirmed", "確定"  # 承認済み納品で asset 確定
    AIRED = "aired", "放送済"


class Episode(models.Model):
    """番組(Series)の回(エピソード)。納品の主ターゲット (#5 / docs/delivery.md)。

    Episode = 回(コンテンツ)であり放映ではない。再放送は 1 Episode → 複数 Program
    (Program.episode で結ぶ)。air_date は放送予定日/初回の情報で unique でも展開キーでもない。
    承認済み納品の本編素材が asset に確定し、展開 (expand_series_slots) が default_asset より
    優先採用する。channel は series から派生 (stored FK にしない)。
    """

    series = models.ForeignKey(Series, on_delete=models.CASCADE, related_name="episodes")
    episode_no = models.IntegerField(null=True, blank=True)  # 回数 (人/納品が付与・非自動採番)
    air_date = models.DateField(null=True, blank=True)  # 放送予定日/初回 (情報・非 unique)
    title = models.CharField(max_length=300, blank=True)
    asset = models.ForeignKey(
        "medialib.Asset",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )  # 承認済み納品で確定する回別本編素材
    status = models.CharField(
        max_length=12, choices=EpisodeStatus.choices, default=EpisodeStatus.PLANNED
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "episode"
        constraints = [
            # 回数は series 内で一意 (未採番=NULL は複数可)
            models.UniqueConstraint(
                fields=["series", "episode_no"],
                condition=Q(episode_no__isnull=False),
                name="uq_episode_series_no",
            ),
        ]
        indexes = [
            models.Index(fields=["series", "air_date"], name="idx_episode_series_date"),
        ]

    def __str__(self):
        no = f"#{self.episode_no}" if self.episode_no is not None else "?"
        return f"{self.series.title} 第{no}回"

    @property
    def channel(self):
        """放送チャンネル (series から派生)。"""
        return self.series.channel


class Program(models.Model):
    """番組(編成: 人が組む / フリー編成。任意時刻に配置)。"""

    channel = models.ForeignKey("core.Channel", on_delete=models.PROTECT, related_name="programs")
    series = models.ForeignKey(
        Series,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="programs",
    )  # レギュラー紐付け (単発は NULL)。#6 sales
    episode = models.ForeignKey(
        "scheduling.Episode",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="programs",
    )  # この放映が表す回 (再放送は同一 Episode を複数 Program が指す)。#5
    type = models.CharField(max_length=10, choices=ProgramType.choices)
    title = models.CharField(max_length=300)
    # ジャンル (#7 公開フロント)。空なら series.genre にフォールバック (resolved_genre)。
    genre = models.CharField(max_length=20, choices=Genre.choices, blank=True, default="")
    # 視聴年齢制限 (#BILL-02)。空なら series.rating にフォールバック (resolved_rating)。
    # 主管理は Series 側 / Django admin。ProgramForm には載せない (studio 保存での上書き消去を回避)。
    rating = models.CharField(max_length=8, choices=ContentRating.choices, blank=True, default="")
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()  # = start_at + 素材尺 + Σ(ad_break.duration)
    asset = models.ForeignKey(
        "medialib.Asset",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    live_source = models.ForeignKey(
        "core.LiveSource",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    # type=LIVE でも録画済みメザニンを保持するための欄。chk_program_source とは独立(制約対象外)。
    # 生放送を録画して見逃し配信(VOD)に載せる際、playback_asset が type に応じて asset/recording_asset
    # を出し分ける (#VOD 生放送録画)。
    recording_asset = models.ForeignKey(
        "medialib.Asset",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    # この放送を自動録画するか (Studio 編成時の opt-in)。type=LIVE の番組でのみ意味を持つ。
    record_live = models.BooleanField(default=False, verbose_name="この放送を自動録画する")
    cm_bundle = models.ForeignKey(
        "medialib.CmBundle",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    description = models.TextField(blank=True, null=True)
    # 出演者 (#EPG-01)。空なら series.cast にフォールバック (resolved_cast)。
    cast = models.TextField(blank=True, null=True)
    # 朝・夕の左上時計 (daypart) を、時間帯内でもこの放映では出さない (番組単位の opt-out)。
    clock_hidden = models.BooleanField(default=False, verbose_name="朝・夕の時計を表示しない")
    # 時計スタイル上書き。null = 継承 (シリーズ → チャンネル)。値があれば優先マージ。
    clock_style_override = models.JSONField(null=True, blank=True, default=None)
    # Lバー (下L字) をこの放映では出さない (番組単位の opt-out)。
    lbar_hidden = models.BooleanField(default=False, verbose_name="L字を表示しない")
    public_visible = models.BooleanField(default=True)
    # 見逃し配信 (VOD)。放送後にオンデマンド再生を許すか / 公開範囲 (#VOD-01)。
    # 既定 off=権利配慮の opt-in。RIGHTS-02 (配信権) の期間/媒体管理はこの土台に乗せる。
    vod_visibility = models.CharField(
        max_length=12, choices=VodVisibility.choices, default=VodVisibility.OFF
    )
    vod_available_until = models.DateTimeField(null=True, blank=True)  # 公開終了 (null=無期限)
    # #27 ファンクラブ限定 (vod_visibility=fanclub 時に必須)。NULL=完全公開/0=無料会員以上/
    # n=有料ティアn以上。判定は fanclub.services.can_view_level (fanclub app への FK は作らない)。
    fc_required_level = models.PositiveSmallIntegerField(null=True, blank=True)
    # 生放送の配信ポリシー (#27、docs/site-only-broadcast.md)。空なら series.exposure_policy_default
    # にフォールバック (resolved_exposure_policy。genre/rating/cast と同じ継承の流儀)。
    exposure_policy = models.CharField(
        max_length=20, choices=ExposurePolicy.choices, blank=True, default=""
    )
    # 見逃しトップの featured Hero に出す番組(編集選択)。#Phase2 rewire。
    # 複数なら最新放送(end_at desc)の VOD 公開可能なものが採用される。
    is_featured = models.BooleanField(default=False)
    # #23 YouTube 番組専用枠。True なら rolling 枠と並行して専用 liveBroadcast を立てる。
    # 解決順は番組 OR シリーズ (wants_dedicated)、preset は番組→シリーズ (resolved_youtube_preset)。
    youtube_dedicated = models.BooleanField(default=False, verbose_name="YouTube 専用枠を立てる")
    youtube_preset = models.ForeignKey(
        "youtube.YoutubeBroadcastPreset",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "program"
        indexes = [models.Index(fields=["channel", "start_at"], name="idx_program_ch_time")]
        constraints = [
            models.CheckConstraint(
                name="chk_program_time",
                condition=Q(end_at__gt=F("start_at")),
            ),
            models.CheckConstraint(
                name="chk_program_source",
                condition=(
                    Q(type="recorded", asset__isnull=False, live_source__isnull=True)
                    | Q(type="live", live_source__isnull=False, asset__isnull=True)
                ),
            ),
            # #27: vod_visibility=fanclub のときは fc_required_level が必須 (NULL=完全公開のまま
            # fanclub 限定を名乗る事故を防ぐ)。他の visibility では fc_required_level の値は無視される。
            models.CheckConstraint(
                name="chk_program_fc_requires_level",
                condition=(
                    Q(vod_visibility="fanclub", fc_required_level__isnull=False)
                    | ~Q(vod_visibility="fanclub")
                ),
            ),
            ExclusionConstraint(
                name="program_no_overlap_per_channel",
                expressions=[
                    (F("channel"), RangeOperators.EQUAL),
                    (TsTzRange(F("start_at"), F("end_at")), RangeOperators.OVERLAPS),
                ],
            ),
        ]

    def __str__(self):
        return f"[{self.type}] {self.title}"

    @property
    def resolved_genre(self) -> str:
        """ジャンルの解決順: 番組 → シリーズ → 空 (#7 公開フロント)。"""
        if self.genre:
            return self.genre
        if self.series_id and self.series and self.series.genre:
            return self.series.genre
        return ""

    @property
    def resolved_cast(self) -> str:
        """出演者の解決順: 番組 → シリーズ → 空 (#EPG-01)。"""
        if self.cast:
            return self.cast
        if self.series_id and self.series and self.series.cast:
            return self.series.cast
        return ""

    @property
    def resolved_exposure_policy(self) -> str:
        """配信ポリシーの解決順: 番組 → シリーズ既定 → public (#27)。"""
        if self.exposure_policy:
            return self.exposure_policy
        if self.series_id and self.series:
            return self.series.exposure_policy_default
        return ExposurePolicy.PUBLIC

    @property
    def wants_dedicated(self) -> bool:
        """YouTube 専用枠を立てるか (#23): 番組 OR シリーズ。"""
        if self.youtube_dedicated:
            return True
        return bool(self.series_id and self.series and self.series.youtube_dedicated)

    @property
    def resolved_youtube_preset(self):
        """専用枠プリセットの解決順 (#23): 番組 → シリーズ → None。"""
        if self.youtube_preset_id:
            return self.youtube_preset
        if self.series_id and self.series and self.series.youtube_preset_id:
            return self.series.youtube_preset
        return None

    @property
    def resolved_rating(self) -> str:
        """視聴年齢制限の解決順: 番組 → シリーズ → 空 (#BILL-02)。"""
        if self.rating:
            return self.rating
        if self.series_id and self.series and self.series.rating:
            return self.series.rating
        return ""

    @property
    def resolved_rating_label(self) -> str:
        """resolved_rating の表示ラベル (例 'R18+')。空=制限なしは空文字。"""
        r = self.resolved_rating
        return ContentRating(r).label if r else ""

    @property
    def min_age(self) -> int:
        """resolved_rating が要求する最低視聴年齢(歳)。0 = ハードゲート無し。"""
        return RATING_MIN_AGE.get(self.resolved_rating, 0)

    @property
    def resolved_clock_hidden(self) -> bool:
        """朝・夕の時計をこの放映で出さないか。番組 OR シリーズ のどちらかが非表示なら非表示。"""
        return bool(
            self.clock_hidden or (self.series_id and self.series and self.series.clock_hidden)
        )

    @property
    def resolved_clock_style_override(self) -> dict | None:
        """時計スタイル上書き。番組 → シリーズ の優先順で最初の非 None を返す。None = 上書き無し。"""
        if self.clock_style_override is not None:
            return self.clock_style_override
        if self.series_id and self.series and self.series.clock_style_override is not None:
            return self.series.clock_style_override
        return None

    @property
    def resolved_lbar_hidden(self) -> bool:
        """L字(Lバー)をこの放映で出さないか。番組 OR シリーズ のどちらかが非表示なら非表示。"""
        return bool(
            self.lbar_hidden or (self.series_id and self.series and self.series.lbar_hidden)
        )

    @property
    def thumb_url(self) -> str:
        """番組サムネの解決順: 素材 → シリーズ → 空 (空はテンプレが既定プレースホルダを描画)。"""
        if self.asset_id and self.asset and self.asset.thumbnail_url:
            return self.asset.thumbnail_url
        if self.series_id and self.series and self.series.thumbnail_url:
            return self.series.thumbnail_url
        return ""

    @property
    def playback_asset(self):
        """VOD再生に使う asset。type=RECORDED なら asset、type=LIVE なら recording_asset。"""
        return self.asset if self.type == ProgramType.RECORDED else self.recording_asset


class AdBreak(models.Model):
    """CM枠(録画番組内)。枠尺は grid 整数倍 (chk_grid_multiple は 0002 migration で付与)。"""

    program = models.ForeignKey(Program, on_delete=models.CASCADE, related_name="ad_breaks")
    offset_ms = models.BigIntegerField()  # 番組頭からの挿入位置
    grid = models.CharField(max_length=8, choices=CmGrid.choices)
    duration_ms = models.BigIntegerField()

    class Meta:
        db_table = "ad_break"


class AdBreakItem(models.Model):
    """CM枠の充填結果(スケジューラが選定)。"""

    ad_break = models.ForeignKey(AdBreak, on_delete=models.CASCADE, related_name="items")
    seq = models.IntegerField()
    cm_asset = models.ForeignKey("medialib.CmCreative", on_delete=models.PROTECT, related_name="+")

    class Meta:
        db_table = "ad_break_item"
        constraints = [
            models.UniqueConstraint(fields=["ad_break", "seq"], name="uq_ad_break_item_seq"),
        ]
        ordering = ["ad_break", "seq"]


class GraphicKind(models.TextChoices):
    GRAPHIC = "graphic", "画像/動画+文字"
    VIDEO = "video", "透過動画"
    TEXT = "text", "文字"


class GraphicCue(models.Model):
    """番組/フィラー/CM 単位の自動グラフィック 1 行 (#18 §C / cg-layers.md)。

    owner は series / program / channel(+context) のいずれか 1 つ。Series=繰り返し原本、
    Program=個別回(展開時に Series からコピー)/単発、Channel(+context=filler|cm)=基本セット。
    resolver が cg_cues として番組/フィラー/CM イベントに載せ、agent がタイミング表示する。
    """

    series = models.ForeignKey(
        Series, on_delete=models.CASCADE, null=True, blank=True, related_name="graphic_cues"
    )
    program = models.ForeignKey(
        Program, on_delete=models.CASCADE, null=True, blank=True, related_name="graphic_cues"
    )
    channel = models.ForeignKey(
        "core.Channel", on_delete=models.CASCADE, null=True, blank=True, related_name="graphic_cues"
    )
    context = models.CharField(max_length=8, blank=True, null=True)  # channel: filler|cm

    layer = models.IntegerField()  # 1-89 (90=スレート予約)
    kind = models.CharField(max_length=8, choices=GraphicKind.choices, default=GraphicKind.GRAPHIC)
    # graphic: {"elements":[{media,url,text,x,y,w,size,color,align}, …]} / video: {"clip":..,"loop":bool}
    # text: {"text":..}
    data = models.JSONField(default=dict, blank=True)
    template = models.CharField(max_length=64, blank=True)  # 任意のテンプレ上書き
    show_at_ms = models.BigIntegerField(default=0)  # 番組/クリップ頭からの表示開始
    hide_at_ms = models.BigIntegerField(null=True, blank=True)  # 表示終了 (null=末尾まで)
    seq = models.IntegerField(default=0)

    class Meta:
        db_table = "graphic_cue"
        ordering = ["seq", "id"]


class LiveRundown(models.Model):
    """生番組の進行表(ランダウン)＝義務台帳＋参照タイムライン (docs/timekeeper-live.md §3/§4)。"""

    program = models.OneToOneField(Program, on_delete=models.CASCADE, related_name="live_rundown")
    note = models.TextField(blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "live_rundown"


class LiveCueKind(models.TextChoices):
    SECTION = "section", "本編"
    CM = "cm", "CM"
    VT = "vt", "VT"


class LiveCueState(models.TextChoices):
    PENDING = "pending", "pending"
    FIRING = "firing", "firing"
    AIRED = "aired", "aired"
    SKIPPED = "skipped", "skipped"


class LiveCueAnchor(models.TextChoices):
    """auto_fire の予約時刻アンカー (タイムキープ Phase3 D1 / docs/timekeeper-live.md §11)。"""

    START = "start", "開始相対"  # prog.start_at + auto_offset_ms (既定・既存挙動)
    # 番組日の固定時刻 auto_wall_time に発火。開始ズレ (押え/巻き) に左右されない
    # → ネット CM の定時ジョイン等の固定時刻 cue 向け (設計が auto_fire に推奨する用途)。
    WALLCLOCK = "wallclock", "壁時計"


class LiveCue(models.Model):
    """進行表の1行。義務台帳(残CM/残VT)の実体はこのモデルのpending行の集計 (core.timekeeper)。"""

    rundown = models.ForeignKey(LiveRundown, on_delete=models.CASCADE, related_name="cues")
    seq = models.IntegerField()
    kind = models.CharField(max_length=8, choices=LiveCueKind.choices)
    label = models.CharField(max_length=120, blank=True, null=True)
    planned_duration_ms = models.BigIntegerField()
    # kind=cm 時のみ。kind=vt/section は null (アプリ層検証。CuePoint.grid と同じ流儀 — DB制約なし)。
    cm_bundle = models.ForeignKey(
        "medialib.CmBundle", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    grid = models.CharField(max_length=8, choices=CmGrid.choices, blank=True, null=True)
    # kind=vt 時のみ。
    asset = models.ForeignKey(
        "medialib.Asset", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    state = models.CharField(
        max_length=8, choices=LiveCueState.choices, default=LiveCueState.PENDING
    )
    fired_event = models.ForeignKey(
        "playout.PlayoutEvent", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    auto_fire = models.BooleanField(default=False)  # Phase2が実行消費。Phase1は保持のみ
    auto_offset_ms = models.BigIntegerField(null=True, blank=True)  # anchor=start 時
    auto_anchor = models.CharField(
        max_length=10, choices=LiveCueAnchor.choices, default=LiveCueAnchor.START
    )
    auto_wall_time = models.TimeField(null=True, blank=True)  # anchor=wallclock 時 (番組日の時刻)

    class Meta:
        db_table = "live_cue"
        constraints = [models.UniqueConstraint(fields=["rundown", "seq"], name="uq_live_cue_seq")]
        ordering = ["rundown", "seq"]


class LiveRundownTemplate(models.Model):
    """SeriesSlot の定番進行表(雛形)。当日より前に PC で組んでおき、expand_series_slots の
    live 分岐が展開時に LiveRundown/LiveCue へ複製する (docs/timekeeper-live.md §9・#25 Phase3 C)。
    展開先の LiveRundown/LiveCue は生放送パスの実体で、この雛形は「原本」に徹する
    (Series.graphic_cues → Program.graphic_cues のコピー慣習と同型)。"""

    slot = models.OneToOneField(
        SeriesSlot, on_delete=models.CASCADE, related_name="rundown_template"
    )
    note = models.TextField(blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "live_rundown_template"


class LiveRundownTemplateCue(models.Model):
    """雛形の1行。LiveCue と同じ項目を持つが、state/fired_event は持たない(原本は発火しない)。
    展開時に state=pending の LiveCue として複製される。"""

    template = models.ForeignKey(LiveRundownTemplate, on_delete=models.CASCADE, related_name="cues")
    seq = models.IntegerField()
    kind = models.CharField(max_length=8, choices=LiveCueKind.choices)
    label = models.CharField(max_length=120, blank=True, null=True)
    planned_duration_ms = models.BigIntegerField()
    # kind=cm 時のみ (LiveCue と同じアプリ層検証・DB制約なし)。
    cm_bundle = models.ForeignKey(
        "medialib.CmBundle", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    grid = models.CharField(max_length=8, choices=CmGrid.choices, blank=True, null=True)
    # kind=vt 時のみ。
    asset = models.ForeignKey(
        "medialib.Asset", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    auto_fire = models.BooleanField(default=False)
    auto_offset_ms = models.BigIntegerField(null=True, blank=True)
    auto_anchor = models.CharField(
        max_length=10, choices=LiveCueAnchor.choices, default=LiveCueAnchor.START
    )
    auto_wall_time = models.TimeField(null=True, blank=True)

    class Meta:
        db_table = "live_rundown_template_cue"
        constraints = [models.UniqueConstraint(fields=["template", "seq"], name="uq_tmpl_cue_seq")]
        ordering = ["template", "seq"]


class LiveRecordingIngestState(models.TextChoices):
    """生放送録画クリップ取り込みの進行 (#VOD 生放送録画)。"""

    NORMALIZING = "normalizing", "正規化中"  # Asset 化済み・正規化待ち
    BOUND = "bound", "番組に紐付け済"  # Program.recording_asset を差し替えた
    NO_PROGRAM = "no_program", "対象番組なし"  # source_key の program_id に一致する Program が無い
    FAILED = "failed", "失敗"  # 正規化失敗 / asset 消失


class LiveRecordingIngest(models.Model):
    """R2 ドロップで届いた生放送録画クリップ 1 本の取り込み在庫 (#VOD 生放送録画)。

    録画サブシステムが `ingest/live_recording/<channel_slug>/<program_id>.mp4` を R2 に置き、
    取り込み beat が Asset 化 → 正規化 → READY 後に該当 Program.recording_asset へ紐付ける。
    source_key 一意で二重取り込みを防ぐ (= 冪等の要)。program は source_key 中の program_id から
    解決するため、該当 Program が見つからない場合は program=NULL のまま NO_PROGRAM で保留する。
    """

    source_key = models.CharField(max_length=500, unique=True)  # R2 投入キー (冪等の要)
    channel = models.ForeignKey("core.Channel", on_delete=models.PROTECT, related_name="+")
    program = models.ForeignKey(
        "scheduling.Program", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    asset = models.ForeignKey(
        "medialib.Asset", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    state = models.CharField(
        max_length=12,
        choices=LiveRecordingIngestState.choices,
        default=LiveRecordingIngestState.NORMALIZING,
    )
    error = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "live_recording_ingest"
        indexes = [
            models.Index(fields=["state"], name="idx_live_rec_ingest_state"),
        ]

    def __str__(self):
        return f"{self.source_key} ({self.state})"


class SeriesPostKind(models.TextChoices):
    ARTICLE = "article", "記事・お知らせ"
    CAMPAIGN = "campaign", "キャンペーン応募"


class SeriesPost(models.Model):
    """番組紹介サイト用の投稿ページ(記事/キャンペーン)。シリーズに紐付く公開コンテンツ。"""

    series = models.ForeignKey(Series, on_delete=models.CASCADE, related_name="posts")
    kind = models.CharField(
        max_length=20, choices=SeriesPostKind.choices, default=SeriesPostKind.ARTICLE
    )
    title = models.CharField(max_length=300)
    body = models.TextField(blank=True, help_text="本文(テキスト)。HTML可。")
    media_url = models.CharField(
        max_length=500, blank=True, help_text="画像/動画 URL (外部または R2)"
    )
    # キャンペーン固有
    form_url = models.CharField(
        max_length=500, blank=True, help_text="応募フォーム URL (kind=campaign 時)"
    )
    campaign_start = models.DateTimeField(null=True, blank=True, help_text="応募受付開始")
    campaign_end = models.DateTimeField(null=True, blank=True, help_text="応募受付終了")
    is_published = models.BooleanField(default=False)
    published_at = models.DateTimeField(null=True, blank=True)
    # #27 ファンクラブ限定。NULL=完全公開/0=無料会員以上/n=有料ティアn以上。
    # series が fanclub.Creator に未紐付けなら常に完全公開扱い (fanclub.services.fc_gate_reason)。
    fc_required_level = models.PositiveSmallIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    if TYPE_CHECKING:
        # #27: core.views._render_series_detail がクエリ後に動的付与するテンプレート専用フラグ。
        # DB 列ではない (migration 対象外)。mypy に実体を教えるための型のみの宣言。
        locked: bool

    class Meta:
        db_table = "series_post"
        ordering = ["-published_at"]

    def __str__(self) -> str:
        return f"[{self.get_kind_display()}] {self.title}"


# --------------------------------------------------------------------------- #
#  ネイティブ投書 / キャンペーン応募 フォーム基盤                               #
# --------------------------------------------------------------------------- #


class AudienceFormKind(models.TextChoices):
    MESSAGE = "message", "投書・メッセージ"
    CAMPAIGN = "campaign", "キャンペーン応募"


class AudienceSubmissionStatus(models.TextChoices):
    NEW = "new", "未読"
    READ = "read", "既読"
    HANDLED = "handled", "対応済"


class AudienceForm(models.Model):
    """番組ページに設置する投書/キャンペーン応募フォームの定義。

    SeriesPost(外部 form_url)と並存。本モデルはネイティブな受け取り/保存を提供する。
    fields は [{key,label,type,required,options?,help?}] の JSON リスト (validate_fields で検証)。
    """

    series = models.ForeignKey(Series, on_delete=models.CASCADE, related_name="audience_forms")
    kind = models.CharField(
        max_length=20, choices=AudienceFormKind.choices, default=AudienceFormKind.MESSAGE
    )
    title = models.CharField(max_length=300, help_text='例: "番組へメッセージを送る"')
    description = models.TextField(blank=True, help_text="フォーム上部の説明文。")
    enabled = models.BooleanField(default=False, help_text="公開ページへの表示 ON/OFF。")
    requires_login = models.BooleanField(
        default=False, help_text="True=会員ログイン必須 (campaign の場合は True を推奨)。"
    )
    fields = models.JSONField(default=list, help_text="フィールド定義リスト。")
    success_message = models.TextField(
        blank=True,
        default="送信しました。ありがとうございました。",
        help_text="送信完了メッセージ。",
    )
    notify_email = models.CharField(
        max_length=200,
        blank=True,
        help_text="新着投稿をここへメール通知 (空=通知なし)。複数は半角カンマ区切り。",
    )
    # campaign 固有
    starts_at = models.DateTimeField(null=True, blank=True, help_text="応募受付開始 (campaign)。")
    ends_at = models.DateTimeField(null=True, blank=True, help_text="応募受付終了 (campaign)。")
    prize = models.TextField(blank=True, help_text="賞品・特典の説明 (campaign)。")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "audience_form"
        ordering = ["kind", "pk"]

    def __str__(self) -> str:
        return f"[{self.get_kind_display()}] {self.title} (series={self.series_id})"

    def is_open(self, now=None) -> bool:
        """フォームが受付中かどうか (enabled かつ campaign は期間内)。"""
        if not self.enabled:
            return False
        if self.kind != AudienceFormKind.CAMPAIGN:
            return True
        if now is None:
            from django.utils import timezone

            now = timezone.now()
        if self.starts_at and now < self.starts_at:
            return False
        return not (self.ends_at and now > self.ends_at)

    def field_map(self) -> dict:
        return {f["key"]: f for f in (self.fields or [])}


class AudienceSubmission(models.Model):
    """フォームへの投稿 1 件。payload は {field_key: value} の JSON。"""

    form = models.ForeignKey(AudienceForm, on_delete=models.CASCADE, related_name="submissions")
    member = models.ForeignKey(
        "members.Member",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audience_submissions",
    )
    payload = models.JSONField(default=dict)
    # payload から抽出した代表値 (検索・一覧表示用)
    submitter_name = models.CharField(max_length=200, blank=True)
    submitter_email = models.CharField(max_length=200, blank=True)
    status = models.CharField(
        max_length=10,
        choices=AudienceSubmissionStatus.choices,
        default=AudienceSubmissionStatus.NEW,
    )
    deleted_at = models.DateTimeField(null=True, blank=True)  # soft-delete (Comment 流用)
    ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "audience_submission"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["form", "status"], name="idx_audience_sub_form_status"),
            models.Index(fields=["created_at"], name="idx_audience_sub_created"),
        ]

    def __str__(self) -> str:
        name = self.submitter_name or f"member#{self.member_id}" or "匿名"
        return f"[{self.get_status_display()}] {name} → form#{self.form_id}"
