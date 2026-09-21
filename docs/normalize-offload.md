# 正規化 (mezzanine) の Windows オフロード — R2 ハンドオフ + 常時フォールバック

対象: `medialib.normalize` (素材の mezzanine 正規化)。
先例: slidecast のレンダオフロード (intake pod + Windows watcher を R2 prefix で疎結合) を
正規化ワークロードへ横展開する。slidecast の設計文書と ICS-TV 側の取り込みはこのツリーには
含まれない。

## 0. 背景と目標

- 正規化はクラスタ内で CPU 律速 (k8s のワーカーノード 6 コアに normalize/captions/delivery-qc/
  slidecast-intake 等が集中。normalize pod は concurrency=1 / cpu limit=2)。
  5–10 分素材で実測 17–40 分、30 分超は passthrough (映像コピー) へ妥協している
  (`MEZZ_MAX_SOURCE_SEC=1800`)。
- slidecast 用に確立した Windows レンダ機 (7950X3D 16C/32T, Docker/WSL2, R2 のみで疎結合)
  に正規化エンコードを振れば、この律速を根治できる。
- **ただし Windows 機は常時稼働ではない** (特に深夜帯 — 設計当時は weather の midnight-refresh が
  00:20 生成 → 00:57 放送で正味 37 分しか猶予がなかった。天気予報の自動取り込みはこのツリーには
  含まれない)。したがって:
  - オフロードは *opportunistic な加速* に徹する。**クラスタ単独で今日と同じ品質・
    レイテンシで完結し続ける**ことが大前提。
  - Windows 不在 (ハートビート途絶) は即ローカル処理。ジョブ途中の Windows 停止も
    段階的なデッドラインでローカルへフォールバックする。
  - 時刻直結の短尺素材 (weather 180s) はそもそもオフロードしない (尺閾値で自然に除外)。

### non-goals

- icstv-delivery (納品) 側の正規化 — 別リポ・別パイプライン。対象外。
- 複数 watcher の分散実行 — slidecast 同様、単一 watcher 前提。
- GPU/NVENC — まず libx264 のまま CPU コア数で勝つ。HW encode は将来の別判断。

## 1. 全体構成

```
Asset 作成 (post_save signal)
  └→ dispatch_normalize (celery, default queue — 軽い判定のみ)
       ├─ NORMALIZE_OFFLOAD_ENABLED != "true"      → normalize_asset へ (従来どおり)
       ├─ heartbeat が古い (Windows 不在)          → normalize_asset へ
       ├─ source 尺 < NORMALIZE_OFFLOAD_MIN_SOURCE_SEC → normalize_asset へ
       └─ else → R2 へ request.json PUT + Asset をオフロード中マークして即 return

Windows watcher (icstv リポ同梱の Python 常駐、docker compose)
  ├─ 毎ループ: normalize/watch/heartbeat.json を PUT (生存シグナル)
  ├─ normalize/in/<asset_id>/request.json を走査 → claim (status.json=encoding)
  ├─ R2 から原本 GET → ffmpeg (request 内の spec どおり 2-pass loudnorm + full encode)
  ├─ エンコード中も status.json の updatedAt を定期更新 (進捗ハートビート)
  └─ 成果物 mezz.mp4 → normalize/out/<asset_id>/ へ upload、result.json を最後に PUT

reconcile_normalize_offload (celery beat 60s, default queue)
  ├─ result.json あり → サーバ側 ffprobe で検証 → mezzanine/<kind>/<id>.mp4 へ
  │    server-side copy → Asset READY + メタ確定 → captions 連鎖 → in/out 掃除
  ├─ status=failed / claim されない / 進捗停滞 / 絶対上限超過 → ローカルへフォールバック
  │    (normalize_asset を force_local で再投入。以後この asset は従来経路)
  └─ 孤児 (フォールバック後に遅れて届いた成果物等) を掃除
```

DB (Postgres) と Celery broker (Redis) へは Windows から一切到達しない。連携面は R2 のみ
(slidecast/weather オフロードと同一の疎結合方針)。

## 2. R2 プロトコル

バケットは既存 `R2_BUCKET` (prod=icstv-mezzanine / dev=icstv-mezzanine-dev) を共用し、
prefix で分離する。

