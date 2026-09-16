# 設計概要

本質は「**リニアチャンネルのプレイアウト自動化**」。YouTube Live / 自前 HLS は
フレームが途切れない連続入力を要求するため、**チャンネルごとに常時稼働の送出を1本持ち、
その上を番組表に従って切り替える**のが大原則。番組・CM枠・配信枠はすべてこの連続ストリーム
上の論理的区切りである（ウェザーニュースLiVE / SOLiVE と同構造：YouTube=無料公開、自社経路=有料）。

要件は [requirements.md](requirements.md)、データモデル詳細は [datamodel.md](datamodel.md) を参照。

## 1. 意思決定ログ（確定事項）

| # | 論点 | 決定 | 補足 |
|---|------|------|------|
| 1 | 送出エンジン | **CasparCG Server** | AMCP制御。HTMLテンプレでテロップ/Lバー/CMバンパー |
| 2 | ファンアウト | ~~CF経由でYT再送出~~ → **YouTube ingest 直 push + 自前 HLS (MediaMTX) の tee** | 当初決定 (CF Live Input へ1系統・上り帯域半減) は**その後不採用**。現行は送出ノードの `icstv-encoder@<slug>` が YouTube ingest とローカル MediaMTX へ tee 二重送出し、**CF Live Input は本線経路に無い**（[casparcg.md](casparcg.md) §6.4 / [youtube.md](youtube.md) §本線 egress）。CF Live Input/Output は studio 手動操作と fanclub simulcast (#27) 用に限定残存 |
| 3 | 稼働場所 | **オンプレ集約（自宅 Proxmox）** | 制御も送出/エンコードも自宅 Proxmox（送出ノード=Proxmox 上の LXC）。配信ファンアウト先(YouTube・自前 HLS の CF CDN/Worker)・素材(R2)はクラウド。当初の自宅/クラウド ハイブリッド案は未採用。送出のクラウドGPU移設は Phase2+ のスケール選択肢(#8 残課題) |
| 4 | Phase 1 スコープ | **生番組まで** | 録画＋生＋CM（録画枠15/20＋生バンドルCM） |
| 5 | CasparCG 基盤 | **Linux headless** | GPU(EGL)必須。systemd 常駐＋ウォッチドッグ |
| 6 | 素材配信 | **R2経由＋ローカルキャッシュ** | OMV→正規化→R2→送出ノードが先回りprefetch |
| 7 | 生番組入力 | **RTMP/SRT push** | 送出ノード MediaMTX でローカル終端→CasparCG（外部接続経路は #21 で具体化） |
| 8 | 制御リンク | **送出ノードagentでローカル実行** | as-runをpush、WAN断でも送出継続 |
| 9 | 編成方式 | **フリー編成** | 番組を任意時刻に配置。隙間はフィラー |
| 10 | 尺ズレ吸収 | **フィラーループ** | 番組と番組の隙間を既定フィラーで埋める |
| 11 | 初期チャンネル数 | **1ch** | 全機能を1chで検証してから最大4chへ複製 |
| 12 | サブスク課金 | **Phase 1 対象外** | 当初補足「CFは経路として先行構築」は不採用（#2 補足）。サブスク課金自体は v0.4.0 で実装済み（Stripe。視聴ゲートは Cloudflare Worker = [site-only-broadcast.md](site-only-broadcast.md) §5 リスク#3） |
| 13 | 実装言語 | **Python(Django) 既定／agent は疎結合で将来Go部分置換可** | フレーム精度はCasparCG側。agentは独立プロセス＋言語非依存契約 |
| 14 | デプロイ粒度 | **モジュラモノリス＋k8s複数ワークロード** | 単一Django/単一PGを web/beat/worker/normalize で分離稼働。公開EPG・正規化は将来サービス抽出 |
| 15 | 素材納品 | **オンライン納品ポータル（メディアブランチ相当）を新設** | 外部制作会社RBAC＋全用途(本編/配信/番宣/番販/保管)を一元管理。QC=技術+R128ラウドネス+リーダー検出、mezzanineで-14LUFS強制統一。詳細は [delivery.md](delivery.md) |
| 16 | 広告商流 | **営放サブシステム（sales+billing 2 app）をフル実装** | タイム(提供)+スポット契約、契約駆動CM割付(constraint provider 注入、scheduling へ依存逆流なし)、放確台帳、月次締め・放送確認書・請求書(フル内製)。詳細は [sales.md](sales.md) |
| 17 | 運行・監視 | **APC 運用機能（ウォッチドッグ完成/運行ダッシュボード/押え）を実装** | feed断→SLATE自動退避+自動復帰(ヒステリシス)、notifier抽象+webhook先行、HTMX運行画面、生番組延長は後続繰り下げ+隙間吸収。proto拡張(ReportInterrupt等)を伴う。詳細は [operations.md](operations.md) |
| 18 | CG レイヤ実装 | **レイヤ状態可視化 + 手動グラフィック + 番組/フィラー自動グラフィックを実装** | ①各レイヤ状態/内容を ops 一覧(Heartbeat `repeated LayerState`)。②任意レイヤ N-1〜N-89 へ 画像/動画(alpha,単発/ループ)＋文字 を複数組 手動で載せる(`OVERLAY_OP`)。③番組/フィラー毎に組み合わせ・タイミングを事前定義し自動表示(`GraphicCue`+resolver `cg_cues`+agent タイマー)。合成は CasparCG 側のみ。次番組予告を 1-40→1-35 へ移し速報1-40/自由1-45 を空ける。詳細は [cg-layers.md](cg-layers.md) |
| 19 | 納品の対象モデル | **納品を「どのチャンネル/どの番組(回)/どこが/いつ」で定義し、回を第一級 `Episode` モデルに昇格** | 週間編成番組の特定回向け素材は専用品。納品 target_kind=series_episode/oneoff/cm。`Episode`(series×回数×放送予定日)を納品が指し、本編承認で `Episode.asset` 確定→展開(`expand_series_slots`)が default_asset より優先採用、展開済みは正規化後 `apply_episode_asset` がバックフィル(end_at/ad_break 再計算・EXCLUDE衝突は通知)。CM 枠指定は sales 契約(spot_order/sponsorship)に紐付、無指定は一般プール。proto/agent 無改修。詳細は [delivery.md](delivery.md) |
| 20 | 納品の業者/認証/予算 | **業者・アカウントの専用管理画面 + Google 招待サインイン + 番組予算(AP)を実装** | ①`ProductionCompany`/`DeliveryAccount` を studio 専用画面で CRUD(Django admin は緊急用温存)。②email を業者に紐付け招待(`DeliveryInvitation`)→当人の Google サインイン(既存 google-auth-oauthlib・openid/email スコープ)で email 照合し User+DeliveryAccount を自動バインド(allauth 不使用、`LOGIN_URL=/delivery/login/`)。③新 app `procurement`: Series 予算枠(`ProgramBudget`)に納品単位の発生費用(`DeliveryCost`)を計上し支払(`Payment`)記録。billing(広告収入=AR)とは別系統の支払(AP)。詳細は [delivery.md](delivery.md)。**現況 (2026 Phase 3.9 追記)**: 納品ポータルの実装は別リポ `icstv-delivery` へ移管済み。本体の `LOGIN_URL` は `/admin/login/`、`DeliveryAccount`/`DeliveryInvitation` は migration 0006 で DROP 済み(本体 `delivery` app は models/urls を持たない ghost) |
| 21 | 生入力の外部接続経路（#7 を具体化） | **Cloudflare One（cloudflared private network + WARP）経由で現場 SRT を送出ノード MediaMTX へ終端** | 送出ノードは公開IP不要（本番=自宅 Proxmox LXC）。既存 FreePBX 運用と同一の private network 経路を流用（別建てのエッジVPS / 自前 WireGuard 不要）。二層認証＝WARP enroll＋Cloudflare Access（ネットワーク層）／SRT streamid＋passphrase（アプリ層・per-`LiveSource`）。ノード内は無改修（proto/agent/CasparCG/feed_monitor 不変、`LiveSource` に `srt_passphrase` 列追加のみ）。詳細は §3.5 |
| 22 | 天気予報の自動取り込み | **特定時刻の固定尺枠 × R2ドロップ取り込み（専用 HTTP API/認証を新設しない）** | **このツリーでは本決定の実装（天気予報・ランキングの自動取り込み）を除去済み。設計文書も配布しない**。以下は決定当時の記録。天気予報を 6回/日（毎時57分から3分・固定尺180s・`RecurrenceKind.DAILY` 6スロット）で編成。外部サブシステムが R2 へ置いたクリップを取り込み beat が拾い `Program.asset` を差替（リニア送出・自動承認）。**固定尺だから差替のみで `end_at` 不変・衝突判定不要**、正規化 READY 後にバインド、未着は `default_asset` が安全網。追加=`core.r2.list_objects`＋`WeatherIngest`(source_key unique 冪等)＋取り込み beat（medialib 無改修）。生成側=別プロジェクト（JMA publish 駆動の先取りレンダ・Remotion 固定尺・VOICEVOX＋BGM・k8s CronJob）。**同型の横展開**: 夏季企画「全国 最高気温 ベスト10」（1回/日 16:59:27・固定尺33s・季節ウィンドウ・別リポ icstv-ranking + ranking-admin コンソール）も同じ R2 ドロップ型で運用していた（同じくこのツリーには含まれない） |
| 23 | YouTube 配信プリセット + 番組専用枠（#3 を拡張） | **再利用可能な配信プリセットを新設し、番組単位で rolling 4h 枠と並行する専用 liveBroadcast を同一ch・2本目 liveStream（encoder tee）で立てる** | YouTube Studio の配信設定を `YoutubeBroadcastPreset`（API反映項目＋API不可分は手動チェックリスト）に登録・再利用。`Program`/`Series` の `youtube_dedicated` フラグ＋preset選択で、番組時刻に合わせ専用 broadcast を beat が自動 insert→preset適用(`videos.update`/`thumbnails.set`/playlist)→transition、または studio 手動ボタンで即時起動（**両トリガが同一 primitive を共有**）。2本目 liveStream は beat が自動プロビジョン、送出ノードは encoder を **同一内容 tee** で 2本目 RTMP へ分岐（再エンコード無＝ノード容量壁を回避、ON/OFF は当面 operator 手動）。専用枠を立てた番組の見逃し URL は rolling 枠でなく専用 broadcast を指す。本番稼働中の rolling slot 経路には触れず `ProgramBroadcast` を sibling 化して隔離。詳細は [youtube.md](youtube.md) |
| 24 | 視聴体験の深化（字幕自動生成 + タイムシフト） | **VOD 字幕を faster-whisper で自動生成（送出無風の別 pod）し、ライブは短窓タイムシフトを段階導入する** | PLAYER-04（字幕）+ ADMIN-04（Whisper）は純ソフトで先行（Phase A）: `normalize` 後に `kind=program` の素材だけ専用 `captions` キュー/pod（concurrency 1）で faster-whisper→WebVTT を R2 保存、VOD プレイヤーへ同一オリジンプロキシ配信の `<track>` として供給（`playback_asset` 経由で録画/生放送録画に均一）。PLAYER-03（一時停止/巻戻し/ライブ復帰）は ladder の `hls_list_size` を RAM 内 ~60秒窓へ拡大＋フロントの live-edge ロックを mode-aware 化（Phase B）。PLAYER-02（頭出し）は本格 DVR（tmpfs→ディスク退避が前提の実ノード作業）または VOD/YouTube DVR 代替（Phase C・別 issue）。線形ライブへの字幕・複数音声は送出経路改変ゆえ対象外。詳細は [viewing-experience.md](viewing-experience.md) |
| 25 | 生放送タイムキープ（進行管理）+ 生番組キューシート | **少人数運用向けの生放送タイムキープ画面を ops.* に新設し、生番組にも進行表（キューシート）を導入する** | TV のタイムキーパー業務をスマホ 1 台で回すための画面。放送残り時間・now/next・**次 CM/次セクションまで**・CM 入り/明け・開始/終了カウントダウン・**流し切るべき CM の残本数/残尺**・**生番組内 VT の送出管理**を集約。**生放送のキューシート = 進行表（ランダウン）＝義務台帳＋参照タイムライン**（固定スケジュールではない）。**枠（`end_at`）は固定・ハード境界、キューシート積算は予定尺で枠との差＝押し/巻き表示**（積算で end_at は決めない）。CM/VT は**手動発火が主＋cue 単位で任意に自動発火**（手動が常に上書き）。**カウントダウンはオペレータ画面のみ**（視聴者向けオンエア CG は作らない、CM 入り layer20 バンパーの手動経路発火は塞ぐ）。既存 `CueSheet`(asset-scoped) は温存し `Program` に紐づく新 `LiveRundown`/`LiveCue` を追加。既存 `op_cm_in`/`op_cm_return`/押え巻き/`insert_immediate_event`/公開プレイヤーの epoch+1s tick を流用。Phase0=読み取り画面(migration無・生録両対応)→Phase1=生キューシート＋義務台帳(field追加)→Phase2=自動発火実行＋WS push。詳細は [timekeeper-live.md](timekeeper-live.md) |
| 26 | LT 発表の自動動画化（slidecast） | **スライド PDF + 台本 md + VOICEVOX で発表動画を生成する別リポサブシステム（`~/icstv-slidecast`）+ R2 ドロップ自動取り込み（Asset 化まで自動・編成は手動）** | **このツリーでは本決定の実装（slidecast の自動動画化と取り込み）を除去済み。設計文書も配布しない**。以下は決定当時の記録。台本（`## Slide N` + `@` ディレクティブ）を正本に、文単位 TTS（話者付き実尺を記録）→ Remotion **スライド単位チャンクレンダ**（入力ハッシュキャッシュ・fade はチャンク越境描画で両立）→ ffmpeg concat。**完全自動は前提にしない**: 読みチェックレポート（audio_query のカナ読みを目視 QC）+ 編集画面（文単位試聴/修正/差分再合成・タイムライン簡易編集）+ 辞書 2 層（共通=DB マスタ→R2 スナップショット公開・weather 共用可 / プロジェクト=bundle 内 yaml）。掛け合い（`voices` キャスト・Phase 1 のデータモデルから対応）・実音声 `@rec`・SE/遷移 `@se`/`@transition`・字幕=台本由来の完全一致 VTT（whisper スキップ）。ICSTV 側= `SlidecastIngest`（source_key unique 冪等）+ 取り込み beat が medialib 通常投入（Phase 3）。将来=立ち絵/口パク（文単位話者タイムラインが基盤）。VOICEVOX は weather と同一 Deployment 共用（engine user_dict 不使用）。 |
| 27 | ファンクラブ/番組公式サイト（B2B2C 化） | **配信枠サブスク + YouTube 同時配信 + 番組公式サイト（ティア制 FC 収益化付き）をセット販売するプラットフォーム拡張。Phase A に加え有料ティア課金〜クリエイターページ拡充まで実装済み** | 新app `fanclub`: `Creator` を第 4 の主体として新設し、FC 課金は**クリエイター単位 × YouTube 型の累積ティア**。実装済み: 無料ティア + ブログ/VOD レベルゲート + creator.\* ポータル（Phase A）、**有料ティア課金**（当初の `JOINABLE_LEVELS` は撤去済みで、`tier_is_joinable`＝stripe_price_id 設定済み AND Connect onboarded のティアを個別解禁。Stripe Checkout destination charge）、Stripe Connect 収益分配（`FcSettlement` 分配元帳）、ライブ限定配信のティアゲート（exposure_policy 統合）、クリエイター個人 YouTube ch 宛シミュルキャスト（`CreatorYoutubeOutput`）、ティア変更（アップ/ダウングレード）、デジタル会員証、チップ/チャット/ギフト（`FcTip`/`FcChatMessage`/`FcGift`）。scheduling/medialib/playout → fanclub の依存は作らない（S6 踏襲）。**残は資金決済法等の弁護士レビュー（外部対応）のみ**。正本・詳細は [fanclub.md](fanclub.md) §9/§10 |

## 2. アーキテクチャ全体像

```
[自宅: 制御プレーン]                        [自宅: 送出ノード (LXC)]         [Cloudflare / YouTube]
OMV master                                playout agent
  └ 正規化(mezzanine) ─push→ R2 ──prefetch→ └ ローカルSSDキャッシュ
Django + PostgreSQL                        CasparCG (Linux headless/GPU)
  ├ 編成/番組表/as-run        ─push as-run→ agent → ローカルAMCP駆動
  └ 番組表公開Web                          icstv-encoder@<slug> (tee) ──┬─→ YouTube ingest 直 push(永続キー)
        ↕ WireGuard                         │                          │     ↑ Data APIで 4h枠 rolling
                                           MediaMTX ←─────────────────┘
                                            ├ 自前HLS → tv.yagamin.net (CF は CDN/Worker ゲートのみ)
                                            ↑ 生番組 SRT push (Cloudflare One/WARP, #21)
```

（CF Live Input は本線経路に無い。決定#2 の補足および [casparcg.md](casparcg.md) §6.4 参照）

## 3. コンポーネント別仕様

### 3.1 コントロールプレーン（自宅 / Django + PostgreSQL）
- 番組編成UI、素材・CM管理、番組表公開Web、as-run参照。
- DBは他サービス用の PostgreSQL サーバとは分離し**専用PG**を新設（既存方針に準拠）。
- スケジューラ（Celery beat等）が編成を解決 → as-run を生成し agent へ push。
- RKE2 に載せる。素材マスタは OMV/HDD NAS。

### 3.2 素材パイプライン
- OMV にマスタ投入 → **正規化（解像度/fps/コーデック/音声を統一した mezzanine）** → R2 へ push。
  正規化しないと継ぎ目で映像が乱れるため必須。
- 送出ノードの prefetch agent が、編成の N時間先の素材を R2 からローカルSSDへ先読み＋LRU退避。
- R2 はegress無料が効く（CF以外のGPUクラウドからの取得でもコスト無し）。

### 3.3 playout agent（送出ノード / 自宅 Proxmox LXC）
- 自宅から push された as-run を**数十時間分ローカル保持**（WAN断耐性）。
- ローカル AMCP で CasparCG を駆動。NTP時刻同期必須。
- as-run実績を store-and-forward で自宅へ返す（WAN復帰時に同期）。
- 実装は **Python 既定**（Django と統一）。ただし agent は**疎結合な独立プロセス**とし、契約
  （as-run受信／実行／as-run返送）を言語非依存に保つことで、将来ホットループのみ Go へ部分置換できる
  余地を残す。フレーム精度は CasparCG が担うため Python の tick 精度で十分。

### 3.4 CasparCG 送出（Linux headless / GPU）
- GPU 必須：HWエンコード（本番は Intel iGPU / `h264_vaapi`, EGL surfaceless）＋ミキサ合成＋
  **CEF(HTMLテンプレCG)のGLコンテキスト(EGL)**。
- systemd 常駐＋ウォッチドッグ（AMCP/出力ヘルス監視、異常時スレート自動切替→Zabbix通報）。
- 出力は CasparCG の UDP mpegts をサイドカー `icstv-encoder@<slug>` が受け、`-f tee` で
  **YouTube ingest（直 push）とローカル MediaMTX（自前 HLS）の 2 leg** へ二重送出
  （[casparcg.md](casparcg.md) §6.4 が正本。CF Live Input は経由しない）。

### 3.5 生番組入力
- **外部接続経路（決定#21）**：現場の配信PC/エンコーダを **Cloudflare One の WARP に enroll**（既存
  FreePBX のリモート端末と同一手順）→ 既存 cloudflared トンネルが送出ノードの LAN を private network
  として広告 → OBS が **SRT** で送出ノード MediaMTX の private IP:port へ push。送出ノードは公開IP不要
  （本番=自宅 Proxmox LXC）、別建てのエッジVPS / 自前 WireGuard も不要。
  - FreePBX の SIP/RTP(UDP) と同じ private network routing を流用。SRT も UDP のためそのまま通る。
  - **二層認証**：①ネットワーク層＝WARP enroll＋Cloudflare Access（ingest ポートのみ到達可）、
    ②アプリ層＝SRT `streamid`(`publish:<app>/<key>`)＋`passphrase`（per-`LiveSource`）。
    MediaMTX の「LAN内前提・認証なし」を実質クローズ。
- **ノード内終端（無改修）**：MediaMTX が `<app>/<key>` を終端 → `rtmp://127.0.0.1:1935/<app>/<key>` を
  CasparCG が FFmpeg producer で参照。**proto / agent / CasparCG / feed_monitor は不変**（`LiveSource` に
  `srt_passphrase`〔+任意 `latency_ms`〕列を追加するのみ）。LAN/WG 内サブ卓用に RTMP 直 push も併存可だが、
  現場 last-mile のロス耐性のため SRT を既定とする。
- **実務パラメータ**：SRT payload=1316（断片化回避）、`latency≈2000ms`（Cloudflare ヘアピン RTT 吸収）、
  送出ノード上り帯域は配信ビットレート＋余裕を確保（SIP/RTP と桁違いの数Mbps）。
- フィード断時はスレート（「しばらくお待ちください」）へ自動退避（feed_monitor が publisher 断を検知）。
- **生CM**：番組前に CMリール（複数CM連結）をバンドル登録 → 運用画面「CM IN」で
  ライブ入力→CMリール→ライブ入力に切替（手動CB相当、戻りは手動or残尺自動）。

### 3.6 配信ファンアウト（YouTube 直 push + 自前 HLS）
- ファンアウトは送出ノードの encoder tee が担う（決定#2 補足）: YouTube ingest への直 push と、
  MediaMTX 経由の自前 HLS（`tv.yagamin.net/hls2`、エッジゲートは Cloudflare Worker =
  [site-only-broadcast.md](site-only-broadcast.md) §5 リスク#3）。**CF Live Input/Output は
  本線に無く**、studio 手動操作と fanclub simulcast（#27）用に限定残存。
- **4h枠 rolling**：encoder が YouTube の**永続ストリームキー**へ連続送出。
  その上で Data API v3 が liveBroadcast を4hごとに rolling 生成し testing→live→complete を順送り。
  送出の連続性と枠分割は独立して成立（枠刻みはアーカイブ/運用単位。v0.8.52 で 2h→4h：
  急激な登録者減を受け、ヒアリングで指摘された枠ごとの開始通知の過多を半減）。
- **放送時間帯（`Channel.broadcast_windows`, v0.8.70）**：チャンネルに放送時間帯（`HH:MM-HH:MM` の
  配列）を設定すると、休止帯はリゾルバが休止スレート（`off_air`）を発行し、YouTube 枠も
  生成・LIVE 遷移しない（送出ストリーム自体は連続のまま）。未設定なら従来どおり 24h 放送。
  導入動機は急激な登録者減 ── 通知対策（4h化）に加え、現有コンテンツ量では 24h 編成を
  維持できないため、やむなく休止時間を設ける運用を選択。

## 4. データモデル概要

詳細スキーマは [datamodel.md](datamodel.md)。要点のみ：

- **編成（人が組む）** と **送出（機械が実行する as-run）** を分離。
- `channel` / `asset`(統一, kind=program|cm|filler|bumper|slate) / `cm_creative`(1:1) /
  `program`(recorded|live, フリー編成) / `ad_break`(+`ad_break_item`) /
  `cm_bundle`(+item) / `filler_playlist`(+item) / `youtube_slot` / `playout_event`(as-run)。
- 時間は **ミリ秒整数**（フレーム精度）。同一chの番組重なりは `EXCLUDE USING gist` でDB保証。
  CM枠尺は 15000/20000ms の整数倍を CHECK で強制。`playout_event` は `idempotency_key` で冪等実行。

## 5. スケジューラ状態機械

フリー編成：タイムライン上に番組を任意時刻配置。任意の壁時計時刻で
「予定番組が放送中」か「フィラーループ」かに解決される。

```
        next program start
FILLER ───────────────────▶ PROGRAM_RECORDED ──(AdBreak offset)──▶ CM_BREAK ──back──▶ PROGRAM_RECORDED
   ▲                              │                                                        │
   │ program end / gap            │ (live program start: cut to MediaMTX producer)         │ program end
   │                              ▼                                                        ▼
   └──────────────────────── PROGRAM_LIVE ◀──(CM IN / 戻り)── CM_BREAK ───────────────── (→ FILLER)
                                  │
            feed drop / error ────┴────▶ SLATE（緊急スレート, 全状態から遷移可 → Zabbix通報）
```

リゾルバ／agent実行ループの擬似コードは #2（別文書）で詰める。

## 6. 信頼性・運用（24/7）

- フィラー/スレートで送出が黒画面で死なないこと。
- ウォッチドッグ＋自動復旧、Zabbix連携（RTMPヘルス/枠状態/送出ノード）。
- as-runログ（広告レポート・障害解析の根拠）。
- 制御プレーン↔送出ノードは WireGuard（外部からの生入力到達は決定#21 の Cloudflare One）。R2 はトークン認証。

## 7. Phase 1 構築順序（1ch・生番組まで）

1. **連続送出の土台**：CasparCG(Linux/GPU) を systemd 常駐、フィラーループを無停止で送出
   （実装形は encoder tee → YouTube/MediaMTX。当初計画の CF Live Input 経由は決定#2 補足のとおり不採用）。
2. **YouTube 送出**：encoder が YouTube 永続キーへ直 push。番組表Web（最小）公開。
3. **編成→送出**：Django編成UI + PG + スケジューラ → agent push → AMCP駆動（録画番組の時刻切替）。
4. **素材パイプライン**：OMV→正規化→R2→prefetch のローカルキャッシュ。
5. **CM挿入**：録画番組の15/20グリッド自動充填 + as-runログ。
6. **生番組**：MediaMTX RTMP ingest + ライブ差し込み + バンドルCMの手動/自動トリガ。
7. **YouTube枠管理**：Data API で 4h rolling 生成・transition（放送時間帯外は生成しない）。
8. **緊急系**：スレート自動退避・ウォッチドッグ・Zabbix通報。

## 8. 残課題 / Phase 2 以降

- 視聴体験の深化（決定#24）: Phase A 字幕/Whisper・Phase B 短窓タイムシフトは実装、Phase C 本格DVR/頭出しは
  送出ノードのディスク退避（tmpfs→disk）が前提の別 issue。詳細は [viewing-experience.md](viewing-experience.md)。
- マルチチャンネル（2→4ch）への複製。GPUセッション・上り帯域・運用の再見積り。
- ~~サブスク（CF署名トークン視聴ゲート + 課金基盤 Stripe等 + 会員管理）~~ → 実装済み:
  課金 v0.4.0（Stripe）・会員管理（Member/2FA）。視聴ゲートは CF Stream 署名トークンでなく
  **Cloudflare Worker の HMAC 検証**で実装（[site-only-broadcast.md](site-only-broadcast.md) §5 リスク#3）。
- ~~CM出稿管理の作り込み（広告主・出稿期間・回数・レポート）~~ → 決定#16 / [sales.md](sales.md) で設計済み。
- 冗長化（送出ノード二重化、ホットスタンバイ）。
- 映像スペック確定（1080p60/30, ビットレート, 音声）。
- GPUクラウド事業者の選定（送出ノードのクラウド移設を行う場合。現状は自宅 Proxmox の LXC に集約）。
