# R2 レイアウト正本

> ステータス: **実測記録 + レイアウト正本**（2026-09-02 監査で新設）。件数・サイズは
> 2026-09-02 の bounded 実リスト（`list_objects_v2`、件数・概算サイズのみ取得）に基づく
> スナップショットで、以後は変動する。キー規約・書き手・読み手は実装（コード）が正本で、
> 本表はその横断インデックス。各サブシステムの詳細は §6 のポインタ先へ（重複記載しない）。

Cloudflare R2 のバケット/プレフィックスの全数棚卸し。「どの prefix に誰が書き、誰が読み、
何が消すのか（消えないなら理由）」を 1 箇所で見えるようにする。

> 注記: 本書に現れる weather / ranking / slidecast 系 prefix のうち、**ICS-TV 側の取り込み**
> （天気予報の自動取り込み・ランキング企画の自動取り込み・LT 発表の自動動画化）は**追加提供側**の
> 機能で、**このツリーには含まれない**。書き手として挙がる icstv-weather / icstv-ranking /
> icstv-slidecast は別リポジトリとして実在し、R2 側のオブジェクトも実在する。以下の表・一覧は
> 2026-09-02 実測時点の記録としてそのまま残す。

## 1. バケット一覧

| バケット | 用途 | 参照元 |
|---|---|---|
| `icstv-mezzanine` | **prod 本体**（`R2_BUCKET`）。§2 が全数表 | `server/core/r2.py`、導入者の配備基盤側の ConfigMap 相当 |
| `icstv-mezzanine-dev` | dev 環境用（offload は無効固定）。captions/ mezzanine/ slidecast/ の残骸あり（実害なし・気になったら手動全消しで良い） | 導入者の配備基盤側の dev 設定 |
| `icstv-public` | slidecast slideshow の恒久公開（custom domain の割当は導入者が設定）。トップレベルは `slidecast/` のみ = 設計どおり | icstv-slidecast 側の配備設定（`R2_PUBLIC_BUCKET`） |

**資格情報の注意**: アプリの R2 トークンはアカウント広域で、上記の他バケットにも届く
（web pod の資格情報で icstv-public / -dev のリストが可能なことを実測確認）。Windows watcher
トークンの広域スコープは [normalize-offload.md](normalize-offload.md) §残存リスクに既知として
文書化済み。

## 2. icstv-mezzanine 全数表（本書の本体）

実測（2026-09-02）: トップレベル 12 prefix・root 直下 0 件・計約 62GB。

