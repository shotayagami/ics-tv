# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
ICS-TV control plane (modular monolith) settings.
Env 駆動。単一コード/単一PG を web/beat/worker/normalize の複数ワークロードで分離稼働する。
"""

from pathlib import Path

import environ
from django.core.exceptions import ImproperlyConfigured

from config.checks import parse_distribution_mode

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    # 安全側: 未設定なら DEBUG=False。dev は .env で DJANGO_DEBUG=true を明示する。
    DJANGO_DEBUG=(bool, False),
    DJANGO_ALLOWED_HOSTS=(list, []),
)
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("DJANGO_SECRET_KEY", default="dev-insecure-change-me")
DEBUG = env("DJANGO_DEBUG")
ALLOWED_HOSTS = env("DJANGO_ALLOWED_HOSTS")

# 外部公開 (Cloudflare Tunnel → ingress-nginx) は TLS を edge/ingress で終端し、Pod へは
# X-Forwarded-Proto: https で届く。Django に「この接続は https」と伝え、CSRF/secure cookie を
# 正しく機能させる。Host は書き換えず保持する方式 (cookie ドメイン整合のため)。
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
# 外部 https オリジンからの POST (admin/編成/会員) を CSRF 許可する。空なら内部のみ。
# ⚠ 会員フォームは公開ホスト (tv.*) 初の POST。本番は公開オリジンを必ず含めること
#   (例: https://tv.example.com)。含めないと CF Tunnel 経由の会員 POST が 403 になる。
CSRF_TRUSTED_ORIGINS = env.list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[])
# 本番 (DEBUG=False) は会員資格情報がセッションに乗るため secure cookie を強制
# (edge は CF/ingress で TLS 終端、Pod へ X-Forwarded-Proto: https)。dev は False。
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG

# セキュリティヘッダ (#sec L-10/L-11)。
#
# 下の 3 つは Django の既定値と同じ値だが、**既定に依存せず明示する**ために書いている。
# 点検 (2026-07-03) 時点でヘッダは実際に出ていたものの、settings に記述が無いため
# 「未設定」と読み違えられた。Django のメジャーアップグレードで既定が変わっても
# 気付けないという実害もある (実機で 2026-08-12 に現行値を確認済み)。
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

# HSTS は **意図的に Django 側で出さない** (既定の 0 のまま)。
# 実配信では Cloudflare (max-age=15552000; includeSubDomains; preload) と
# ingress-nginx (max-age=31536000; includeSubDomains) の両方が付けており、
# ここで足すと nginx の add_header と重複してヘッダが 2 本になる。
# RFC 6797 上は先頭が採用されて実害は無いが、どこが正なのか分からなくなるため
# 「TLS を終端する層が付ける」に一本化する。edge を通らない経路を作るときは
# ここを 31536000 にすること。
SECURE_HSTS_SECONDS = 0

# Content-Security-Policy (#sec L-11)。点検の指示どおり段階導入する。
#
# 現状のテンプレートは inline のイベントハンドラ 41 箇所と inline <style> 8 ファイルを
# 持ち、外部オリジン (unpkg / Google Fonts / YouTube 埋め込み) にも依存している。
# よって script-src / style-src をいきなり enforce すると公開サイトが壊れる。
#
# 第 1 段の本 PR では:
#   - 壊しようがない指示子だけを **enforce** する (frame-ancestors / base-uri / object-src)
#   - 完全版は **Report-Only** で並走させ、違反の実績を見てから締める
# 第 2 段で inline を外し、Report-Only 側を enforce へ寄せる。
# 違反レポートの収集エンドポイントは意図的に作っていない (未認証 POST を増やす判断は
# 別途レビューが要る)。現状は開発者ツールで観測する前提。
CSP_ENFORCE = "; ".join(
    [
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "object-src 'none'",
    ]
)
CSP_REPORT_ONLY = "; ".join(
    [
        "default-src 'self'",
        # unpkg = htmx。fonts.googleapis/gstatic = Web フォント。
        "script-src 'self' 'unsafe-inline' https://unpkg.com",
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
        "font-src 'self' https://fonts.gstatic.com",
        # R2 / Cloudflare Stream のサムネイルとプレイヤー、および data: URI。
        "img-src 'self' data: https:",
        "media-src 'self' https:",
        # YouTube 埋め込み。
        "frame-src https://www.youtube.com https://www.youtube-nocookie.com",
        "connect-src 'self' https:",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "object-src 'none'",
    ]
)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    # local apps (= モジュール境界 = 将来のサービス抽出シーム)
    "core",
    "medialib",
    "scheduling",
    "youtube",
    "playout",
    "delivery",
    "sales",
    "billing",
    "procurement",
    "members",
    "subscriptions",
    "fanclub",
    "rights",
    "analytics",
    # フロント React/API 分離 (Phase 0)。JSON API ルート (django-ninja)。
    "api",
    # WebSocket (#COMM-01 ライブコメント)
    "channels",
]

# 納品ポータル (#5 P3) の login_required リダイレクト先 = 納品ログイン (Google サインイン)。
# 既定の login_required リダイレクト先。会員 (members/subscriptions) は独自の /members/login/ へ
# 飛ばし、staff_member_required は admin ログインへ飛ぶため、本設定に依存する箇所は現状無い
# (旧 delivery ポータル撤去・Phase 3.9)。安全のため admin ログインを既定にしておく。
LOGIN_URL = "/admin/login/"

# #6 契約駆動 CM 割付の constraint provider (dotted path)。空文字なら従来の均等ローテに
# デグレード (S6: sales 不在でも 24/7 送出が動く安全性)。resolver.fill_break が import_string で生成。
ICSTV_FILL_CONSTRAINT_PROVIDER = env(
    "ICSTV_FILL_CONSTRAINT_PROVIDER",
    default="sales.constraints.ContractConstraintProvider",
)

# 機密フィールドの DB at-rest 暗号化鍵 (#3 / core.fields.EncryptedTextField)。Fernet 鍵
# (urlsafe base64 32B)。env → 配備基盤側の Secret 由来。空なら平文保管 (dev のみ)。
# 生成: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
ICSTV_FIELD_ENCRYPTION_KEY = env("ICSTV_FIELD_ENCRYPTION_KEY", default="")

# アプリ版数 (フッタ表示)。ビルド時に Dockerfile ARG→ENV で git タグ/sha が焼き込まれる
# (CI build-arg)。ローカル/未ビルド環境は "dev"。手動 bump 不要。
ICSTV_VERSION = env("ICSTV_VERSION", default="dev")

# 本線 HLS 署名トークン (#27 / core.hls_auth・docs/site-only-broadcast.md §4.7)。
# エッジ (Cloudflare Worker、deploy/cloudflare-worker-hls/) と共有する HMAC 鍵。Worker 側の
# secret が漏れても Django の SECRET_KEY まで巻き込まないよう分離する。未設定は SECRET_KEY へ
# フォールバック (= Worker 未配線の従来動作)。生成: openssl rand -hex 32
ICSTV_HLS_SIGNING_KEY = env("ICSTV_HLS_SIGNING_KEY", default="")
# 導入者が配布開始時に明示するfail-closedモード。Django/Worker共通の規約は、trim後の
# true/on/ok/y/yes/1（大文字小文字不問）または符号付きASCII十進整数（非zeroならtrue）。
# それ以外はfalse。既定falseのため、既存dev/CI/監視構成の意味は変えない。
ICSTV_DISTRIBUTION_MODE = parse_distribution_mode(env("ICSTV_DISTRIBUTION_MODE", default=""))
# トークンの有効期間。エンタイトルメント変更がエッジへ効くまでの最大遅延がそのままこの値になる
# (エッジは番組/ティアを判定できないため)。短いほど使い回しの窓が狭まるが、hls.js を使わない
# ネイティブ HLS (Safari の <video src>) はトークンを差し替えられないため、視聴継続の下限にもなる。
try:
    ICSTV_HLS_TOKEN_TTL_SEC = env.int("ICSTV_HLS_TOKEN_TTL_SEC", default=3600)
except (TypeError, ValueError):
    if ICSTV_DISTRIBUTION_MODE:
        raise ImproperlyConfigured(
            "ICSTV_HLS_TOKEN_TTL_SEC は配布モードで整数を指定する必要があります。"
        ) from None
    raise

# 公開サイトの絶対ベース URL (例 https://tv.example.com)。管理ホストから公開ページへのリンク、
# および公開テンプレのナビを必ず公開ホストへ向けるために使う。未設定はローカル相対パス。
ICSTV_PUBLIC_BASE_URL = env("ICSTV_PUBLIC_BASE_URL", default="")

# 運営者の識別情報 (特定商取引法11条相当。fanclub.models.Creator の同名フィールドと同じ語彙)。
# tokushoho/privacy/terms の公開ページが表示する。未設定 (既定は空文字/False) でも起動でき、
# 開発環境ではその状態のまま表示される。公開運用での必須化は ICSTV_REQUIRE_OPERATOR_INFO を参照。
ICSTV_OPERATOR_LEGAL_NAME = env("ICSTV_OPERATOR_LEGAL_NAME", default="")
ICSTV_OPERATOR_IS_INDIVIDUAL = env.bool("ICSTV_OPERATOR_IS_INDIVIDUAL", default=False)
ICSTV_OPERATOR_REPRESENTATIVE_NAME = env("ICSTV_OPERATOR_REPRESENTATIVE_NAME", default="")
ICSTV_OPERATOR_ADDRESS = env("ICSTV_OPERATOR_ADDRESS", default="")
ICSTV_OPERATOR_PHONE = env("ICSTV_OPERATOR_PHONE", default="")
# True の間は公開画面で住所/電話を表示せず「請求により遅滞なく開示する」表記に倒す
# (fanclub.models.Creator.hide_contact_details と同じ運用)。既定 True は、未設定のまま
# 空の住所/電話をそのまま表示してしまわない安全側の初期値 (Creator モデルの既定と同じ)。
ICSTV_OPERATOR_HIDE_CONTACT_DETAILS = env.bool("ICSTV_OPERATOR_HIDE_CONTACT_DETAILS", default=True)
ICSTV_OPERATOR_CONTACT_EMAIL = env("ICSTV_OPERATOR_CONTACT_EMAIL", default="")
ICSTV_OPERATOR_INVOICE_REGISTRATION_NUMBER = env(
    "ICSTV_OPERATOR_INVOICE_REGISTRATION_NUMBER", default=""
)

# 生入力 (LiveSource) ingest URL 生成に使う送出ノードの内部 IP/ホスト (例 192.0.2.10)。
# studio「生入力」画面が OBS へ渡す SRT/RTMP push URL を組み立てる際に LiveSource.*_ingest_url(host)
# へ渡す。未設定は雛形プレースホルダのまま表示し、画面側で「未設定」を警告する (値は秘密ではない
# 内部 IP なので env で十分・暗号化不要)。
ICSTV_INGEST_NODE_HOST = env("ICSTV_INGEST_NODE_HOST", default="")

# 内部連携トークン (#22): 天気予報サブシステム管理コンソールが /api/v1/internal/weather-import を
# 叩く際の共有秘密 (X-Internal-Token)。未設定なら内部エンドポイントは常に 401 (機能無効)。
WEATHER_IMPORT_TOKEN = env("WEATHER_IMPORT_TOKEN", default="")

# 地震速報サブシステム (別リポ icstv-earthquake) → 速報テロップ発射の共有秘密 (X-Internal-Token)。
# /api/v1/internal/breaking-telop の認証。未設定なら常に 401 (= 外部からの発射は無効)。
EARTHQUAKE_FIRE_TOKEN = env("EARTHQUAKE_FIRE_TOKEN", default="")
# 注: 組込の unnerv ポーリング ingest (EARTHQUAKE_ALERT_ENABLED 等) はリファクタ Phase 1.5 で撤去。
# 地震速報は別リポ icstv-earthquake が担い、本体は上記 /internal/breaking-telop だけを公開する。

# 納品サブシステム (別リポ icstv-delivery・リファクタ Phase 2) → 完成 asset 登録の共有秘密
# (X-Internal-Token)。/api/v1/internal/delivery-asset・/delivery-refs の認証。未設定なら常に
# 401 (= 外部からの登録は無効)。サービス未稼働の間は空のままで害なし (#2.1 seam を先行設置)。
DELIVERY_REGISTER_TOKEN = env("DELIVERY_REGISTER_TOKEN", default="")


# バックオフィスサブシステム (別リポ icstv-backoffice・リファクタ Phase 3・read-API-first) →
# 経理/予算/会員の **読み取り集計** 共有秘密 (X-Internal-Token)。/api/v1/internal/backoffice/*
# の認証。未設定なら常に 401 (= 集計取得は無効)。サービス未稼働の間は空のままで害なし (3.1 seam 先行設置)。
BACKOFFICE_READ_TOKEN = env("BACKOFFICE_READ_TOKEN", default="")

# 外形監視 (Zabbix サーバ、クラスタ外) → agent heartbeat 経過秒の **読み取り専用** 共有秘密
# (X-Internal-Token)。/api/v1/internal/monitor/heartbeat の認証。未設定なら常に 401。
# 死活 beat (playout.tasks.check_agent_liveness) は監視対象クラスタの中の Celery beat/worker で
# 動いており、beat/worker/Redis ごと止まると通知は出ない。この endpoint は request 経路の
# icstv-web が DB を直接読んで返すので、Zabbix 側の nodata が「制御プレーン全体の沈黙」を外から
# 拾える (docs/operations.md O6 の第 4 層)。
MONITOR_READ_TOKEN = env("MONITOR_READ_TOKEN", default="")

# 朝・夕の左上時計オーバーレイ (daypart corner clock) の既定時間帯 (JST)。channel.clock_windows が
# 空のチャンネルはこの既定を使う (Channel.effective_clock_windows)。"HH:MM" の start/end ペア配列。
# 表示の有効化自体は channel ごとの clock_overlay_enabled (既定 False, opt-in)。
ICSTV_CLOCK_WINDOWS = env.json(
    "ICSTV_CLOCK_WINDOWS",
    default=[{"start": "04:30", "end": "08:00"}, {"start": "15:00", "end": "19:00"}],
)

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # 静的配信 (gunicorn には web サーバが無いため WhiteNoise で /static/ を返す)。
    # SecurityMiddleware の直後に置くのが規約。
    "whitenoise.middleware.WhiteNoiseMiddleware",
    # HTTP アクセスログ記録 (awstats 的な集計の入力)。ホストを見るだけなので極力早い位置に置く。
    "analytics.middleware.AccessLogMiddleware",
    # ホスト別 urlconf 切替 (#7 公開/管理分離)。CommonMiddleware の URL 処理前に置く。
    "core.middleware.HostUrlconfMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # 公開サイトの視聴者会員 (#会員管理)。session["member_id"] → request.member を遅延付与。
    # User 認証 (AuthenticationMiddleware) の直後・SessionMiddleware の後に置く。
    "members.middleware.MemberAuthMiddleware",
    # creator.* ホストのクリエイター本人 (#27)。session["creator_account_id"] → request.creator_account
    # を遅延付与。専用URLは creator.* ホストにしか存在しないため他ホストで立っても無害。
    "fanclub.creator_middleware.CreatorAccountAuthMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # CSP (#sec L-11)。レスポンスを見るだけなので順序の制約は無いが、
    # 他のセキュリティヘッダ系と並べて末尾に置く。
    "core.middleware.ContentSecurityPolicyMiddleware",
]

ROOT_URLCONF = "config.urls"  # 管理ホスト (フル機能)。他ホストは下記 urlconf へ切替。
PUBLIC_URLCONF = "config.urls_public"  # 公開ホスト (視聴者向けのみ)
OPS_URLCONF = (
    "config.urls_ops"  # 🔴 放送コンソール専用ホスト (ops.*、放送当直のみ。リファクタ Phase 1)
)
# 管理 (フル urlconf) を許すホスト。これ以外 (公開ホスト/納品ホスト/未知) はフル機能を見せない。
# testserver = Django test client 既定ホスト (テストはフル機能を見る)。
ICSTV_ADMIN_HOSTS = env.list(
    "DJANGO_ADMIN_HOSTS",
    default=[
        "localhost",
        "127.0.0.1",
        "testserver",
        "icstv-web",
    ],
)

# 🔴 放送コンソール専用ホスト (リファクタ Phase 1 / ops.*)。放送当直のモバイルファースト面。
# config.urls_ops で動き、放送コンソール SPA + API + 認証のみ公開 (編成/納品/経理 UI は非搭載=放送シェルが受ける)。
# 既定は空 = 無効。導入者が DJANGO_OPS_HOSTS で自分のホストを指定して有効化する。
ICSTV_OPS_HOSTS = env.list(
    "DJANGO_OPS_HOSTS",
    default=[],
)

# クリエイター専用ホスト (#27 ファンクラブ)。config.urls_creator で動き、Google招待サインイン +
# セルフサービス画面(ティア/投稿/会員数/契約閲覧)のみを公開する。他ホストからは到達不能。
# 既定は空 = 無効。導入者が DJANGO_CREATOR_HOSTS で自分のホストを指定して有効化する。
ICSTV_CREATOR_HOSTS = env.list(
    "DJANGO_CREATOR_HOSTS",
    default=[],
)
CREATOR_URLCONF = "config.urls_creator"

# creator.* の Google OAuth (サインインのみ、YouTube 用 ICSTV_OAUTH_CLIENT_ID とは別 GCP クライアント)。
ICSTV_CREATOR_OAUTH_CLIENT_ID = env("ICSTV_CREATOR_OAUTH_CLIENT_ID", default="")
ICSTV_CREATOR_OAUTH_CLIENT_SECRET = env("ICSTV_CREATOR_OAUTH_CLIENT_SECRET", default="")
# studio → creator.* への絶対URL組み立て用 (招待メール本文・招待URL表示に使う)。
ICSTV_CREATOR_BASE_URL = env("ICSTV_CREATOR_BASE_URL", default="")
ICSTV_CREATOR_INVITATION_TTL_DAYS = env.int("ICSTV_CREATOR_INVITATION_TTL_DAYS", default=7)

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.version",
                "core.context_processors.public_base_url",
                "core.context_processors.nav_defaults",
                "members.context_processors.member",
                "subscriptions.context_processors.entitlements",
                "fanclub.context_processors.creator_account",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
# ASGI は不要 (Django は WSGI/gunicorn のみ。WebSocket/gRPC は別プロセス grpcserve に分離)。

# 専用 PostgreSQL (他サービスと共有の PostgreSQL サーバとは別インスタンス)
DATABASES = {
    "default": env.db("DATABASE_URL", default="postgres://icstv:icstv@localhost:5432/icstv"),
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "ja"
TIME_ZONE = "Asia/Tokyo"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
# WhiteNoise で圧縮配信 (Dockerfile の collectstatic で STATIC_ROOT を生成)。manifest 版は
# テンプレ未参照の静的が欠けると 500 になるため、非 manifest の圧縮ストレージを採用。
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

# フロント React アプリ (Phase 0 / Option B = web イメージ同梱)。Vite ビルド成果物
# (frontend/apps/web/dist) を Dockerfile の node ステージが /app/frontend_dist へ配置 →
# collectstatic が STATIC_ROOT/web へ集約 → WhiteNoise が /static/web/ で配信する
# (Vite の base=/static/web/ と一致)。SPA シェル html は /app/ (api.spa.spa_index)。
# ビルド未配置 (ローカル/CI test) でも check/collectstatic が落ちないよう、存在時のみ登録。
FRONTEND_DIST = BASE_DIR / "frontend_dist"
STATICFILES_DIRS = [("web", FRONTEND_DIST)] if FRONTEND_DIST.is_dir() else []

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Logging (構造化 JSON 1 行/ログ。ログ基盤/Zabbix 取込前提) ---
# python-json-logger 等の外部依存を増やさず、標準 logging で JSON 形式を最小実装する。
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            "()": "logging.Formatter",
            "format": (
                '{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":%(message)r}'
            ),
        },
        # icstv.security 専用 (#sec §3)。core.security_log.emit の message は必ず妥当な JSON
        # オブジェクトなので %(message)s で素通しし、msg をネスト JSON にする (ログ基盤側で JSON 展開可)。
        "security_json": {
            "()": "logging.Formatter",
            "format": (
                '{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":%(message)s}'
            ),
        },
    },
    "handlers": {
        "stdout": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "formatter": "json",
        },
        "security_stdout": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "formatter": "security_json",
        },
    },
    "root": {"handlers": ["stdout"], "level": env("LOG_LEVEL", default="INFO")},
    "loggers": {
        # Django access ログは uvicorn/gunicorn 側 (or reverse proxy) に任せる。
        "django.server": {"handlers": ["stdout"], "level": "WARNING", "propagate": False},
        # セキュリティイベント (認証/認可/課金/内部連携) は専用ロガー (#sec §3)。SIEM 向けに分離し、
        # propagate=False で root の json ハンドラへ二重出力しない。
        "icstv.security": {
            "handlers": ["security_stdout"],
            "level": "INFO",
            "propagate": False,
        },
        # 自前 app は INFO 以上を root から取得。
    },
}

# --- Channels (WebSocket・#COMM-01 ライブコメント) ---
# ASGI アプリは config.asgi:application (gunicorn の uvicorn worker で配信)。channel layer は
# 既存 Redis を db 2 で利用 (broker=0 / result=1 と分離)。未設定でも HTTP は通常どおり動く。
ASGI_APPLICATION = "config.asgi.application"
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {"hosts": [env("ICSTV_CHANNEL_REDIS_URL", default="redis://localhost:6379/2")]},
    }
}

# --- Celery (Redis broker) ---
CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="redis://localhost:6379/0")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", default="redis://localhost:6379/1")
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_DEFAULT_QUEUE = "default"
# normalize/captions ワーカーは別キュー(独立スケール)。タスク側でも queue を指定する。
# 字幕自動生成 (transcribe_asset) は faster-whisper が重いため captions 専任 pod へ隔離し、
# normalize を飢餓させない (#ADMIN-04・決定#24)。
CELERY_TASK_ROUTES = {
    "medialib.tasks.normalize_*": {"queue": "normalize"},
    "medialib.tasks.transcribe_*": {"queue": "captions"},
    # 正規化オフロードの振り分け/取り込みは専用 queue へ (default queue の締切系ビート=weather 取り込み
    # や resolver と競合させない・docs/normalize-offload.md / レビュー #4/#10)。軽い判定と I/O 待ちの
    # copy が主で CPU は使わないため小さな専任 worker (offload queue 専任) で捌く。
    "medialib.tasks.dispatch_normalize": {"queue": "offload"},
    "medialib.tasks.reconcile_normalize_offload": {"queue": "offload"},
    # 職員 upload の検証 (multipart 完了後の全体 download + SHA256 + ffprobe) は専任 queue へ
    # (所有者決定G)。normalize は concurrency=1 で encode に実測 17-40 分塞がるため、同じ queue に
    # 積むと encode 中は verify が待たされ、verify 中は正規化が進まない直列競合になる。
    # dispatch_normalize を offload queue へ分けた理由 (軽い/別種の処理を重い処理に相乗りさせない)
    # と同じ論理。専任 worker は verify queue 専任のものを配備基盤側に置く。
    "medialib.tasks.verify_asset_upload": {"queue": "verify"},
}

# --- 長時間タスク(正規化 ffmpeg は実測 17-40 分)の重複実行・取りこぼし対策 ---
# 既定は worker_prefetch_multiplier=4 + acks_late=False。30-40 分の encode が並ぶと、
# 予約だけされて未着手のタスクが visibility_timeout(既定 3600s) 超で「worker が死んだ」と
# 誤判定され別 worker に再配信 → 二重エンコードになる(実測: asset 8 が 3 回 encode された)。
# prefetch=1 にして「いま走っているぶんだけ予約」へ。redelivery 時計が languish 中の
# タスクで進まなくなり、二重エンコードが止まる(最優先・効果大)。
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_ACKS_LATE = True
# worker 消失(デプロイ/OOM/SIGKILL)時にタスクを失わず再配信する。normalize は r2_key を
# asset.id から再導出する冪等処理なので再実行は安全 → "processing 永久滞留" も解消する。
CELERY_TASK_REJECT_ON_WORKER_LOST = True
# 単一タスクの最長実行(番組の長尺 encode は CPU で数時間)より長く取り、走行中タスクの
# 誤再配信を防ぐ。invariant: ffmpeg_timeout ≤ soft ≤ hard < visibility < stale-reaper。
# GPU/preset で encode を短縮できたら各値を締めてよい(全て env で上書き可)。
CELERY_BROKER_TRANSPORT_OPTIONS = {
    "visibility_timeout": env.int("CELERY_VISIBILITY_TIMEOUT", default=25200),  # 7h
}

CELERY_BEAT_SCHEDULE = {
    # リゾルバ: 5 分ごとに [now, now+48h) を再解決。idempotency_key 決定論で diff 反映。
    "scheduling-resolve-all": {
        "task": "scheduling.tasks.resolve_all_channels",
        "schedule": 300.0,
        "kwargs": {"hours": 48},
    },
    # 生放送録画取り込み (#VOD 生放送録画): 1 分ごとに R2 ドロップを scan → Asset 化、
    # READY を Program.recording_asset へ bind。
    "scheduling-ingest-live-recordings": {
        "task": "scheduling.tasks.ingest_live_recordings",
        "schedule": 60.0,
    },
    # YouTube 枠 rolling 生成: 10 分ごとに rolling_hours 先まで埋める (冪等 unique)。
    "youtube-generate-slots-all": {
        "task": "youtube.tasks.generate_slots_all",
        "schedule": 600.0,
    },
    # YouTube 枠遷移: 1 分ごとに 2h 境界を検出 → transition(live/complete)。
    "youtube-rotate-slots-all": {
        "task": "youtube.tasks.rotate_slots_all",
        "schedule": 60.0,
    },
    # YouTube 次枠誘導: 1 分ごとに、終了 nudge_lead_minutes 分前の live 枠から次枠 watch URL へ
    # ライブチャット投稿 + 説明欄追記で誘導 (枠ごとに watch URL が変わる仕様への対応。1 枠 1 回)。
    "youtube-nudge-next-slot-all": {
        "task": "youtube.tasks.nudge_next_slot_all",
        "schedule": 60.0,
    },
    # #23 番組専用枠生成: 5 分ごとに、直近の dedicated 番組へ rolling 枠と並行する専用 broadcast を
    # insert→preset適用→ready (2 本目 liveStream は対象があれば自動プロビジョン)。
    "youtube-generate-dedicated-all": {
        "task": "youtube.tasks.generate_dedicated_broadcasts_all",
        "schedule": 300.0,
    },
    # #23 番組専用枠遷移: 1 分ごとに番組開始/終了で transition(live/complete)。
    "youtube-rotate-dedicated-all": {
        "task": "youtube.tasks.rotate_dedicated_all",
        "schedule": 60.0,
    },
    # agent 死活: 1 分ごとに heartbeat 途絶 (>90s) を検出し online↔offline 遷移を通知 (#7 O7)。
    "playout-check-agent-liveness": {
        "task": "playout.tasks.check_agent_liveness",
        "schedule": 60.0,
    },
    # スレート固着: 1 分ごとに「放送中なのにスレートが継続」を検出して通知する。本線が正常でも
    # スレート層が被れば視聴者には停波と同じだが、送出イベントは成功し続けるため既存の死活/
    # 送出失敗監視には掛からない (誰も見ていなければ固着したまま何時間も続きうる)。
    "playout-check-stuck-slate": {
        "task": "playout.tasks.check_stuck_slate",
        "schedule": 60.0,
    },
    # 放確台帳の日次リコンサイル: DONE なのに airing が無い CM を回収 (broker 不達対策。#6 S7)。
    "sales-reconcile-airings": {
        "task": "sales.tasks.reconcile_airings",
        "schedule": 86400.0,
    },
    # 欠送検知: 毎時 failed/skipped の placement 済みイベントから make_good を起票 (#6 S10)。
    "sales-detect-missed-airings": {
        "task": "sales.tasks.detect_missed_airings",
        "schedule": 3600.0,
    },
    # 週間基本編成の展開: 週次で series_slot を 4 週先まで Program へ (#6 Phase C)。
    "scheduling-expand-series-slots": {
        "task": "scheduling.tasks.expand_series_slots",
        "schedule": 604800.0,
        "kwargs": {"weeks": 4},
    },
    # 正規化 stale 回収: 30 分ごとに、閾値超で processing 滞留した asset を failed 化する。
    # acks_late/reject_on_worker_lost/time_limit の最終防衛線(取りこぼし救済)。
    "medialib-reconcile-stale-normalize": {
        "task": "medialib.tasks.reconcile_stale_normalize",
        "schedule": 1800.0,
    },
    # 正規化 Windows オフロードの取り込み/フォールバック/孤児掃除 (docs/normalize-offload.md)。
    # 60s ごと。offload 無効時は候補ゼロで即 return するので常時有効で无害。
    "medialib-reconcile-normalize-offload": {
        "task": "medialib.tasks.reconcile_normalize_offload",
        "schedule": 60.0,
    },
    # staff multipart uploadの期限切れ・staging object回収。対象はDB台帳のexact keyだけ。
    "medialib-cleanup-asset-uploads": {
        "task": "medialib.tasks.cleanup_asset_uploads",
        "schedule": 300.0,
    },
    # 視聴リマインド: 5 分ごとに、開始 lead_minutes 分前以内に入った予約をメール通知 (一度だけ)。#EPG-03
    "members-send-due-reminders": {
        "task": "members.tasks.send_due_reminders",
        "schedule": 300.0,
        "kwargs": {"lead_minutes": 30},
    },
    "analytics-prune-stale-presence": {
        "task": "analytics.tasks.prune_stale_presence",
        "schedule": 300.0,  # 5 分。視聴在席 (ViewerPresence) の古い行を掃除 (ProgramView は累積保持)
    },
    # アクセスログ (AccessLogEntry) の retention。awstats 的な直近集計にしか使わないので溜めない。
    "analytics-prune-access-log": {
        "task": "analytics.tasks.prune_access_log",
        "schedule": 3600.0 * 6,  # 6 時間ごと
        "kwargs": {"retention_days": 35},
    },
    # クリエイター個人 YouTube チャンネル宛シミュルキャスト (#27 Part B): on-air 窓へ合わせて
    # CF Live Output の enabled を切替える。対象 (active な creator_channel 契約 + ストリーム
    # キー設定済) が無ければ即 return するので常時有効で無害。
    # 周期がそのまま「公開→ファンクラブ限定へ切り替わってから、クリエイター個人の公開 YouTube
    # チャンネルへの中継が止まるまでの最大遅延」になる (docs/fanclub.md §9)。番組境界の到来は
    # DB 書き込みを伴わないためシグナルでは捕捉できず、ここを短くするのが唯一の直接的な手段。
    # 定常時は「対象 creator の抽出 + on-air 判定」の軽量クエリのみで CF API は状態遷移時しか
    # 叩かないため、10 秒でも default queue への負荷はほぼ増えない。
    # expires: CF API (httpx timeout 10s) が詰まって 1 tick が周期を超えたときに、待機中の tick が
    # 積み上がって worker を食い潰さないよう、遅れた tick は実行せず捨てる。冪等なので取りこぼしは
    # 次の tick が回収する。
    "fanclub-reconcile-creator-youtube-outputs": {
        "task": "fanclub.tasks.reconcile_creator_youtube_outputs",
        "schedule": 10.0,
        "options": {"expires": 25.0},
    },
}

# ---- 通知 (docs/operations.md 決定 O3) ----
# notifier (core.notify) の webhook 配送先 (Discord/Slack 互換)。未設定なら WebhookBackend は no-op。
ICSTV_NOTIFY_WEBHOOK_URL = env("ICSTV_NOTIFY_WEBHOOK_URL", default="")

# ---- メール送信 (会員のメール検証/2FA。#会員管理) ----
# 本番は Mailgun SMTP を env で差す (追加依存なし)。dev=console (stdout にコード出力)、
# test=locmem。整うまで本番のメール検証は未送達=会員は未認証のまま使える (仕様どおり)。
EMAIL_BACKEND = env(
    "DJANGO_EMAIL_BACKEND", default="django.core.mail.backends.console.EmailBackend"
)
EMAIL_HOST = env("DJANGO_EMAIL_HOST", default="smtp.mailgun.org")
EMAIL_PORT = env.int("DJANGO_EMAIL_PORT", default=587)
EMAIL_HOST_USER = env("DJANGO_EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("DJANGO_EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = env.bool("DJANGO_EMAIL_USE_TLS", default=True)
DEFAULT_FROM_EMAIL = env("DJANGO_DEFAULT_FROM_EMAIL", default="")  # 配送する backend では必須

# 会員のメール検証/2FA コードのパラメータ (全て env 上書き可)
ICSTV_MEMBER_CODE_TTL = env.int("ICSTV_MEMBER_CODE_TTL", default=600)  # コード有効秒 (10分)
ICSTV_MEMBER_CODE_MAX_ATTEMPTS = env.int("ICSTV_MEMBER_CODE_MAX_ATTEMPTS", default=5)
ICSTV_MEMBER_CODE_SEND_COOLDOWN = env.int(
    "ICSTV_MEMBER_CODE_SEND_COOLDOWN", default=60
)  # 再送間隔秒
ICSTV_MEMBER_CODE_SEND_MAX_PER_HOUR = env.int("ICSTV_MEMBER_CODE_SEND_MAX_PER_HOUR", default=8)

# ---- 認証系の Cookie 非依存レート制限 (H-1 / M-1・members.throttle) ----
# 従来の session 依存 throttle は Cookie を捨てれば無効化できた。email (HMAC) と IP の両系統で
# DB に失敗回数を数える。email 系を主防御 (上限低め)、IP 系は XFF 偽装余地があるため上限高め。
ICSTV_LOGIN_FAIL_MAX = env.int("ICSTV_LOGIN_FAIL_MAX", default=5)  # email 単位の上限
ICSTV_LOGIN_IP_FAIL_MAX = env.int("ICSTV_LOGIN_IP_FAIL_MAX", default=20)  # IP 単位の上限
ICSTV_LOGIN_FAIL_WINDOW = env.int("ICSTV_LOGIN_FAIL_WINDOW", default=900)  # ウィンドウ秒 (15分)
# TOTP 第2要素の試行上限 (M-1)。パスワード通過後の総当たり/2FA バイパスを防ぐ。
ICSTV_TOTP_FAIL_MAX = env.int("ICSTV_TOTP_FAIL_MAX", default=5)  # member 単位の上限
ICSTV_TOTP_IP_FAIL_MAX = env.int("ICSTV_TOTP_IP_FAIL_MAX", default=20)  # IP 単位の上限
ICSTV_TOTP_FAIL_WINDOW = env.int("ICSTV_TOTP_FAIL_WINDOW", default=900)  # ウィンドウ秒
# 実クライアント IP を読む META キー。CF Tunnel→ingress の構成に応じ本番で上書き可
# (例: CF-Connecting-IP を転写しているなら "HTTP_CF_CONNECTING_IP")。既定は XFF 最左。
ICSTV_CLIENT_IP_HEADER = env("ICSTV_CLIENT_IP_HEADER", default="HTTP_X_FORWARDED_FOR")

# ---- Stripe (視聴者サブスク課金。サブスク) ----
# 機密 (secret/webhook secret) は配備基盤側の Secret 由来。publishable は非機密。空なら課金は無効状態
# (ページは出るが Checkout で 503)。Stripe ダッシュボードで Products/Prices(松竹梅)・Webhook を設定する。
STRIPE_SECRET_KEY = env("STRIPE_SECRET_KEY", default="")
STRIPE_PUBLISHABLE_KEY = env("STRIPE_PUBLISHABLE_KEY", default="")
STRIPE_WEBHOOK_SECRET = env("STRIPE_WEBHOOK_SECRET", default="")

# ---- Stripe Connect (クリエイターFC有料ティア。#27 Phase B) ----
# STRIPE_SECRET_KEY を共用する同一 Stripe アカウント上の Connect Express。プラットフォームが
# Checkout で徴収し、手数料(STRIPE_CONNECT_APPLICATION_FEE_PERCENT)を差し引いた残額を
# クリエイターの connected account へ自動送金する(destination charge)。資金の滞留を作らない。
# webhook は視聴者サブスクと別エンドポイント/別secretで Stripe ダッシュボードに登録する想定。
STRIPE_FC_WEBHOOK_SECRET = env("STRIPE_FC_WEBHOOK_SECRET", default="")
STRIPE_CONNECT_APPLICATION_FEE_PERCENT = env.float(
    "STRIPE_CONNECT_APPLICATION_FEE_PERCENT", default=10.0
)

# ---- 本番 fail-closed 設定ガード (#sec M-5/M-6/I-4) ----
# 本番は配備基盤側の ConfigMap 相当で ICSTV_REQUIRE_SECRETS=true を設定する。SECRET_KEY / 暗号鍵 /
# DB 資格情報が既定値 (=未設定) のまま起動しようとしたら ImproperlyConfigured で止める。
# テスト/ローカル/CI は既定 False で不介入 (これらは既定値のまま DEBUG=False で走るため)。
if env.bool("ICSTV_REQUIRE_SECRETS", default=False):
    from config.checks import enforce_secure_config

    enforce_secure_config(
        secret_key=SECRET_KEY,
        field_encryption_key=ICSTV_FIELD_ENCRYPTION_KEY,
        database_url=env("DATABASE_URL", default=""),
    )

if ICSTV_DISTRIBUTION_MODE:
    from config.checks import enforce_distribution_config

    enforce_distribution_config(
        debug=DEBUG,
        secret_key=SECRET_KEY,
        field_encryption_key=ICSTV_FIELD_ENCRYPTION_KEY,
        database_url=env("DATABASE_URL", default=""),
        hls_signing_key=ICSTV_HLS_SIGNING_KEY,
        hls_token_ttl_sec=ICSTV_HLS_TOKEN_TTL_SEC,
        allowed_hosts=ALLOWED_HOSTS,
    )

# ---- 運営者表示 (特定商取引法11条相当) の fail-closed 設定ガード ----
# 公開運用へ進める段になったら deploy 側で ICSTV_REQUIRE_OPERATOR_INFO=true を設定する運用を
# 想定 (ICSTV_REQUIRE_SECRETS と同じ考え方)。テスト/ローカル/CI は既定 False で不介入。
if env.bool("ICSTV_REQUIRE_OPERATOR_INFO", default=False):
    from config.checks import enforce_operator_config

    enforce_operator_config(
        legal_name=ICSTV_OPERATOR_LEGAL_NAME,
        representative_name=ICSTV_OPERATOR_REPRESENTATIVE_NAME,
        address=ICSTV_OPERATOR_ADDRESS,
        phone=ICSTV_OPERATOR_PHONE,
        hide_contact_details=ICSTV_OPERATOR_HIDE_CONTACT_DETAILS,
        contact_email=ICSTV_OPERATOR_CONTACT_EMAIL,
    )

# ---- メール送信元の fail-closed 設定ガード ----
# DEFAULT_FROM_EMAIL の既定は空。配送する backend (smtp など) で送信元が空のまま起動しようと
# したら ImproperlyConfigured で止める。他のガードと違い ICSTV_REQUIRE_* のフラグを設けず常に
# 検証する。配送しない backend (console/locmem/dummy/filebased) は dev/test/CI の既定なので
# 影響を受けない。配送する backend で送信元が空ならメール送信時に失敗するので、起動時に
# 止めるのはその失敗を前倒しで検出することにあたる。
# 本番は配備基盤側の ConfigMap 相当で DJANGO_EMAIL_BACKEND と DJANGO_DEFAULT_FROM_EMAIL の両方を設定する。
from config.checks import enforce_mail_config  # noqa: E402

enforce_mail_config(email_backend=EMAIL_BACKEND, default_from_email=DEFAULT_FROM_EMAIL)
