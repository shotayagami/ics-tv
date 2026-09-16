# 本線ライブ HLS のエッジ認証 Worker

`tv.yagamin.net/hls2/*` へのリクエストを Cloudflare 上で検証する Worker。
背景と選択肢の比較は [docs/site-only-broadcast.md](../../docs/site-only-broadcast.md) §5 リスク#3。

## なぜ Worker か

本線ライブの実配信経路は Cloudflare Stream ではなく、送出ノードの nginx (ABR ladder) を
k8s ingress-nginx 経由で出したもの。したがって CF Stream の `requireSignedURLs` は使えない。
残る選択肢のうち Worker を選んだのは、**HLS 配信の可用性を icstv-web の可用性から切り離せる**ため
(ingress-nginx の `auth-url` 方式だと、Django が落ちるとライブ視聴も落ちる)。

## 仕組み

1. `?token=` (Django の `core.hls_auth.sign_hls_token` が発行、HMAC-SHA256) を検証する
2. オリジンへは **token を外した URL** で取りに行き、Cloudflare のキャッシュキーを視聴者間で共有させる
3. `.m3u8` はレスポンスを書き換えて、子プレイリスト/セグメントの相対 URI に token を伝播する
   (HLS の相対 URI は親のクエリを引き継がないため)

トークンが束縛するのは **channel と有効期限だけ**。エッジからは「その番組が今ゲート対象か」を
判定できないので、**エンタイトルメント変更が効くまでの最大遅延は TTL** (`ICSTV_HLS_TOKEN_TTL_SEC`)
そのものになる。短くするほど使い回しの窓が狭まる。

## テスト

```bash
node test.mjs
```

署名検証 (channel 不一致 / 期限切れ / 改竄 / 鍵違い) と manifest 書き換え、および **WebCrypto の
HMAC が Django 側 (`server/core/hls_auth.py`) と一致すること**を検証する。CI には載せていない
(この Worker はフロント/サーバのワークスペース外) ので、`src/index.js` を触ったら手元で実行する。

## デプロイ

```bash
cd deploy/cloudflare-worker-hls
npm install -g wrangler          # 未導入なら
wrangler login                   # または CLOUDFLARE_API_TOKEN (Workers Scripts:Edit + Workers Routes:Edit)

# 署名鍵を投入する。Django の ICSTV_HLS_SIGNING_KEY と同一の値にすること。
#   生成: openssl rand -hex 32
wrangler secret put HLS_SIGNING_KEY

wrangler deploy
```

Django 側は SealedSecret へ `ICSTV_HLS_SIGNING_KEY` を追加する
(`scripts/seal-secrets.sh` は `--from-literal` の分しか書かないため、稼働中 Secret から
差分追加する方式を採ること。手順は [docs/fanclub.md](../../docs/fanclub.md) の SealedSecret 節と同じ)。

## 段階導入の順序 (24/7 送出を止めないため)

1. **サーバ側を先に本番へ**。完全公開の番組を含め全 URL に token が付くようになる
   (この時点では Worker が居ないので、token はただのクエリとして無視される)
2. Worker を **`HLS_AUTH_ENFORCE = "false"` のまま** deploy する。素通しのまま
   `x-icstv-hls-auth: ok|invalid` ヘッダだけが付くので、正当な視聴者が invalid にならないことを確認する
   ```bash
   curl -sI "https://tv.yagamin.net/hls2/ch1/master.m3u8?token=<発行されたtoken>" | grep -i x-icstv
   ```
3. 問題が無ければ `HLS_AUTH_ENFORCE = "true"` にして `wrangler deploy`。以降 token 無しは 403
4. 実際に落ちることを確認する
   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' https://tv.yagamin.net/hls2/ch1/master.m3u8   # → 403
   ```
5. 落ち着いたら `ICSTV_HLS_TOKEN_TTL_SEC` を段階的に短くする (既定 3600 秒)

## ロールバック

- 即時: `HLS_AUTH_ENFORCE = "false"` にして `wrangler deploy` (素通しへ戻る)
- 完全撤去: `wrangler delete` またはダッシュボードから route を外す。オリジンは無改変なので
  Worker が消えれば従来どおりの配信に戻る

## 監視: 実視聴者が 403 で弾かれていないか

**判定基準: `/hls2` で User-Agent がブラウザ系の 403 が出ていたら異常**。正常時の 403 は検証用
`curl` だけになる (アプリ/ブラウザは API 由来の token を必ず持つため)。

Cloudflare GraphQL Analytics で集計する。`.env` の `CF_WORKERS_API_TOKEN` に
**Zone → Analytics → Read** が必要 (2026-07-30 付与済み)。

```bash
source ~/.env
SINCE=$(date -u -d '6 hours ago' '+%Y-%m-%dT%H:%M:%SZ')
UNTIL=$(date -u '+%Y-%m-%dT%H:%M:%SZ')

cat > /tmp/q.json <<JSON
{"query":"query { viewer { zones(filter: {zoneTag: \"$CF_ZONE_ID\"}) { httpRequestsAdaptiveGroups(limit: 100, filter: {datetime_geq: \"$SINCE\", datetime_leq: \"$UNTIL\", clientRequestHTTPHost: \"tv.yagamin.net\", edgeResponseStatus: 403}, orderBy: [datetimeHour_ASC]) { count dimensions { datetimeHour clientRequestPath userAgentBrowser } } } } }"}
JSON

curl -s -X POST -H "Authorization: Bearer $CF_WORKERS_API_TOKEN" \
  -H "Content-Type: application/json" -d @/tmp/q.json \
  https://api.cloudflare.com/client/v4/graphql | python3 -m json.tool
```

ステータス別の全体比率を見るときは `edgeResponseStatus: 403` のフィルタを外し、
`dimensions { edgeResponseStatus clientRequestPath }` で集計する。

**注意: リクエスト数が少ない期間の「403 ゼロ」は根拠にならない**。ライブ HLS は視聴者1人あたり
約2秒ごとにセグメントを取得するため、`/hls2` 全体が数百件しかない期間は「ほぼ誰も見ていない」
ことを意味する。2026-07-30 の enforce 切替直後13時間は `/hls2` が 172 件 (200 が 95.3%、403 は
6 件すべて検証用 curl、ブラウザ由来の 403 は 0) で、**強制が動いていることの確認にはなるが、
実視聴者が弾かれないことの積極的な裏付けにはならなかった**。実視聴が乗る時間帯で再確認すること。

## 既知の限界

- **Safari のネイティブ HLS** (`<video src>` 直指定) はリクエスト URL に介入できないため、
  再生中に token を差し替えられない。TTL を超える連続視聴はそこで切れる (hls.js を使う
  ブラウザは `@icstv/api` の `createHlsTokenLoader` が差し替えるため影響を受けない)。
  TTL を短縮する際はこの点を考慮すること
- トークンは視聴者を識別しない。URL の共有・転売そのものは防げない (TTL 内に限られる)
- `/hls` (MediaMTX) 経路は対象外