| キー | 書き手 | 内容 |
|---|---|---|
| `normalize/watch/heartbeat.json` | watcher | `{updatedAt, host, version}`。毎ポーリングループで PUT |
| `normalize/in/<asset_id>/request.json` | クラスタ | ジョブ依頼 (下記)。asset ごとに 1 オブジェクトを上書き |
| `normalize/out/<asset_id>/status.json` | watcher | `{assetId, requestId, phase: encoding\|failed, error?, updatedAt}` |
| `normalize/out/<asset_id>/mezz.mp4` | watcher | エンコード成果物 (multipart upload) |
| `normalize/out/<asset_id>/result.json` | watcher | `{assetId, requestId, probe: {duration_ms,width,height,fps,vcodec,acodec}, completedAt}`。**最後に PUT = 完了マーカー** |

`request.json`:

```json
{
  "assetId": 123,
  "requestId": "<uuid4 — 再投入ごとに更新>",
  "sourceKey": "ingest/live_recording/ch1/456.mp4",
  "sourceDurationSec": 5400.0,
  "resultPrefix": "normalize/out/123/",
  "spec": {
    "width": 1920, "height": 1080, "fps": 60,
    "vbitrate": "16M", "abitrate": "192k", "arate": "48000",
    "vcodec": "libx264", "preset": "veryfast",
    "acodec": "aac", "pix_fmt": "yuv420p", "profile": "high", "container": "mp4",
    "loudnorm_i": "-14", "loudnorm_lra": "11", "loudnorm_tp": "-1.5"
  },
  "submittedAt": "2026-07-13T12:00:00+09:00"
}
```

設計判断:

- **spec は request.json に全量焼き込む** (watcher 側に MEZZ_* 環境変数を持たせない)。
  クラスタの configmap が唯一の真実源になり、Windows 側との設定ドリフトが構造的に起きない。
- **Windows では長尺ガード (passthrough) を適用しない**。長尺こそフルエンコードの価値が
  最大 (passthrough は CPU 飢餓の妥協策なので、CPU が潤沢な側では本物の mezzanine を作る)。
  フォールバックでローカルへ戻った場合は従来どおり `MEZZ_MAX_SOURCE_SEC` の passthrough が
  効く — つまり today の挙動が常に下限として保証される。
- watcher は **数値/列挙のホワイトリストで spec を検証してから** ffmpeg 引数を組み立てる
  (R2 が汚染された場合のコマンドインジェクション多層防御。slidecast `sanitizeBuildArgs` と
  同じ発想だが、自由引数を渡さず型付きフィールドのみなのでより強い)。
- 完了マーカーは result.json の「最後に PUT」規約 (slidecast の meta.json / request.json と
  同じイディオム)。
- watcher は DELETE を一切呼ばない。`normalize/in|out` の掃除と `mezzanine/` への昇格 copy は
  クラスタ側 reconciler のみが行う (slidecast の promote/cleanupInbox と同じ責務分離)。

## 3. クラスタ側の状態機械

### 3.1 Asset 追加フィールド (migration)

- `offload_request_id: char(36) | null` — オフロード中の requestId。null = オフロードしていない
- `offload_dispatched_at: datetime | null` — request.json を PUT した時刻

`normalize_status` は既存のまま (`PROCESSING` を維持) — studio UI / API / 既存 reconcile は
一切変更不要。オフロード中かどうかは `offload_request_id is not null` で判別する。

### 3.2 dispatch_normalize (新タスク, default queue)

`medialib.signals` と `medialib.services.renormalize()` の投入先を
`normalize_asset.delay` → `dispatch_normalize.delay` に差し替える。

判定 (上から順に、最初に該当した経路へ):

1. `NORMALIZE_OFFLOAD_ENABLED != "true"` → `normalize_asset.delay(asset_id)` (従来経路)
2. `source_path` が `r2://` でない → 従来経路 (ローカルパス素材は watcher が取得できない)
3. heartbeat の R2 LastModified が `NORMALIZE_OFFLOAD_HEARTBEAT_FRESH_SEC` 超 → 従来経路
4. source 尺を ffprobe (presigned GET URL 経由・DL しない) — 取得不可 or
   `< NORMALIZE_OFFLOAD_MIN_SOURCE_SEC` → 従来経路
