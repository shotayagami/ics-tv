# API 仕様と適合検査

> ステータス: **実装済み**(2026-10-16 改訂)。OpenAPI の仕様は実装から生成し、CI で検査している。
> ドキュメントの正は実装であり、ここには場所と手順、確かめた挙動を記す。仕様の実体は `openapi.json`、実装は `server/api/` にある。

## 1. 仕様の場所と生成

本体の JSON API は django-ninja で実装している。OpenAPI 3.1 の仕様は実装から生成し、リポジトリ直下の `openapi.json` に置く。公開 API と、管理画面・内部連携用の API は別々の `NinjaAPI` で実装しており、`openapi.json` は両方を 1 つにまとめた全体の仕様である。

実行中のサーバーは `/api/v1/openapi.json` と `/api/v1/docs` でも仕様を返す。ただし、返すのは公開 API の仕様だけで、管理 API と内部連携は含まない。同じ接頭辞に 2 つの API をマウントしており、先にマウントした公開 API が応答するためである。

仕様と TypeScript の型は次のコマンドで再生成する。

```bash
cd server && python manage.py dump_openapi --out ../openapi.json
cd ../frontend && npm run gen:api
```

2 行目は `frontend/packages/api/src/schema.ts` の生成である。CI は両方を再生成して差分を検出するため、コードを変えて仕様が変わったのに生成物をコミットしていないと失敗する。あわせて `openapi-spec-validator` で、仕様そのものが OpenAPI として有効かを検査する。

## 2. 認証方式

仕様の `securitySchemes` に認証方式を定義している。各 operation の `security` は受け付ける方式を示し、並んだ方式のどれか 1 つを満たせばよい。

| 方式 | 送り方 | 用途 |
|---|---|---|
| セッション (staff) | cookie `sessionid`。書き込みには `X-CSRFToken` ヘッダも要る | 管理画面 (SPA) |
| 会員 | cookie `sessionid`、または `Authorization: Bearer <token>`。Bearer は CSRF トークンが要らない | 視聴者向けの機能、アプリ |
| 共有トークン | `X-Internal-Token` ヘッダ。用途ごとに別の設定値を持つ | 別サービスからの連携 (納品、地震速報、バックオフィス、監視など) |

## 3. エラー形式

エラー応答はすべて `{"detail": "<文字列>"}` の形で返す。SPA は `detail` をそのまま画面に出すため、文字列に統一している。

入力検証の失敗 (422) では `detail` に項目名と理由を 1 行にまとめた文字列を入れ、pydantic の詳細を `errors` に載せる。

```json
{
  "detail": "email は必須です; password は必須です",
  "errors": [{"type": "missing", "loc": ["body", "payload", "email"], "msg": "Field required"}]
}
```

ステータスの意味は次のとおりである。

| ステータス | 意味 |
|---|---|
| 400 | DB が受け付けない入力 (NUL 文字など)、または操作ごとの入力エラー |
| 401 | 認証されていない |
| 403 | 権限がない |
| 404 | 対象がない。パスの型が合わない場合も JSON で返す |
| 409 | 状態が合わない、または DB の制約に反する |
| 422 | 入力の検証に失敗した |
| 429 | 試行回数の上限に達した |
| 500 | 想定外のエラー。`{"detail": "Internal Server Error"}` を返す |

仕様には全 operation に `default` (`ErrorOut`) を、認証が要る operation に `401` を、入力を持つ operation に `422` (`ValidationErrorOut`) を宣言している。操作ごとに返すその他のステータスは、`default` で受ける。

実装は `server/api/openapi_errors.py` (仕様への宣言と例外ハンドラ) と `server/api/error_views.py` (`/api/` 配下の 404 と 500) にある。

## 4. 入力の検証

仕様に書いた型と制約は、実行時にも検証する。

- 必須のテキストは空文字と空白だけの値を拒否する。仕様では `pattern: \S` と表す。
- 整数と実数は、JSON の `true` と `false` を受け付けない。真偽値は `0` と `1` を受け付けない。数値の文字列 (`"12"`) は整数として受け付ける。
- 日付や時刻など、仕様に載せていない書式は、各操作が検証する。

仕様に合う入力でも、4xx を返す操作がある。参照先が存在しない、状態が合わない、項目間の規則に反するといった理由で、OpenAPI では表現できない。

## 5. 適合検査

実装が仕様どおりに応答することを、schemathesis で検査する。テストは `server/tests/api/test_openapi_conformance.py` と `server/tests/api/test_openapi_errors.py` にある。PostgreSQL が要るため、リポジトリのテスト用サービスで実行する。

```bash
docker compose --profile test run --rm test pytest tests/api/test_openapi_conformance.py tests/api/test_openapi_errors.py
```

CI の通常のテスト (`-m "not grpc and not e2e"`) にも含まれる。

仕様から入力を生成し、アプリへ直接送って、次を確かめる。

- 5xx を返さない。
- ステータス、content-type、本文が仕様に適合する。
- 型が違う値など、不正な入力を拒否する。

対象は全 operation で、認証の種類ごとにテストを分けている。公開、staff、会員、共有トークンの 4 種類で、GET は読み取り、POST・PUT・DELETE は書き込みとして検査する。パスパラメータは `seeded` fixture が作る実在のオブジェクトに差し替え、成功時の応答形式まで検査する。

書き込みの副作用は 2 つの方法で抑える。

- 呼び出しのたびに savepoint を張り、必ずロールバックする。
- テスト中の外部ネットワーク接続を禁止する。オブジェクトストレージは `_FakeS3` に差し替える。外へ出る経路があれば、5xx として検出される。

次のチェックは除外している。

- `positive_data_acceptance`: 4 章のとおり、仕様に合う入力でも 4xx が正しい操作があるため。
- `ignored_auth`: 認証 cookie を別名のヘッダで渡しており、認証を外した再送ができないため。401 の宣言は `test_openapi_errors.py` が検査する。

入力は実行のたびにランダムに生成される。変更後は数回続けて実行して確かめる。

## 6. エンドポイントを追加・変更するとき

1. ルーターを実装する。入力は `server/api/schemas.py` の `*In` スキーマで受ける。必須のテキストは `NonBlank`、整数は `JsonInt`、実数は `JsonFloat`、真偽値は `StrictBool` を使う。
2. `dump_openapi` と `gen:api` で `openapi.json` と `schema.ts` を再生成し、一緒にコミットする。
3. 新しい ID 系のパスパラメータ (`{xxx_id}` など) を持つ operation を足すと、`test_seeded_covers_every_path_parameter` が失敗する。`seeded` fixture にオブジェクトを追加し、`_ID_PARAMS` に対応を足す。
4. 新しい認証方式を足したときは、`test_openapi_conformance.py` で対象の operation を選ぶ関数に、その方式を追加する。追加しないと、その方式の operation は検査されない。
