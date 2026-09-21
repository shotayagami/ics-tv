# #3 YouTube枠管理 APIシーケンス

YouTube Data API v3 で、永続 `liveStream`（RTMPキー）の上に `liveBroadcast`（枠）を
**4時間枠** rolling 生成・遷移する（v0.8.52 で 2h→4h へ変更、経緯は後述）。
永続キー・OAuth・枠設定は **Web UI で管理**する。
追加データモデルは [datamodel.md](datamodel.md) の「YouTube連携テーブル」を参照。

## §0 事前準備 (Google Cloud) — 別環境で立ち上げる場合

以下の Google Cloud 側の準備が済んでいないと、この文書のシーケンスは 1 つも動かない。
断片は `server/.env.example` のコメントにもあるが、まとまった手順はここを正とする。

1. **GCP プロジェクト**を作成し、**YouTube Data API v3 を有効化**する
   (APIs & Services → Enable APIs)。
2. **配信用 OAuth クライアント** (Web application) を作成する:
   - scope: `https://www.googleapis.com/auth/youtube.force-ssl` (書き込み API は
     OAuth2 必須・API キー不可)
   - redirect URI: `https://<管理ホスト>/admin-ui/oauth/callback/`
     (route の正本は `server/core/urls.py` の `youtube_oauth_callback`。管理ホストの実値は
     `DJANGO_ADMIN_HOSTS` — 導入者が設定する。例 `studio.<内部ドメイン>`)
   - client id/secret を `ICSTV_OAUTH_CLIENT_ID` / `ICSTV_OAUTH_CLIENT_SECRET` へ
     (本番は配備基盤側の Secret、ローカルは `server/.env`)
