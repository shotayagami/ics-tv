# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""API が OpenAPI 仕様どおりに応答することを検査する (schemathesis)。

仕様から生成した入力を in-process の WSGI アプリへ送り、次を確かめる。

- 5xx を返さない (`not_a_server_error`)
- 応答のステータス・content-type・body が仕様に適合する (`status_code_conformance` ほか)

対象は全 operation で、認証の種類ごとにテストを分けている。

- security を持たない operation (全メソッド)
- staff セッション (GET は読み取り、POST / PUT / DELETE は CSRF トークン付き)
- 会員トークン (GET と書き込み)
- 共有トークン (`X-Internal-Token`。Backoffice / Delivery / Monitor / 天気 / 地震 / ランキング)

パスパラメータ (slug / ID 系) は `seeded` fixture の実在オブジェクトに差し替え、2xx の応答形式まで
検査する。新しい ID 系パラメータが増えたら `test_seeded_covers_every_path_parameter` が落ちる。

副作用のある操作は 2 重に抑える。呼び出しごとに savepoint を張って必ずロールバックし、外部
ネットワークへの接続はテスト中に禁止する (オブジェクトストレージは `_FakeS3` に差し替え)。
外へ出る経路があれば 5xx として検出される。

書き込み系・トークン系では `positive_data_acceptance` と `negative_data_rejection` を除外する
(理由は `_LAX_CHECKS`)。5xx・応答の形・宣言外のステータスは検査する。
"""

import contextlib
import io
import socket
from types import SimpleNamespace

import pytest
import schemathesis
from django.conf import settings as dj_settings
from django.core import signals
from django.db import close_old_connections, transaction
from django.test import override_settings
from django.utils import timezone
from hypothesis import HealthCheck, settings
from schemathesis.generation import GenerationMode
from schemathesis.specs.openapi.checks import (
    ignored_auth,
    negative_data_rejection,
    positive_data_acceptance,
)

from api.management.commands.dump_openapi import _merged_schema
from config.wsgi import application

# werkzeug のテストクライアントは、自前の cookie jar を持つため、リクエストの Cookie ヘッダを
# 捨てる (ヘッダで渡すと黙って未認証 = 401 になる)。認証用の cookie は別名ヘッダで渡し、
# アプリの手前で HTTP_COOKIE に戻す。
_COOKIE_HEADER = "X-Test-Cookie"


def _app_with_cookie_header(environ, start_response):
    forwarded = environ.pop("HTTP_X_TEST_COOKIE", None)
    if forwarded:
        environ["HTTP_COOKIE"] = forwarded
    return application(environ, start_response)


_schema = schemathesis.openapi.from_dict(_merged_schema())
_schema.app = _app_with_cookie_header
# security_parameters: 認証 cookie は上記の別名ヘッダで渡すので、schemathesis に生成・欠落
# させない (欠落を「不正入力」と見なして受理を誤検出する)。
# allow_x00: NUL 文字は JSON として正当だが PostgreSQL の text に入らず、`DataError` を 400 に
# している。仕様は文字列の内容を制約しないため「受理すべき入力」と判定される。
# ponytail: NUL の扱いは test_openapi_errors の回帰テストで固定。仕様側で表現できたら外す
_schema.config.generation.update(with_security_parameters=False, allow_x00=False)


@pytest.fixture(autouse=True)
def _keep_db_connection():
    """WSGI ハンドラは request_started/finished で接続を閉じ、テストのトランザクションが
    壊れる。Django のテストクライアントと同じく、テスト中だけ切り離す。"""
    signals.request_started.disconnect(close_old_connections)
    signals.request_finished.disconnect(close_old_connections)
    yield
    signals.request_started.connect(close_old_connections)
    signals.request_finished.connect(close_old_connections)


def _alternatives(ctx) -> list[set[str]]:
    """security は OR の配列 (各要素のスキーム名の集合)。"""
    return [set(a) for a in ctx.operation.definition.raw.get("security", [])]


def _is_public(ctx) -> bool:
    return not ctx.operation.definition.raw.get("security")


def _is_get(ctx) -> bool:
    return ctx.operation.method.upper() == "GET"


def _staff_get(ctx) -> bool:
    return _is_get(ctx) and {"SessionAuthIsStaff"} in _alternatives(ctx)


def _member_get(ctx) -> bool:
    return _is_get(ctx) and {"MemberTokenAuth"} in _alternatives(ctx)


def _assert_authenticated(response) -> None:
    """未認証 (401) のまま「仕様に適合」と判定される空振りを防ぐ。

    401 は認証が効いていないことを意味する。認証の効いた呼び出しが 401 を返したら失敗にする。
    """
    assert response.status_code != 401, "認証が効いていない (401)"


_SETTINGS = settings(max_examples=15, deadline=None, suppress_health_check=list(HealthCheck))
# テストクライアントの既定ホスト (testserver) は管理ホスト扱いで api/v1/ が解決される
_HOST = {"Host": "testserver"}


@_schema.include(func=_is_public).parametrize()
@_SETTINGS
@pytest.mark.django_db
def test_public_operation_conforms_to_spec(case):
    case.call_and_validate(headers=_HOST)


@pytest.fixture
def seeded(db, channel, asset_ready, staff_user):
    """パスパラメータ (ID 系) に差し込む、実在のオブジェクト一式。

    生成された ID は存在せず 404 になるだけで、2xx の応答形式を検査できない。fixture は
    関数あたり 1 回だけ実行される (hypothesis は例ごとに関数を繰り返し呼ぶ)。
    """
    from datetime import date, time, timedelta

    from fanclub import services as fc_services
    from fanclub.models import (
        Creator,
        CreatorInvitation,
        CreatorSeriesLink,
        CreatorTier,
        SlotContract,
    )
    from medialib import models as medialib_models
    from scheduling.models import (
        AudienceForm,
        AudienceFormKind,
        AudienceSubmission,
        Episode,
        Program,
        ProgramType,
        Series,
        SeriesPost,
        SeriesSlot,
    )
    from youtube.models import (
        ProgramBroadcast,
        YoutubeBroadcastPreset,
        YoutubeDescriptionTemplate,
    )

    creator = Creator.objects.create(name="検査サークル", slug="conformance-circle")
    fc_services.ensure_free_tier(creator)
    series = Series.objects.create(channel=channel, title="検査シリーズ")
    CreatorSeriesLink.objects.create(series=series, creator=creator)
    slot = SeriesSlot.objects.create(
        series=series,
        dow=0,
        start_time=time(12, 0),
        duration_ms=30 * 60 * 1000,
        program_type=ProgramType.RECORDED,
        default_asset=asset_ready,
        effective_from=date(2026, 1, 1),
    )
    end = timezone.now() - timedelta(hours=2)
    program = Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title="検査番組",
        start_at=end - timedelta(hours=1),
        end_at=end,
        asset=asset_ready,
    )
    form = AudienceForm.objects.create(
        series=series, kind=AudienceFormKind.MESSAGE, title="検査フォーム", enabled=True
    )
    submission = AudienceSubmission.objects.create(form=form, payload={"message": "検査"})
    preset = YoutubeBroadcastPreset.objects.create(name="検査プリセット")
    today = date(2026, 6, 1)
    contract = SlotContract.objects.create(
        creator=creator, title="検査契約", monthly_fee_minor=1000, starts_on=today
    )
    tier = CreatorTier.objects.create(creator=creator, level=1, name="検査ティア")
    invitation = CreatorInvitation.objects.create(
        creator=creator,
        email="invited@example.com",
        token="conformance-invitation-token",  # pragma: allowlist secret - test only
        expires_at=timezone.now() + timedelta(days=7),
    )
    episode = Episode.objects.create(series=series)
    post = SeriesPost.objects.create(series=series, title="検査投稿")
    template = YoutubeDescriptionTemplate.objects.create(name="検査テンプレート")
    program_broadcast = ProgramBroadcast.objects.create(program=program, broadcast_id="BC")
    cost = invoice = None
    with contextlib.suppress(ImportError):  # 調達のモデルは配布版で異なる
        from procurement.models import DeliveryCost

        cost = DeliveryCost.objects.create(
            delivery_id=1, delivery_title="検査", series=series, amount=1000, incurred_on=today
        )
    with contextlib.suppress(ImportError):
        from billing.models import BillingPeriod, Invoice, InvoiceStatus
        from sales.models import AdContract, Advertiser, ContractKind, Industry

        industry = Industry.objects.create(code="conformance", name="検査")
        advertiser = Advertiser.objects.create(name="検査広告主", industry=industry)
        ad_contract = AdContract.objects.create(
            kind=ContractKind.SPOT,
            advertiser=advertiser,
            title="検査契約",
            period_start=today,
            period_end=date(2026, 6, 30),
        )
        invoice = Invoice.objects.create(
            invoice_number="INV-CONFORMANCE-0001",
            period=BillingPeriod.objects.create(year=2026, month=6),
            contract=ad_contract,
            bill_to_agency=False,
            subtotal=1000,
            commission_amount=0,
            tax_amount=100,
            total=1100,
            status=InvoiceStatus.DRAFT,
        )
    upload = None
    if hasattr(medialib_models, "AssetUpload"):  # ブラウザ upload は配布版だけにある
        upload = medialib_models.AssetUpload.objects.create(
            owner=staff_user,
            kind=medialib_models.AssetKind.PROGRAM,
            title="検査アップロード",
            original_filename="conformance.mp4",
            content_type="video/mp4",
            expected_size_bytes=1,
            expected_sha256="0" * 64,
            staging_key="staging/conformance",
            idempotency_key_hash="0" * 64,
            request_fingerprint="0" * 64,
            expires_at=timezone.now() + timedelta(hours=1),
        )
    return SimpleNamespace(
        upload=upload,
        contract=contract,
        tier=tier,
        invitation=invitation,
        episode=episode,
        post=post,
        template=template,
        program_broadcast=program_broadcast,
        cost=cost,
        invoice=invoice,
        submission=submission,
        channel=channel,
        creator=creator,
        series=series,
        slot=slot,
        program=program,
        form=form,
        asset=asset_ready,
        preset=preset,
    )


# パスパラメータ名 → seeded から値を取り出す関数。slug / owner / owner_id は個別に扱う
_ID_PARAMS = {
    "creator_id": lambda s: s.creator.pk,
    "series_id": lambda s: s.series.pk,
    "form_id": lambda s: s.form.pk,
    "program_id": lambda s: s.program.pk,
    "slot_id": lambda s: s.slot.pk,
    "asset_id": lambda s: s.asset.pk,
    "preset_id": lambda s: s.preset.pk,
    "upload_id": lambda s: s.upload.pk,  # 配布版だけにある AssetUpload
    "contract_id": lambda s: s.contract.pk,
    "tier_id": lambda s: s.tier.pk,
    "invitation_id": lambda s: s.invitation.pk,
    "episode_id": lambda s: s.episode.pk,
    "post_id": lambda s: s.post.pk,
    "template_id": lambda s: s.template.pk,
    "pb_id": lambda s: s.program_broadcast.pk,
    "cost_id": lambda s: s.cost.pk,
    "invoice_id": lambda s: s.invoice.pk,
    "sub_id": lambda s: s.submission.pk,
    "cid": lambda s: s.comment.pk,  # member_token fixture が追加する
    "message_id": lambda s: s.chat_message.pk,  # 同上
}
_BOUND_PARAMS = {*_ID_PARAMS, "slug", "owner", "owner_id"}


def _covered_by_bound_tests(ctx) -> bool:
    """seeded でパスパラメータを差し替える (staff / 会員 / トークン) 検査の対象か。"""
    return (
        _staff_get(ctx)
        or _member_get(ctx)
        or _staff_write(ctx)
        or _member_write(ctx)
        or _token_op(ctx)
    )


def test_seeded_covers_every_path_parameter():
    """検査対象のパスパラメータは、すべて seeded で差し替えられること。

    新しい ID 系パラメータを持つ operation が増えたら、ここで失敗させる。放置すると生成された
    存在しない ID が 404 になるだけで、2xx の応答形式が検査されないまま通ってしまう。
    """
    unbound = {
        f"{op.label}: {param.name}"
        for result in _schema.get_all_operations()
        if (op := result.ok()) is not None
        if _covered_by_bound_tests(SimpleNamespace(operation=op))
        for param in op.path_parameters
        if param.name not in _BOUND_PARAMS
    }
    assert not unbound, f"seeded fixture で扱えないパスパラメータ: {sorted(unbound)}"


def _bind_path_params(case, seeded) -> None:
    """生成されたパスパラメータを、実在のオブジェクトの値に差し替える。

    負例 (不正な値を意図して生成したケース) は差し替えない。差し替えると有効な入力になり、
    「不正入力が拒否されること」を期待する検査と矛盾する。
    """
    params = case.path_parameters
    if not params or (case.meta and case.meta.generation.mode == GenerationMode.NEGATIVE):
        return
    for key, getter in _ID_PARAMS.items():
        if key in params:
            params[key] = getter(seeded)
    if "owner" in params:  # graphic-cues/{owner}/{owner_id}
        params["owner"] = "series"
        params["owner_id"] = seeded.series.pk
    if "slug" in params:
        fanclub = case.operation.path.startswith("/api/v1/fc/")
        params["slug"] = seeded.creator.slug if fanclub else seeded.channel.slug


@_schema.include(func=_staff_get).parametrize()
@_SETTINGS
@pytest.mark.django_db
def test_staff_get_conforms_to_spec(case, staff_client, seeded):
    name = dj_settings.SESSION_COOKIE_NAME
    _bind_path_params(case, seeded)
    cookie = f"{name}={staff_client.cookies[name].value}"
    response = case.call(headers={**_HOST, _COOKIE_HEADER: cookie})
    _assert_authenticated(response)
    # ignored_auth は認証を外して再送する検査。認証を別名ヘッダで渡している (上記) ため
    # 外せず、常に「認証なしで通る」と誤検出する。401 の宣言は test_openapi_errors が見る
    case.validate_response(response, excluded_checks=[ignored_auth])


@pytest.fixture
def member_token(db, request, seeded):
    """会員とそのトークン。hypothesis は 1 つの関数を例ごとに繰り返し呼ぶため、
    unique 制約のある行は関数内でなく fixture (関数あたり 1 回) で作る。"""
    with contextlib.suppress(pytest.FixtureLookupError):
        request.getfixturevalue("owner_tenant")  # テナント分離のあるツリーだけに存在する
    from members import api_tokens
    from members.models import Member

    member = Member.objects.create(
        email="conformance@example.com",
        nickname="検査",
        birth_year=1990,
        birth_month=4,
        postal_code="1000001",
        email_verified_at=timezone.now(),
    )
    # fc/{slug}/chat は、そのクリエイターの会員だけが読める
    from fanclub.models import CreatorMembership, CreatorTier, MembershipStatus

    CreatorMembership.objects.create(
        member=member,
        creator=seeded.creator,
        tier=CreatorTier.objects.filter(creator=seeded.creator).order_by("id").first(),
        status=MembershipStatus.ACTIVE,
        joined_at=timezone.now(),
    )
    # 会員自身の投稿 (DELETE /comments/{cid}, DELETE /fc/{slug}/chat/{message_id} の対象)
    from fanclub.models import FcChatMessage
    from members.models import Comment

    seeded.comment = Comment.objects.create(channel=seeded.channel, member=member, body="検査")
    seeded.chat_message = FcChatMessage.objects.create(
        creator=seeded.creator, member=member, body="検査"
    )
    return api_tokens.issue(member)


@_schema.include(func=_member_get).parametrize()
@_SETTINGS
@pytest.mark.django_db
@override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])
def test_member_get_conforms_to_spec(case, member_token, seeded):
    _bind_path_params(case, seeded)
    response = case.call(headers={**_HOST, "Authorization": f"Bearer {member_token}"})
    _assert_authenticated(response)
    # ignored_auth は認証を外して再送する検査。認証を別名ヘッダで渡している (上記) ため
    # 外せず、常に「認証なしで通る」と誤検出する。401 の宣言は test_openapi_errors が見る
    case.validate_response(response, excluded_checks=[ignored_auth])


# ---- 副作用のある操作 (POST / PUT / PATCH / DELETE) とトークン認証 ----
#
# 副作用は 2 重に抑える。(1) 呼び出しごとに savepoint を張って必ずロールバックし、DB を残さない
# (DELETE の対象も例ごとに復元される)。(2) 外部ネットワークへの接続をテスト中は禁止し、外へ出る
# 経路は 5xx として検出する (課金・メール送信・外部 API 呼び出しを誤って起こさない)。


@pytest.fixture(autouse=True)
def _block_external_network(monkeypatch):
    real_connect = socket.socket.connect

    def guarded(self, address):
        host = address[0] if isinstance(address, tuple) else None
        if host is not None and host not in {"127.0.0.1", "::1", "localhost"}:
            raise OSError(f"テスト中の外部ネットワーク接続は禁止: {host}")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)


class _FakeS3:
    """core.r2.client() の差し替え。オブジェクトストレージへ実際に書き込まない。"""

    def generate_presigned_url(self, *args, **kwargs):
        return "https://r2.invalid/presigned"

    def get_object(self, *args, **kwargs):
        return {"Body": io.BytesIO(b""), "ContentType": "application/octet-stream"}

    def create_multipart_upload(self, *args, **kwargs):
        return {"UploadId": "conformance-upload"}

    def __getattr__(self, name):  # put_object / delete_object / head_object など
        return lambda *args, **kwargs: {}


@pytest.fixture(autouse=True)
def _fake_object_storage(monkeypatch):
    monkeypatch.setattr("core.r2.client", lambda: _FakeS3())
    monkeypatch.setattr("core.r2.bucket", lambda: "conformance-bucket")


# 書き込み系・トークン系で除外するチェック。いずれも仕様が表現しない意味的な検証・型変換で、
# 実装の欠陥ではなく仕様の粒度の問題として現れる。5xx・応答の形・宣言外のステータスは検査する。
# - positive_data_acceptance: 日付の範囲や業務ルールなど、仕様に無い検証で 400/422 を返す
# - negative_data_rejection: pydantic の lax モードが "12" → 12 のように型を変換して受理する
_LAX_CHECKS = [ignored_auth, positive_data_acceptance, negative_data_rejection]


def _unsendable(case) -> bool:
    """werkzeug のテストクライアントが送れない負例か。

    multipart の body にオブジェクト以外 (`True` など) が生成されると、クライアント側で
    AttributeError になる (アプリには届かない)。正例と、dict の負例は送る。
    """
    media = (case.media_type or "").lower()
    return media.startswith("multipart/") and not isinstance(case.body, dict)


def _call_rolled_back(case, **kwargs):
    """case を送り、DB への変更を (成功・失敗にかかわらず) 巻き戻す。"""
    with transaction.atomic():
        response = case.call(**kwargs)
        transaction.set_rollback(True)
    return response


def _csrf_pair() -> tuple[str, str]:
    """(cookie の値, X-CSRFToken ヘッダの値)。ninja のセッション認証は書き込みに CSRF を要求する。"""
    # Django の公開 API はリクエストが要るため、トークン生成の内部関数を使う (型スタブに無い)
    from django.middleware import csrf

    secret = csrf._get_new_csrf_string()
    return secret, csrf._mask_cipher_secret(secret)  # type: ignore[attr-defined]


# 共有トークン認証のスキーム名 → 設定名 (api/routers/internal.py)
_TOKEN_SETTINGS = {
    "InternalToken": "WEATHER_IMPORT_TOKEN",
    "RankingToken": "RANKING_IMPORT_TOKEN",
    "EarthquakeToken": "EARTHQUAKE_FIRE_TOKEN",
    "DeliveryToken": "DELIVERY_REGISTER_TOKEN",
    "BackofficeToken": "BACKOFFICE_READ_TOKEN",
    "MonitorToken": "MONITOR_READ_TOKEN",
}
_TEST_TOKEN = "conformance-token"  # pragma: allowlist secret - test only


def _is_write(ctx) -> bool:
    return not _is_get(ctx)


def _token_op(ctx) -> bool:
    return any(a & set(_TOKEN_SETTINGS) for a in _alternatives(ctx))


def _staff_write(ctx) -> bool:
    return _is_write(ctx) and {"SessionAuthIsStaff"} in _alternatives(ctx)


def _member_write(ctx) -> bool:
    return _is_write(ctx) and {"MemberTokenAuth"} in _alternatives(ctx)


@_schema.include(func=_token_op).parametrize()
@_SETTINGS
@pytest.mark.django_db
@override_settings(**dict.fromkeys(_TOKEN_SETTINGS.values(), _TEST_TOKEN))
def test_token_operation_conforms_to_spec(case, seeded):
    if _unsendable(case):
        return
    _bind_path_params(case, seeded)
    response = _call_rolled_back(case, headers={**_HOST, "X-Internal-Token": _TEST_TOKEN})
    _assert_authenticated(response)
    case.validate_response(response, excluded_checks=_LAX_CHECKS)


@_schema.include(func=_staff_write).parametrize()
@_SETTINGS
@pytest.mark.django_db
def test_staff_write_conforms_to_spec(case, staff_client, seeded):
    if _unsendable(case):
        return
    _bind_path_params(case, seeded)
    sid = staff_client.cookies[dj_settings.SESSION_COOKIE_NAME].value
    secret, header = _csrf_pair()
    cookie = f"{dj_settings.SESSION_COOKIE_NAME}={sid}; {dj_settings.CSRF_COOKIE_NAME}={secret}"
    headers = {**_HOST, _COOKIE_HEADER: cookie, "X-CSRFToken": header}
    response = _call_rolled_back(case, headers=headers)
    _assert_authenticated(response)
    case.validate_response(response, excluded_checks=_LAX_CHECKS)


@_schema.include(func=_member_write).parametrize()
@_SETTINGS
@pytest.mark.django_db
@override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])
def test_member_write_conforms_to_spec(case, member_token, seeded):
    if _unsendable(case):
        return
    _bind_path_params(case, seeded)
    headers = {**_HOST, "Authorization": f"Bearer {member_token}"}
    response = _call_rolled_back(case, headers=headers)
    _assert_authenticated(response)
    case.validate_response(response, excluded_checks=_LAX_CHECKS)