| prefix / キー規約 | 書き手 | 読み手 | 消す仕組み | 実測 |
|---|---|---|---|---|
| `mezzanine/{program,cm,filler}/<id>.mp4` | ①server 正規化（**Asset.id**、`medialib/normalize.py`）②offload finalize の server-side copy（`medialib/offload.py`）③**icstv-delivery 正規化 worker（delivery_file.id**、`src/r2.ts`） | agent media_cache・VOD（`scheduling/vod.py`）・captions transcribe | **無（放送原本・仕様）**。Asset 削除フックなし。⚠️ ①と③が同一 prefix を別 id 空間で共有 = 衝突リスク（§4） | 590 obj / 44.5GB（program 576/30.7GB, filler 14/13.8GB）。Asset r2_key 保有 589 件とほぼ 1:1 = 孤児は僅少 |
| `delivery/inbox/<deliveryId>/<fileId>/<name>`（quarantine 原本） | icstv-delivery portal の presigned multipart | delivery QC/正規化 worker、server 正規化（`source_path=r2://`） | **無・仕様（意図的に永久保持と決定 2026-09-02）**。業者のファイル削除操作（`portal.ts`）のみが削除経路 = [delivery.md](delivery.md) 参照 | 24 obj / **14.6GB**（バケットの約 1/4） |
| `ingest/weather/` | icstv-weather | server beat scan（`scheduling/weather.py`） | **有**: weather-cleanup CronJob（03:30、直近 3 日残し。icstv-weather 側で運用）。実測 21 obj で機能中 | 21 obj |
| `ingest/ranking/` | icstv-ranking（produce CLI） | server beat scan（`scheduling/ranking.py`） | **無・要対応**（取り込み済みでも残置）。掃除 CLI を実装予定（icstv-ranking 側） | 47 obj / 495MB |
| `ingest/ranking-archive/` | icstv-ranking（過去日投入・取り込み対象外） | （なし） | **無・要対応**（シーズンアーカイブとして残すなら要合意）。掃除 CLI を実装予定（icstv-ranking 側） | 6 obj |
| `ingest/heatpoints/`・`ingest/heatpoints-regional/`・`ingest/regional/` | icstv-ranking | **server 側読み手なし**（SeriesSlot 未編成・admin console proxy のみ） | **無・要対応**（書き逃げ状態）。掃除 CLI を実装予定（icstv-ranking 側） | 7/217MB・14/159MB・2 obj |
| `ingest/live_recording/` | agent（server 発行 presigned PUT） | server beat scan（`scheduling/live_recording.py`） | **無** | 0 obj |
| `slidecast/in/`（支給 bundle） | slidecast intake pod | Windows レンダ watcher | **無・要対応**: `cleanupInbox`（`icstv-slidecast/src/offload.ts`）は実装済みだが**本番未配線**（呼び出しがテストのみ） | 33 obj / 17MB |
| `slidecast/review/`（レンダ結果） | Windows レンダ watcher | intake pod（レビュー UI） | **無・要対応**（promote 時も copy のみ） | 32 obj / 735MB |
| `slidecast/out/`（承認済み最終物 + 取り込みマーカー） | intake pod（promoteToOut） | server beat scan（`scheduling/slidecast.py`） | **無・仕様**（意図的恒久保持） | 25 obj / 840MB |
| `normalize/{in,out}/<assetId>/`・`normalize/watch/heartbeat.json` | server offload / Windows watcher | 相互 | **有（二重）**: finalize/fallback 後の `_delete_prefixes` + stale orphan sweeper（削除直前再 HEAD の TOCTOU 対策付き。`medialib/offload.py`）。実働（残 1 obj = heartbeat のみ） | 1 obj |
| `captions/program/` | captions transcribe・slidecast 先付け | VOD 同一オリジン VTT 配信 | **無・許容**（再正規化・取り込み競合で孤児が出るが許容 = `scheduling/slidecast.py` 冒頭に明記。現況 1.6MB で実害なし） | 484 obj / 1.6MB |
| `chime/lib/` | studio 音源アップロード | agent | **有**: 音源削除時に R2 も削除（`core/chimes.py`） | 3 obj |
| `live/<slug>.jpg`（poster） | poster grabber | 公開面 | **無・仕様**（上書き運用） | 1 obj |
| `config/{weather,ranking}/`・`media/{weather,ranking}/` | admin console 管理 | 各サブシステム | **無・仕様** | 9+5 obj |
| `data/observations/` | icstv-ranking（1 日 1 ファイル蓄積） | icstv-ranking | **無・仕様**（蓄積が仕様） | 609 obj / 61MB |
| `thumbnails/` | `core/thumbnails.py`（uuid キー・差し替え時に旧 obj が残る置き逃げ設計） | 公開面 | **無・許容** | 本番実体 0 |
| `billing/invoice/` | `billing/services.py` | — | **無・仕様** | 本番実体 0（Stripe 課金準備中と整合） |
| `manual/` | **運用者手動**（CF dashboard 等からの手動投入。コード上の書き手なし = 正規の手動投入原本置き場として規約化 2026-09-02） | server 正規化（Asset が `source_path=r2://manual/` で参照。実測 2 件） | **無・仕様**（手動投入原本。残り 2 obj は未参照の可能性 = 中身確認はユーザ作業） | 4 obj |

