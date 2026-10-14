# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""API が OpenAPI 仕様どおりに応答することを検査する (schemathesis)。

仕様から生成した入力を in-process の WSGI アプリへ送り、次を確かめる。

- 5xx を返さない (`not_a_server_error`)
- 応答のステータス・content-type・body が仕様に適合する (`status_code_conformance` ほか)

対象は次のとおり。副作用のない GET に限り、認証付きは既存 fixture の認証情報で呼ぶ。

- security を持たない operation (全メソッド)
- staff セッションで通る GET (`staff_client` のセッション cookie)
- 会員トークンで通る GET (`api_tokens.issue` のトークン)

パスパラメータ (slug / ID 系) は `seeded` fixture の実在オブジェクトに差し替え、2xx の応答形式まで
検査する。新しい ID 系パラメータが増えたら `test_seeded_covers_every_path_parameter` が落ちる。

次は未対応。トークン系 (Backoffice / Delivery / Monitor の GET)、副作用のある POST / PUT /
DELETE (テストデータの後始末と、入力の生成範囲を決める必要がある)。
"""

import contextlib
from types import SimpleNamespace

import pytest
import schemathesis
from django.conf import settings as dj_settings
from django.core import signals
from django.db import close_old_connections
from django.test import override_settings
from django.utils import timezone
from hypothesis import HealthCheck, settings
from schemathesis.generation import GenerationMode
from schemathesis.specs.openapi.checks import ignored_auth

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
    from fanclub.models import Creator, CreatorSeriesLink
    from medialib import models as medialib_models
    from scheduling.models import (
        AudienceForm,
        AudienceFormKind,
        AudienceSubmission,
        Program,
        ProgramType,
        Series,
        SeriesSlot,
    )
    from youtube.models import YoutubeBroadcastPreset

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
    AudienceSubmission.objects.create(form=form, payload={"message": "検査"})
    preset = YoutubeBroadcastPreset.objects.create(name="検査プリセット")
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
}
_BOUND_PARAMS = {*_ID_PARAMS, "slug", "owner", "owner_id"}


def test_seeded_covers_every_path_parameter():
    """認証付き GET のパスパラメータは、すべて seeded で差し替えられること。

    新しい ID 系パラメータを持つ operation が増えたら、ここで失敗させる。放置すると生成された
    存在しない ID が 404 になるだけで、2xx の応答形式が検査されないまま通ってしまう。
    """
    unbound = {
        f"{op.label}: {name}"
        for op in _schema.get_all_operations()
        if (r := op.ok()) is not None
        for op in [r]
        if _staff_get(SimpleNamespace(operation=op)) or _member_get(SimpleNamespace(operation=op))
        for name in (p.name for p in op.path_parameters)
        if name not in _BOUND_PARAMS
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