5. **尺 > `MEZZ_MAX_SOURCE_SEC` (>0 のとき) → 従来経路**。ローカルなら即 passthrough
   (映像コピー) で数分 READY にできる長尺を、オフロードのフルエンコード (数時間) で VOD 即時性を
   退行させないため (レビュー #5)。オフロード対象は実質 `[MIN, MAX]` 帯 = ローカルだと遅い
   フルエンコードになる素材で、加速の価値が最大の範囲。
6. オフロード: request.json PUT → Asset を
   `normalize_status=PROCESSING, normalize_started_at=now, offload_request_id=<new uuid>,
   offload_dispatched_at=now` で保存 → return

`dispatch` の判定 (網越し ffprobe) は行ロック外で行い、状態遷移 (PENDING→PROCESSING) だけを
`select_for_update(skip_locked)` の短いトランザクションに閉じる。R2 PUT/DB のいずれかで例外が出た
場合 `dispatch_normalize` タスクは**ローカル正規化にフォールバック**する (asset を PENDING のまま
取り残さない・レビュー #8/#14。PENDING はどの reconcile も監視しないため)。

専用 `offload` queue に置く理由: default queue (concurrency=4) には resolver など締切系ビートが乗る
(設計当時は天気予報の自動取り込みも同じ queue にあった。取り込みはこのツリーには含まれない)。
dispatch の ffprobe や reconcile の I/O 待ち copy をそこに積むと締切を侵食する
(レビュー #4/#10)。CPU はほぼ使わない (判定 + I/O 待ち) ので小さな専任 worker (`56-offload`,
concurrency=2) で捌く。`CELERY_TASK_ROUTES` で `dispatch_normalize`/`reconcile_normalize_offload` を
`offload` queue へ明示ルーティング (glob `normalize_*` には乗らないので明示が必要)。

### 3.3 reconcile_normalize_offload (新 beat, 60s, default queue)

対象: `Asset.objects.filter(normalize_status=PROCESSING, offload_request_id__isnull=False)`
(DB 起点なので R2 の全走査はしない)。asset ごとに:

| 観測 | 遷移 |
|---|---|
| `result.json` あり & requestId 一致 | **finalize** (§3.4) |
| `status.json` が `failed` & requestId 一致 | **fallback** |
| status 無し (or 旧 requestId) & `offload_dispatched_at + CLAIM_SEC` 超過 | **fallback** (claim されない = watcher 直後死/不在) |
| `status=encoding` & status.json の R2 LastModified が `PROGRESS_STALE_SEC` 超 | **fallback** (エンコード中の Windows 停止) |
| `offload_dispatched_at + MAX_WAIT_SEC` 超過 | **fallback** (絶対上限・ops 通知) |
| それ以外 | 待機 (次周) |

鮮度判定は status.json 本体の updatedAt でなく **R2 の LastModified** を使う (外部機の時計に
依存しない)。各 fallback/finalize は短い `select_for_update(skip_locked)` + requestId CAS で確定し、
判定と DB 反映の間に requestId が変わっていれば (再正規化) スキップする。

**fallback** = `offload_request_id / offload_dispatched_at` をクリア + **`normalize_started_at` を
now にリセット** → `normalize/in/<id>/request.json` を削除 (watcher が以後拾わないように) →
`normalize_asset.delay` で従来経路へ on_commit 再投入 → warning ログ。requestId 不一致の遅延成果物は
無視される。**`normalize_started_at` をこの時点でリセットするのが肝** (レビュー #2/#12): ローカル
normalize の実行開始 (queue 滞留で遅れうる) を待たずにリセットしないと、8h stale 回収
(`reconcile_stale_normalize`) が「まだ queue で待っているだけ」の asset を誤って FAILED にする。
MAX_WAIT 到達時は `notify()` で運用アラート (agent liveness と同じ経路・レビュー #19)。

なお `reconcile_stale_normalize` (8h 最終防衛線) 自体も、オフロード中のまま滞留した asset を FAILED
化する際に offload フィールドをクリアし request.json を削除する (レビュー #3。残すと
reconcile_offload が finalize を試み続ける)。

**孤児掃除**: `normalize/out/` 直下の prefix を列挙し、「オフロード中でない asset」の
成果物で更新が `ORPHAN_TTL_SEC` (既定 24h) より古いものを削除する (フォールバック後に遅れて届いた
mezz.mp4 は GB 級なので掃除必須)。**削除直前に再 HEAD して stale を再確認**し、list スナップショット後に
再ディスパッチで上書きされたオブジェクトを誤削除しない (TOCTOU・レビュー #1)。削除はクラスタ側のみ
(watcher に DELETE 責務を持たせない)。

### 3.4 finalize (成果物の検証と昇格)

**重い R2 I/O (ffprobe/copy) は行ロック外で行う** (`produce_mezzanine`)。数分かかりうる昇格 copy 中に
asset の行ロックを保持すると、運用者の再正規化が無期限にブロックされるため (レビュー #11)。
DB 反映だけを短い `select_for_update` + requestId CAS に閉じる。

1. (ロック外) `normalize/out/<id>/mezz.mp4` を ffprobe (presigned GET URL 経由) —
   watcher の自己申告 (result.json) は使わず**クラスタ自身が計測した値**を使う。
2. (ロック外) 検証: video stream 存在・**解像度が spec と一致**・**codec が h264**・
   **fps が spec ±1** (VFR/別 fps を弾く・レビュー #13)・duration が `sourceDurationSec` と
   ±max(2s, 2%) で整合。NG → fallback (ローカルで作り直し)。
3. (ロック外) `mezzanine/<kind>/<id>.<container>` へ server-side copy (boto3 managed copy =
   5GB 超も multipart copy)。**最終 prefix `mezzanine/` へ書くのはクラスタだけ**。copy 失敗は
   例外を伝播させず fallback (poison loop 回避)。mezzanine キーは決定的なので並行/再実行で
   上書きしても最後の書き手が勝つだけ。
4. (短い CAS ロック) requestId が変わっていない (再正規化されていない) ことを確認して Asset 更新:
   `r2_key / duration_ms / width / height / fps / vcodec / acodec / normalize_status=READY /
   passthrough=False / normalize_error=None` + offload フィールドクリア。
5. (on_commit) `_maybe_enqueue_captions(asset)` を明示的に呼ぶ (normalize_asset 成功路と同じ連鎖。
   ここを忘れると offload 経由の番組素材だけ字幕が付かない — 既知のギャップ
   `/internal/delivery-asset` と同じ轍を踏まない)。
6. (on_commit) `normalize/in/<id>/` と `normalize/out/<id>/` を削除 (掃除)。

### 3.5 既存防衛線との関係 (invariant)

```
CLAIM_SEC(300) < PROGRESS_STALE_SEC(900) < MAX_WAIT_SEC(14400=4h) < NORMALIZE_STALE_SEC(28800=8h)
```

- reconcile_stale_normalize (8h) は最終防衛線としてそのまま生きる (オフロード中も
  normalize_status=PROCESSING なので、万一 reconciler beat 自体が死んでも 8h で failed 回収)。
- フォールバック後のローカル encode は normalize_started_at がリセットされるため、
  8h カウンタと衝突しない。
- 二重エンコード防止契約 (acks_late/visibility_timeout) と干渉しない: dispatch は数秒で
  return するので 7h の再配信窓を占有しない。redelivery で dispatch が二重実行されても
  request.json の上書き + 新 requestId で冪等 (旧成果物は requestId 不一致で棄却)。

## 4. 環境変数 (すべて killswitch 流儀 = os.environ 直読み)

| env | 既定 | 意味 |
|---|---|---|
| `NORMALIZE_OFFLOAD_ENABLED` | コード既定 `false` / **本番の配備値は `true`** | opt-in。false なら dispatch は即従来経路 (完全无害)。**二段構造に注意**: コード (os.environ 直読み) の既定は false のままで、有効化は導入者の配備基盤側 (このリポジトリの範囲外) の ConfigMap 相当が v0.8.92 以降 `"true"` を配ることで行う = **本番は有効**。dev 環境は `"false"` 維持 (§7/§8) |
| `NORMALIZE_OFFLOAD_MIN_SOURCE_SEC` | `300` | これ未満の尺はローカル (weather 180s は自然除外。短尺は往復コストが勝つ) |
| `NORMALIZE_OFFLOAD_HEARTBEAT_FRESH_SEC` | `180` | heartbeat がこれより古ければ Windows 不在とみなす |
| `NORMALIZE_OFFLOAD_CLAIM_SEC` | `300` | dispatch 後この時間 claim されなければ fallback |
| `NORMALIZE_OFFLOAD_PROGRESS_STALE_SEC` | `900` | encoding 中 status 更新が止まってこの時間で fallback |
| `NORMALIZE_OFFLOAD_MAX_WAIT_SEC` | `14400` | dispatch からの絶対上限 (< NORMALIZE_STALE_SEC 必須) |
| `NORMALIZE_OFFLOAD_ORPHAN_TTL_SEC` | `86400` | 孤児成果物の掃除猶予 |

watcher 側 env (Windows `.env`): `R2_ENDPOINT_URL / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY /
R2_BUCKET / NORMALIZE_WATCH_INTERVAL_SEC(20)` のみ。MEZZ_* は持たない (§2)。

## 5. Windows watcher の実装と配布

- **エンコードコマンド構築の単一ソース化**: `server/medialib/mezz.py` (新設・Django import
  なしの純粋モジュール) に spec 構築/loudnorm 1-pass/2-pass フィルタ/ffmpeg 引数組み立て/
  ffprobe を抽出し、`medialib/normalize.py` と watcher の両方がこれを使う。
  TS へ再実装しない (icstv-delivery で既にロジック二重化が起きており、これ以上増やさない)。
- **watcher 本体**: `server/tools/normalize_watcher.py` — boto3 + 標準ライブラリのみ、
  Django 非依存の常駐スクリプト。slidecast watch.ts と同じ構造
  (ポーリング → claim → 実行 → 成果物 PUT、進捗スロットル付き status 更新、
  例外はログして次周へ)。
- **配布**: watcher を動かすコンテナイメージ (python:3.12-slim + ffmpeg + boto3。
  `server/medialib/mezz.py` と watcher のみ COPY — Django アプリ全体は積まない)、その
  compose 定義 (Windows 側、ソースから docker build = 自己完結方針)、および Windows 側の
  構築手順 (R2 資格情報の帯域外受け渡し手順込み) は、導入者の配備基盤側 (このリポジトリの
  範囲外) で用意する。
- **R2 資格情報**: slidecast 用に発行済みの icstv-mezzanine スコープトークンを流用可
  (同一バケット・同等の信頼境界。normalize/ prefix の read/write は既に可能な権限)。
  新規発行するなら同スコープで。
- **クラッシュ復帰**: watcher 再起動後、`status=encoding` のジョブは拾い直さない
  (slidecast と同じ)。復旧は reconciler の PROGRESS_STALE fallback に任せる —
  「Windows 側は使い捨て、正は常にクラスタ」の原則。
- **heartbeat は「新規を受けられるか」の信号 (レビュー #17)**: watcher は各ポーリング周回の頭で
  heartbeat.json を PUT する。長尺エンコード中 (`process_one` 実行中) はループが戻らないため
  heartbeat が意図的に stale になる → クラスタは新規 asset を**即ローカル**に回す。単一 watcher は
  1 本ずつ逐次処理で busy 中は新規を捌けないので、これは「busy なら新規はローカル」という正しい
  load-shed であり自己潰しではない (新規を dispatch して in/ に積み CLAIM_SEC 待たせるより速い)。
  実行中ジョブ自身の liveness は heartbeat ではなく status.json の進捗 bump (別スレッド) で
  クラスタに伝わるので、encode 中に heartbeat が stale でも in-flight は fallback されない。

## 5.1 残存リスク (既知・許容 or 将来対応)

敵対的レビューで確認された、コードでは塞がず**設計判断として許容/将来対応**とする項目:

- **R2 トークンがバケット全体スコープ (レビュー #15・重大)**: Windows 機のトークンは
  icstv-mezzanine バケット全体に read/write でき、理屈上は `mezzanine/` (オンエア原本) を直接
  上書きできる (検証機構をバイパス)。設計当時これは**先行する slidecast オフロードと同一の
  信頼境界**であり、本機能で新たに悪化させるものではないと評価した (同一バケット・同一スコープ。
  slidecast オフロードはこのツリーには含まれない)。R2 API
  トークンは prefix 単位のスコープができない (バケット単位) ため、厳密な封じ込めには
  **normalize/in|out 用のステージング用 R2 バケットを分離**し Windows 側トークンをそこに限定、
  昇格は Windows に渡さない別トークンで行う構成が必要。これは slidecast も巻き込む横断作業の
  ため本 PR の対象外とし、**将来のインフラ課題**として記録する。緩和として finalize は
  解像度/fps/codec/尺を ffprobe 検証してから昇格するので、規格外の混入は弾かれる。
- **内容の正しさは検証しない (レビュー #16)**: finalize の検証は構造 (解像度/fps/codec/尺) の
  みで、映像の中身 (正しい素材か・黒画面でないか) は検証できない。番組系で厳密にやるなら人手の
  確認ステップが要る。当面は構造検証 + トークン境界 (#15) の範囲で許容。
- **オフロード中の「再正規化」は Windows 実行中ジョブを中断できない (レビュー #18)**: 運用者が
  再正規化すると新 requestId で再ディスパッチされ、旧 Windows ジョブは走り切ってから requestId
  不一致で無視され、孤児掃除で回収される (無駄な 1 本ぶんの計算)。プリエンプションは実装せず
  許容。UI に「オフロード中」の表示を出す改善は将来対応。

## 6. 運用シナリオ検証

| シナリオ | 挙動 |
|---|---|
| Windows 電源 OFF | heartbeat stale → 全 asset 即ローカル。**今日と完全に同じ** |
| dispatch 直後に Windows 死 | claim されず CLAIM_SEC(5m) で fallback。遅延は +5m 程度 |
| エンコード中に Windows 死 | status 更新停止 → PROGRESS_STALE(15m) で fallback |
| weather midnight (00:20→00:57) | 180s < 300s でそもそもローカル。オフロード導入前と不変 |
| 中尺 (5–30min) のフルエンコード素材 | オフロードの主対象。ローカルだと遅い encode を Windows で加速 |
| 長尺 live_recording (>30m, `MEZZ_MAX_SOURCE_SEC` 超) | **オフロードしない**→ローカル即 passthrough (映像コピー)。VOD 即時性を退行させない (#5) |
| watcher が遅れて成果物を上げた (fallback 済み) | requestId 不一致で無視 → 24h 後に孤児掃除 (削除直前に再 HEAD) |
| 運用者が「再正規化」 | offload 追跡クリア → dispatch からやり直し (新 requestId で request.json 上書き)。旧 Windows ジョブは孤児化 |
| finalize の昇格 copy が長い | 行ロック外で copy → 運用者の再正規化をブロックしない (#11) |
| MAX_WAIT(4h) 到達 | fallback + `notify()` で運用アラート (#19) |
| dispatch の R2/DB 例外 | ローカル正規化へフォールバック (PENDING 放置しない・#8/#14) |
| reconciler (beat) 停止 | 8h の reconcile_stale_normalize が failed 回収 + offload 追跡クリア (#3) |

> 注記: 表の「weather midnight (00:20→00:57)」行は設計当時の検証記録。天気予報の自動取り込みは
> このツリーには含まれない。表は当時の記録としてそのまま残す。

## 7. ロールアウト (完了記録)

手順 1〜3 は**実施済み** (prod 有効化 = v0.8.92):

1. ✅ コード + ConfigMap キー追加 (コード既定 false のまま無害マージ)。
2. ✅ dev E2E (dispatch → claim → encode → finalize → captions 連鎖 → 掃除) 検証後、
   **dev 環境は false へ戻して維持** — dev バケット (icstv-mezzanine-dev) には watcher が
   いないため、有効化すると全 asset が CLAIM_SEC(5分) 待ってからローカルに落ちる。
   E2E 再検証時のみ一時的に有効化する。
3. ✅ prod: 配備側の ConfigMap で有効化 (`NORMALIZE_OFFLOAD_ENABLED: "true"`) + Windows 側 compose
   起動。**Windows watcher の起動有無は運用者管理** — 停止しても heartbeat 途絶で全量ローカルへ
   自動フォールバックする (§6)。

### ロールバック

`NORMALIZE_OFFLOAD_ENABLED=false` に戻すだけ。dispatch は即ローカル正規化に落ち、in-flight の
オフロードは reconciler が finalize/fallback で畳む (reconciler は enabled で gate しない)。
コード revert は不要。

## 8. 実装/運用状況 (2026-09-02 時点)

- **本番有効**: 導入者の配備基盤側 (このリポジトリの範囲外) の ConfigMap 相当で
  `NORMALIZE_OFFLOAD_ENABLED: "true"` を配る (**v0.8.92** から。Windows レンダ機の常時稼働入りを
  受けて有効化)。dev 環境は `"false"`。
  **§4 の既定表はコード側既定 (false) と配備値 (true) の二段書き分け**を参照。
- **専任 worker を分ける**: offload キュー専任の celery worker Deployment (`icstv-offload`) を
  配備基盤側に置く。dispatch/reconcile を default キューの締切系ビートから隔離する
  (レビュー #4/#10)。
- **Windows watcher の起動有無は運用者管理**: 不在なら heartbeat 途絶で全量ローカル (§6 のとおり
  「今日と完全に同じ」に戻るだけ)。