> 注記: 表の「読み手」に挙がる `scheduling/weather.py`・`scheduling/ranking.py`・
> `scheduling/slidecast.py` の 3 モジュールは**このツリーには含まれない**（取り込みが**追加提供側**
> のため）。`ingest/weather/`・`ingest/ranking/`・`ingest/ranking-archive/`・`ingest/heatpoints*`・
> `ingest/regional/`・`slidecast/` 3 prefix を走査する beat も無く、このツリーで R2 prefix を走査
> するのは `scheduling/live_recording.py` と normalize オフロード系だけになる。`captions/program/`
> の「slidecast 先付け」も同様（captions transcribe 側は残る）。表は 2026-09-02 実測時点の記録
> としてそのまま残す。

## 3. ライフサイクル総括

- **掃除「有」**: weather-cleanup CronJob（3 日）／offload finalize + orphan sweeper／chime 削除連動
- **掃除「無・要対応」**: slidecast in/review（cleanupInbox 未配線・review は機構自体なし）・
  ranking 系 ingest 5 prefix（計約 76 obj/950MB。掃除 CLI を icstv-ranking 側で実装予定）
- **掃除「無・仕様/許容」**: mezzanine（放送原本）・delivery/inbox（永久保持と決定 2026-09-02）・
  slidecast/out・billing・data/observations・config/media 系・live poster（上書き）・
  captions 孤児・thumbnails 置き逃げ・manual/（手動投入原本）

## 4. 既知の未決事項

- **mezzanine キー名前空間の衝突（最重大）**: icstv-delivery worker は
  `mezzanine/<kind>/<delivery_file.id>.<container>`、本体は `mezzanine/<kind>/<Asset.id>` に書く。
  本番は Asset id 約 598 に対し delivery file id 約 24 で、新規納品が既存の若い id の Asset 原本を
  上書きし得る。seam（`api/routers/internal.py`）は 2026-09-02 にメタ不一致の同一 r2_key を 409 で
  reject するガードを追加済み（旧 Asset への**無警告差し替え**は解消）だが、R2 obj 自体の上書きは
  ガード外。`icstv-delivery/README.md` §R2 に警告記載済み・恒久策（prefix 分離 + 既存 obj 移行）は
  未決 → 起票先 [delivery-service-split.md](delivery-service-split.md)
- **ranking 系 ingest の保持方針**（シーズンアーカイブとして残すか、weather 同様の日数掃除か。
  掃除 CLI は icstv-ranking 側で実装予定）

> 注記: §3 と本節のうち weather / ranking / slidecast 系にあたるもの（weather-cleanup CronJob・
> slidecast の `cleanupInbox`・ranking 系 ingest 5 prefix の掃除 CLI）は、いずれも生成側サブ
> システム（icstv-weather / icstv-slidecast / icstv-ranking）側の実装・予定で、このツリーには
> 含まれない。offload finalize + orphan sweeper と chime 削除連動はこのツリーに残る。当時の
> 棚卸し記録としてそのまま残す。

## 5. CF dashboard でしか確認できない事項（本書では未検証）

以下はコード + API 実測では確認できず、**未確認**のまま:

- R2 lifecycle rule の有無（コード上は一切未設定の前提で本書を書いている）
- バケットの全数（本表はコードから既知の 3 バケットのみ。dashboard に他があっても検知できない）
- custom domain 設定の実体（公開用バケットへの割当は dashboard 側）
- API トークンの実スコープ（広域であることは実測したが、権限の全容は dashboard のみ）

## 6. サブシステム別ポインタ（詳細の正本）

- icstv-weather: 配備設定の README（cleanup CronJob）
- icstv-ranking: `README.md`（キー規約）
- icstv-slidecast: `src/offload.ts` 冒頭コメント（in/review/out 3-prefix 疎結合）
- icstv-delivery: `README.md` §R2（quarantine・CORS・mezzanine 衝突警告）
- 本体 offload: [normalize-offload.md](normalize-offload.md)（normalize/ prefix と sweeper の正本）