3. **クリエイター招待サインイン用 OAuth クライアント** (#27。**配信用とは別クライアント**)
   を作成する:
   - scope: `openid` + `userinfo.email` のみ (PKCE 付き Flow。実装の正本は
     `server/fanclub/creator_oauth.py`)
   - redirect URI: `https://<クリエイターホスト>/auth/google/callback/`
     (`server/config/urls_creator.py` の `creator_oauth_callback`。ホストは
     導入者が設定する。例 `creator.<内部ドメイン>`)
   - client id/secret を `ICSTV_CREATOR_OAUTH_CLIENT_ID` / `ICSTV_CREATOR_OAUTH_CLIENT_SECRET` へ
4. **クォータ目安**: Data API 既定 10,000 units/日で 1ch 運用は余裕 (内訳と 4ch 時の検討は
   後述「クォータ／エラー」節)。増申請はチャンネル数を増やすまで不要。

## 前提：liveStream と liveBroadcast の関係

- **liveStream**＝受信口（RTMP ingest, 永続キー）。チャンネルに **1本**（`isReusable=true`）。
- **liveBroadcast**＝「枠」（4h）。title / 開始時刻 / 公開設定を持ち、stream に `bind` し、
  `transition` で `live` / `complete` を順送り。
- 連続する RTMP（送出ノードの `icstv-encoder` → liveStream。直 push、CF Live Input は経由しない）の上で
  broadcast を4hごとに回す。**送出は無停止**。
- 書き込みAPIは **OAuth2 必須**（APIキー不可）。scope `youtube.force-ssl`。→ `youtube_credential`。

## Web UI 管理（永続キー等）

**画面：チャンネル設定 → YouTube**

- **YouTube連携（OAuth）**：[接続] → consent → `refresh_token` 保存。状態＝connected / expired / 要再認可。
- **永続ストリーム**：[作成]（`liveStreams.insert`）→ ingest URL ＋ キー（マスク / 表示 / コピー）。
  ローテーション（キー再発行）の一発操作は**存在しない**。`create_persistent_stream_view` は
  `youtube_livestream_id` が既にあると 409 で拒否する（`server/core/admin_views.py`。`youtube/api.py`
  `create_persistent_stream` も ValueError）ため、**Django admin で `channel.youtube_livestream_id` を
  リセットしてから [作成] を再実行する手動2段階**になる。**送出ノードの `/etc/icstv/encoder-<slug>.env`（`CF_INGEST_URL`）は自動更新されない**ので、
  ローテ後は手動で新しい ingest URL/キーへ書き換えて `icstv-encoder@<slug>` を再起動すること（[casparcg.md](casparcg.md) §6.4）。
- **枠テンプレート**：`title_template` / `privacy` / `enable_monitor` / `rolling_hours` / `slot_minutes` / 既定サムネ。

**画面：枠ダッシュボード**

- `youtube_slot` 一覧（window, status バッジ, watch URL, 手動操作：強制transition / 再作成 / 今すぐcomplete / エラー再試行）。

## シーケンス1：永続 liveStream 作成（UI, チャンネルごと一度）

```python
def create_persistent_stream(channel):
    r = yt(channel).liveStreams().insert(
        part="snippet,cdn,contentDetails",
        body={"snippet": {"title": f"ICS-TV {channel.slug} ingest"},
              # variable: 取込解像度/FPS を encoder 出力に追従 (固定値だと実出力が下回り videoIngestionStarved 誤判定)
              "cdn": {"ingestionType": "rtmp", "resolution": "variable", "frameRate": "variable"},
              "contentDetails": {"isReusable": True}}).execute()
    channel.youtube_livestream_id = r["id"]
    channel.youtube_ingest_url    = r["cdn"]["ingestionInfo"]["ingestionAddress"]
    channel.youtube_stream_key    = encrypt(r["cdn"]["ingestionInfo"]["streamName"])  # 永続キー
    channel.save()
    # 送出ノードの icstv-encoder@<slug> がこの ingest URL + key へ直接 tee 送出する
    # (deploy/playout-node/systemd/icstv-encoder@.service。CF Live Input は経由しない。§本線 egress は YouTube 直 push 参照)
```

## シーケンス2：枠 rolling 生成（Celery beat）

```python
def generate_slots(channel):
    cfg = channel.youtube_config
    for w in windows(now(), now() + hours(cfg.rolling_hours), step_min=cfg.slot_minutes):
        if YoutubeSlot.objects.filter(channel=channel, window_start=w.start).exists():
            continue                                    # unique(channel,window_start)で冪等
        b = yt(channel).liveBroadcasts().insert(
            part="snippet,status,contentDetails",
            body={"snippet": {"title": render(cfg.title_template, w),
                              "scheduledStartTime": w.start.isoformat()},
                  "status": {"privacyStatus": cfg.privacy, "selfDeclaredMadeForKids": False},
                  "contentDetails": {"enableAutoStart": False, "enableAutoStop": False,
                                     "monitorStream": {"enableMonitorStream": cfg.enable_monitor},
                                     "enableDvr": True}}).execute()
        yt(channel).liveBroadcasts().bind(
            id=b["id"], part="id", streamId=channel.youtube_livestream_id).execute()
        YoutubeSlot.objects.update_or_create(
            channel=channel, window_start=w.start,
            defaults={"window_end": w.end, "broadcast_id": b["id"], "status": "ready",
                      "title": render(cfg.title_template, w)})
```

`enable_monitor=false` なら `created→live` 直行（testing をスキップ）。手動制御のため
`enableAutoStart/Stop=false`（遷移はこちらで明示）。

**放送時間帯（`broadcast_windows`, v0.8.70/73）**：設定チャンネルでは放送時間帯と重ならない
窓をスキップし（`slot_has_on_air`）、枠は放送時間帯に揃えて生成する（固定 4h グリッドで
分断しない）。放送時間帯外は READY→LIVE 遷移も保留し、LIVE 中に時間帯外へ入った枠は
COMPLETE へ遷移（`rotate_slots`）。

## シーケンス3：枠遷移（boundary worker, 枠境界）

```python
def rotate(channel, at):
    s = YoutubeSlot.objects.get(channel=channel, window_start=at)        # 開始する枠
    if not livestream_active(channel):                                  # 事前条件
        retry_with_backoff(); return                                    # ダメなら alert(下記)
    yt(channel).liveBroadcasts().transition(
        broadcastStatus="live", id=s.broadcast_id, part="status").execute()
    s.status = "live"; s.save()
    # 直前の枠を閉じる（新を先にlive→隙間防止）
    p = YoutubeSlot.objects.filter(channel=channel, status="live", window_end=at).first()
    if p:
        yt(channel).liveBroadcasts().transition(
            broadcastStatus="complete", id=p.broadcast_id, part="status").execute()
        p.status = "complete"; p.save()

def livestream_active(channel):  # liveStreams.list の status.streamStatus を確認
    r = yt(channel).liveStreams().list(part="status", id=channel.youtube_livestream_id).execute()
    return r["items"][0]["status"]["streamStatus"] == "active"
```

**事前条件**：`transition(live)` は stream が `active`（RTMP流入中）でないと失敗する。失敗時は
backoff 再試行 → なお不可なら **alert（YT枠が空白。ただし送出ノードの encoder 自体は tee のもう一方の
leg＝ローカル MediaMTX への送出を継続するため、自前プレイヤー側は無影響）**。

> ⚠️ **「alert」の実態**：`rotate_slots` の blocked/missed は
> `logger.warning` とスロットの ERROR 化（studio コンソールの ERROR バッジ）**のみ**で、
> `core.notify` の Notification 発報は未実装。加えて `ICSTV_NOTIFY_WEBHOOK_URL` が
> 未設定なら notifier 自体が no-op（[operations.md](operations.md) 監視・通知経路の盲点 2）。
> encoder tee の YouTube 枝が凍結する（TCP は ESTAB のまま bytes 進行ゼロ、
> `FIFO queue full` → 無音で死ぬ `onfail=ignore` 構成）と、枠が連続して miss しても
> Notification は 1 件も出ない。通知経路の追加と YouTube 枝ヘルスの監視は残課題
> （方式はユーザ判断: server 側 healthStatus ポーリング or 送出ノード側 tee 統計の監視系への取り込み）。
> また **missed 確定した枠の YouTube 側 broadcast は過去日時の public upcoming として
> 残り続ける**（自動後始末なし）— 手動掃除 or rotate への後始末実装が残課題。

## クォータ／エラー

- Data API 既定 **10,000 units/日**。主コスト：insert=50 / bind=50 / transition=50 / list=1。
- 1枠ライフサイクル ≒ insert+bind+live+complete = **200 units**。4h枠×6/日 = **1,200 / ch**
  （放送時間帯設定時はさらに減る。2h×12 時代は 2,400 / ch）。
- **次枠誘導**：予告 list+chat+update=101 ＋ 移動後 update=50 = **151 units/枠 = 906 / ch・日**（シーケンス4）。
- **1ch（Phase 1）**：1,200 ＋ 906 ＋ 整合polling ≒ 10k 内に余裕。**4ch は要検討** → クォータ増申請 or
  操作削減（complete省略 / polling間引き）。← 残課題で追跡。
- **整合**：定期 `liveBroadcasts.list` で実状態を `youtube_slot` に同期（drift吸収）。polは低頻度（5–10分）。
- **エラー**：401 → `refresh_token` で再取得 / `transition` 失敗（stream未active）→ backoff /
  重複 → `unique(channel, window_start)` で冪等。
- **transition 失敗の自己回復**（transition の 503 を放置すると、その枠がまるごと無配信になる）：
  5xx（YouTube 側一過性）は ERROR 確定にせず status を保持し翌分の rotate が再試行（`deferred`）。
  4xx（invalidTransition 等）は `liveBroadcasts.list` で実 lifeCycleStatus を確認し、既に目的状態
  （live 化済み／complete 済み・削除済み）なら追認して正常遷移扱い。窓を live 化されずに過ぎた
  READY は ERROR に落として studio コンソールで可視化（`missed`）。rolling / #23 専用枠とも同じ。

## 本線 egress は YouTube 直 push（CF Live Input は経由しない）

- 本線の実 egress は送出ノードの `icstv-encoder@<slug>`（サイドカー、`h264_vaapi`）が CasparCG の UDP 出力を
  `tee` で **YouTube ingest URL ＋ 永続キーへ直接 push**する（`docs/casparcg.md` §6.4、
  `deploy/playout-node/systemd/icstv-encoder@.service` が正本）。同じ tee のもう一方の leg はローカル
  MediaMTX（自前 HLS）。**Cloudflare Live Input はこの経路のどこにも登場しない**。
- liveStream は encoder が push している限り `active`。broadcast の rotate は独立。
- 永続キー **ローテ時はノードの `/etc/icstv/encoder-<slug>.env`（`CF_INGEST_URL`。変数名は historical で
  実体は YouTube ingest URL）を更新**する必要がある（UI 側の自動反映は無い。手動反映）。
- CF Live Input/Output オブジェクト自体は本線と別に存在し、studio の手動操作（`core/admin_views.py` の
  create_live_input / create_live_output 等）と fanclub simulcast（`fanclub/tasks.py`）用に使われる。
  本線の永続 liveStream のローテとは独立で、自動連動はしない。

## 公開ページ／番組表 連携

- 視聴ページの「現在配信中」は `status='live'` のスロットの watch URL を指す。
- 枠は **watch URL が枠ごとに変わる**（YouTube仕様）→ 過去枠はアーカイブ。
  単一 broadcast で24h連続も技術的に可能だが、枠分割を採用（アーカイブ／運用単位）。
  当初 2h×12 で運用したが、**急激な登録者減**が発生。ヒアリングで**枠ごとの開始通知の過多**が
  指摘されたため v0.8.52 で **4h 枠へ変更**（`YoutubeConfig.slot_minutes=240`）。
  視聴URL切替の不連続は **公開ページ側が吸収**し、**YouTube で直接視聴している人は下記「次枠誘導」で次枠へ送る**。

## シーケンス4：次枠誘導（boundary 直前）

枠は watch URL が枠ごとに変わるため、現枠を直接視聴している人は `complete` で取り残される
（公開ページは吸収するが YouTube 直視聴は別）。終了が近づいた `live` 枠から、次枠の watch URL へ
**ライブチャット投稿 ＋ 説明欄追記**で誘導する。

- **タイミング**：`nudge_next_slot`（beat 1分周期）が、`status='live'` かつ
  `window_end - nudge_lead_minutes <= now < window_end` かつ `next_nudged=false` の枠を拾う
  （既定 lead=10分。`nudge_lead_minutes=0` で無効化）。
- **次枠**：`window_start >= 当枠 window_end` の同channelで `broadcast_id` を持つ最初の枠
  （`complete`/`error` 除外）。無ければ skip。
- **予告（live）**：次枠 watch URL（`https://www.youtube.com/watch?v={broadcast_id}`）を
  `nudge_template`（既定「まもなくこの配信は終了します…▶ {url}」、`{url}/{start}/{end}`）で整形し、
  1. 現枠の **ライブチャット**へ投稿（`liveBroadcasts.list(snippet)→liveChatId`／`liveChatMessages.insert`）、
  2. 現枠の **説明欄**先頭に同文を prepend（`liveBroadcasts.update`）。
  - **冪等**：チャット投稿成功（またはチャット未開設で no-op）で `next_nudged=true` を立て、1分 beat の
    重複投稿を防ぐ。`HttpError` 時はフラグを立てず window 内で再試行。説明欄追記は best-effort（内容は冪等）。
- **移動後（complete）**：`status='complete'` かつ `next_nudged=true` かつ `ended_nudged=false` の枠（＝予告済みで
  終了した枠）の **説明欄**を `nudge_ended_template`（既定「この配信は終了しています…▶ {url}」）へ差し替え、
  `ended_nudged=true` を立てる。アーカイブに残る予告文（「まもなく終了」）を終了後の文言へ更新するのが狙い。
  **ライブチャットは `complete` で閉じる（replay）ため対象外**＝説明欄のみ。次枠が無い／ended テンプレ空なら
  差し替えず flag だけ立て再走を避ける。`HttpError` 時はフラグを立てず再試行。
- **クォータ**：予告 list=1 ＋ chat insert=50 ＋ desc update=50 ＝ **101 units/枠**、移動後 update=50。
  合計 ≒ 151 units/枠（枠ごと各1回）。4h枠×6/日 ≒ 906/ch（2h×12 時代は 1,812/ch）。
  ライブチャットの API ピン留めは Data API 非対応のため、通常メッセージとして投稿する。

## 本番運用で踏んだ SaaS 側の実態

DB（`channel` / `youtube_slot`）と YouTube 側実体（liveStreams / liveBroadcasts）を
突合すると、rolling 枠の DB↔SaaS 整合自体は健全に保てる（ready/complete の一致・bind 先一致・
scheduledStartTime の窓頭整列）。一方で、次の形は運用を続けると必ず出てくるので押さえておく。

**同一 YouTube チャンネルに liveStream が 4 本ある**：

| liveStream | 対応 | cdn 設定 |
|---|---|---|
| ICS-TV ch1 ingest | DB ch1 の `youtube_livestream_id`（rolling 本線） | **1080p/60fps 固定** |
| ICS-TV ch2 ingest | DB ch2（**停止済み・enabled=False**） | 1080p/60fps 固定 |
| ICSTV | **DB 非管理**（手動作成の variable stream） | variable |
| Default stream key | DB 非管理（2021 年作成の既定キー） | variable |

- **variable 化カットオーバー（既存 liveStream を variable 版へ作り直す作業。手順を書いた運用
  runbook は本リポジトリに含めない）は未実施**。コード（`create_persistent_stream`、v0.8.14〜）は
  variable で作るが、稼働中の
  ingest stream 2 本は旧 1080p/60fps 固定のまま → encoder `VBITRATE 6.5M` の対症
  （videoIngestionStarved 回避）が引き続き前提。
- **ch1 と ch2 の YoutubeCredential は同一 YouTube チャンネルに接続されている**
  （mine=true の一覧が完全同一）。「liveStream はチャンネルに 1 本」の前提と現実は異なる。
  マルチ ch 再開時は**別 YouTube チャンネル + 別 OAuth** を前提とすること。
- DB 非管理の手動 broadcast（VRChat 生配信の予約枠）が手動 ICSTV stream に bind されて
  実在 = #23 の代表ユースケースは現状 **YouTube Studio 手動運用**されている（下記 #23 現況）。
  手動リソースの温存/整理はユーザ判断。
- completed 総数の差分（SaaS 718 vs DB 446）は ICSTV 導入前の同チャンネル配信履歴等で
  乖離ではない。
- encoder tee の YouTube 枝が凍結すると、以降の全窓が live 化されず**実質停波**になる
  （シーケンス3 の注記参照。復旧は encoder restart = operator 作業）。

## 未確定・論点

- `enableAutoStart/Stop` を使うか手動 transition 固定か（既定：手動固定）。
- **4ch 時のクォータ**：増申請 vs 操作削減。
- サムネ／タイトルを番組表からどこまで動的生成するか。
- サブスク向け非公開配信は CF 側で実施（YouTube は基本 public）。
- **`_credentials` の expiry 未指定バグ**（`youtube/api.py`）：`Credentials(token=...)` に
  `expiry` を渡さないため `valid` が常に True → 能動 refresh + DB write-back 分岐が
  到達不能で、毎 API 呼び出しが 401 → transport 層 refresh で救済されている
  （DB の access_token/token_expiry は 2026-06 で凍結）。`expiry=cred.token_expiry` を
  渡す 1 行修正 + 保存分岐の回復が候補（実施時期はユーザ判断）。
- rotate の blocked/missed への Notification 発報と missed 枠の SaaS 側後始末
  （シーケンス3 の注記参照）。

---

# #23 配信プリセット + 番組専用枠（rolling 枠と並行）

上記 #3 の rolling 4h 枠（チャンネルのリニア配信）は**そのまま温存**する。本節はその上に、
**番組単位で専用の `liveBroadcast` を並行で立てる**仕組みと、Studio 配信設定を再利用する
**配信プリセット**を足す。代表ユースケース＝生番組（タイトルに企画名と出演者名を入れた単発配信）を、
リニア枠とは別にクリーンな単独動画／専用メタデータで残す。

> **この仕組みは、対象の Program・Series を作るまで一切動かない**。preset /
> description template / ProgramBroadcast / `youtube_dedicated=True` が 0 件のあいだは
> beat が 5 分周期で空回りし、2 本目 liveStream（`youtube_livestream_id_2`）も作られない
> （対象番組が出現したときだけ lazy provision する実装どおりで、異常ではない）。
> 仕組みを使わずに YouTube Studio 側で手動配信しても、DB 非管理の stream / broadcast が
> 増えるだけで衝突はしない（§本番運用で踏んだ SaaS 側の実態）。

## 決定事項（確定）

| 論点 | 決定 |
|------|------|
| 専用枠の宛先 | **同一 YouTube チャンネル**（同一 OAuth）に、rolling 枠と**並行**して立てる |
| プリセット範囲 | **全項目を保存**。API反映できない項目は **配信ごとの手動チェックリスト**として残す |
| 起動トリガ | **手動ボタン と Program/Series フラグ自動の両対応**（同一 create/transition primitive を共有） |
| 映像出力 | **同一内容を encoder tee で 2本目 RTMP へ**（再エンコード無＝ノード容量壁を回避） |
| 2本目 liveStream の作成 | **beat が自動プロビジョン**（key1 の `create_persistent_stream` を踏襲） |
| 見逃しURL | 専用枠を立てた番組は `archive_watch_url` を **rolling 枠でなく専用 broadcast** に向ける |
| 手動チェックリスト | **配信ごとにチェック状態を残す**（`ProgramBroadcast.checklist_state`） |

## API でできること / できないこと（重要な現実）

YouTube Data API v3 は Studio の配信設定の**約半分しか**触れない。プリセットは「API反映群」と
「手動チェックリスト群」に分けて持つ。

**API反映できる**（`liveBroadcasts.insert/update` ＋ broadcast_id を video id として `videos.update` ＋ `thumbnails.set` ＋ `playlistItems.insert`）：
- title / description / scheduledStart・End / privacyStatus / selfDeclaredMadeForKids
- `contentDetails`: latencyPreference(normal|low|ultraLow) / enableDvr / enableEmbed /
  enableAutoStart・Stop / recordFromStart / enableClosedCaptions・closedCaptionsType
- `videos.update` 経由: snippet.categoryId / snippet.tags / snippet.defaultLanguage・defaultAudioLanguage /
  status.license(youtube|creativeCommon) / status.publicStatsViewable / recordingDetails(date/location)
- サムネ画像（`thumbnails.set`）/ 再生リスト追加（`playlistItems.insert`）

**API では設定できない**（Studio UI 専用 → 手動チェックリスト）：
- 有料プロモーション開示 / AI 使用開示 / 年齢制限(18+, ライブは不可) / 字幕の認定
- チャプター自動生成 / 注目の場所 / コンセプトの自動説明
- チャット: 翻訳・要約・ランキング / 参加者モード / 低速モード / リダイレクト
- デュアルストリーム（縦型ショート自動生成）

> ⚠️ 実装注意: `videos.update` の snippet 更新は **title と categoryId を同時指定必須**
> （部分更新でも snippet 全体を送る）。insert 済み broadcast の video リソースは直後に存在するので
> insert → bind → `videos.update` の順で適用できる。

## データモデル（追加。詳細 DDL は [datamodel.md](datamodel.md)）

- **`youtube_broadcast_preset`**（再利用ライブラリ）: `name` / `channel`(任意, null=全ch共通) /
  上記「API反映群」フィールド一式 / `manual_checklist`(JSON=チェック項目の定義＋既定値)。
- **`channel`** に 2本目永続 liveStream: `youtube_livestream_id_2` / `youtube_ingest_url_2` /
  `youtube_stream_key_2`(暗号化)。番組は時間で重ならないので **専用枠は全番組でこの 1 本を共有**。
- **`program_broadcast`**（rolling の `youtube_slot` とは**別モデル**＝本番経路を隔離）:
  `program`(FK) / `preset`(FK) / `broadcast_id` / `status`(created/ready/live/complete/error) /
  `checklist_state`(JSON=配信ごとのチェック状態) / `manual`(手動作成) / `error`。
- **`program` / `series`** に `youtube_dedicated`(bool) ＋ `youtube_preset`(FK,null)。
  Program 優先・空なら Series から解決（rating 解決パターン踏襲）。

## API 追加（`youtube/api.py`）

- `create_dedicated_stream(channel)` — 2本目永続 liveStream（`resolution/frameRate="variable"`,
  `isReusable=True`）を作り `youtube_livestream_id_2` 等に保存。key1 の `create_persistent_stream` と同型。
- `insert_broadcast(...)` 拡張 — `latency_preference` / `enable_closed_captions` / `record_from_start` /
  `scheduled_end` を preset から受ける（rolling 経路は既定値で挙動不変）。
- `apply_broadcast_preset(broadcast_id, preset)` — insert 直後に `videos.update`(snippet: title/categoryId/tags/
  defaultLanguage、status: license/publicStatsViewable/madeForKids) ＋ `thumbnails.set` ＋ `playlistItems.insert`。

## ライフサイクル（beat）

rolling 経路（generate_slots / rotate_slots）とは独立した sibling タスクにする。

- **`generate_dedicated_broadcasts`**（5分周期）: `youtube_livestream_id_2` 未作成なら
  `create_dedicated_stream` で**自動プロビジョン**。直近 N 時間で `youtube_dedicated=True` かつ
  未作成の Program に、専用 stream へ bind した broadcast を insert → `apply_broadcast_preset` →
  `ProgramBroadcast(status=ready)` を upsert（`unique(program)` で冪等）。
- **`rotate_dedicated`**（1分周期）: `program.start_at` 到達 ＆ 専用 liveStream `active` → `transition(live)`、
  `program.end_at` 到達 → `transition(complete)`。rolling の rotate と同型（事前条件＝stream active）。
- **手動ボタン**（studio）: 「専用枠を今すぐ作成 / Go Live / 終了」が同じ create/transition primitive を
  その場で叩く。auto と手動は同一コード経路。

## Studio UI

- **プリセット CRUD**: `channel_settings.html` 流の server-rendered ModelForm
  （`YoutubeConfig` が admin 専用なのと同層を昇格）。手動チェックリストは JSON→チェックボックス群。
- **Program 編集**: ProgramForm に「YouTube 専用枠を立てる」トグル ＋ preset 選択を追加。
- **手動操作 + チェックリスト**: 番組詳細に専用枠の作成/Go Live/終了ボタン、配信ごとの
  チェックリスト（状態を `checklist_state` に保存）、当該 broadcast の Studio 編集ページへの deep-link。

## 送出ノード（encoder tee）

- encoder（送出ノード・`/opt/icstv` は git チェックアウト。`deploy/playout-node/README.md` §ノードの
  更新の `sync-node.sh` で同期。git 外なのは実値を持つ `/etc/icstv/*.env` のみ）に live2 tee 枝を追加し、
  `rtmp://a.rtmp.youtube.com/live2/<key2>` へ**同一の 720p60 出力をコピー送出**（再エンコード無）。
  既存の MediaMTX 向け tee と同じ要領（`+global_header` 必須）。
- **当面 operator がイベント前に tee を ON、後で OFF**（常時 tee は YouTube ingest 帯域が 24h 二重に
  なるため）。agent 制御の自動 tee ON/OFF は将来。ノード上での tee 追加操作（出力を 1 本増やす）の
  具体手順は運用 runbook 側にあり、本リポジトリには含めない。
- ⚠️ ch2 が**ノード容量壁**（LXC・単一 iGPU・他ワークロードと同居）で停止した前科がある。
  tee はコピーで軽量想定だが、**初回は送出ノードで CPU/帯域の実機検証必須**。

## 段階リリース（commit 粒度）

1. `youtube_broadcast_preset` モデル ＋ migration
2. preset CRUD form（studio）
3. `api.py` 拡張（create_dedicated_stream / apply_broadcast_preset / insert_broadcast 引数）
4. `program_broadcast` モデル ＋ beat（generate_dedicated_broadcasts / rotate_dedicated）
5. `program`/`series` フラグ ＋ ProgramForm 連結
6. 手動ボタン ＋ 配信ごとチェックリスト UI ＋ `archive_watch_url` を専用 broadcast 優先へ
7. encoder tee runbook ＋ 送出ノードでの実機検証

## クォータ

専用枠 1 本 ≒ insert50 + bind50 + videos.update50 + thumbnails.set50 + live50 + complete50 = **約 300 units**。
番組数は 1日数本想定なので rolling（1,200/ch）＋誘導（906/ch）に上乗せしても 10k/日 内（1ch 運用）。
preset 適用の `videos.update`/`thumbnails.set` は insert 時の 1 回のみ。

## 未確定・論点（#23）

- preset を将来 `youtube_config`（rolling 枠テンプレ）と統合するか（当面は別管理）。
- 手動チェックリストの定義をグローバル固定にするか preset ごとに編集可にするか（既定：preset ごと）。
- tee ON/OFF を agent 制御プレーンに載せる時期（v1 は operator 手動）。
