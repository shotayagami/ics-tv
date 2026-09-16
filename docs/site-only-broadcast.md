# サイト限定 / 会員限定放送（番組単位の配信ポリシー切替）

> ステータス: **実装済み**(2026-07 実装、本項目のみ「設計確定・未実装」のまま更新漏れだった記述を是正)。初版 2026-06-19（方式B / `site_only` bool）。**2026-06-20 改訂**: 収益化承認で YouTube メンバーシップ限定配信が使えるようになったため、メンバーシップ軸を追加し `site_only`(bool) を **4プリセットの `exposure_policy`(enum)** へ拡張。§4 のモデル/resolver/agent側ミラー制御（`YT_MIRROR_FILLER`/`YT_MIRROR_ROUTE`、`agent/icstv_agent/mirror.py`）・§4.7 のサイト会員限定 HLS ゲート（`scheduling/exposure_gate.py`・`core/hls_auth.py` の署名トークン）まで実装済み。**2026-07-28 追記**: ファンクラブ ティア軸（`docs/fanclub.md` §6.4）を `scheduling/exposure_gate.py` に統合し、「会員か否か」の2値ゲートに加えクリエイター別ティアレベルでのライブゲートにも対応した。**2026-07-30 追記**: 残課題だった本番の deploy 側配線（エッジ強制）は **Cloudflare Worker 方式で完了・enforce 稼働中**（§5 リスク#3）。残るは GPU 容量の実測と、トークンが channel+期限しか束縛しないことに由来する TTL 分のエンタイトルメント反映遅延のみ。
> 関連バックログ: `design/ICS-TV_backlog.md` の `BROADCAST-SITEONLY-01`、`RIGHTS-02`(媒体別配信)、`BILL-01`(exclusive=会員限定)。
> 関連ドキュメント: [casparcg.md](casparcg.md)（レイヤ規約 / route producer §6.6）, [youtube.md](youtube.md), [scheduler.md](scheduler.md), [operations.md](operations.md)、会員/サブスク（v0.4.0）。

## 1. 目的・背景

番組単位で **公開 YouTube 本線には本編を出さず、フィラー＋案内を流し、本編は別経路（自前 HLS サイト／YouTube メンバー限定配信）にだけ出す**制御を可能にする。収録・生のどちらでも発生しうる。

現状の送出ノードは **1 本の CasparCG 出力を ffmpeg `-f tee` で複製**し、YouTube（RTMP ingest へ**直接 push**。CF Live Input は経由しない — [youtube.md](youtube.md) §本線 egress）と自前 HLS（MediaMTX）に**同一映像**を流している（`deploy/playout-node/systemd/icstv-encoder@.service`）。そのため両者で違う映像を出すことが構造的にできない。

```
現状:
  CasparCG(1ch, UDP) ── icstv-encoder@ch1 (h264_vaapi, -f tee)
        ├─→ CF_INGEST_URL (=YouTube ingest へ直 push。変数名は historical) ─→ YouTube
        └─→ rtmp://127.0.0.1:1935/hls/ch1 ── MediaMTX ─→ 自前HLS(サイト)
```

### 1.1 2つの「会員」は別物（重要）
- **サイト会員** = v0.4.0 の Member / サブスク（Stripe・独自 ID、entitlement "exclusive"）。
- **YouTube メンバーシップ** = YouTube 側課金・ID。

同一人物がどちらか / 両方 / どちらでもない。**本編視聴権はプラットフォーム別に解錠**する（各メンバーシップは各々の面のみ。サイト会員→サイト本編、YouTube 会員→YouTube 限定配信）。サイト上で YouTube 会員を解錠する横断連携は **しない**（YouTube membership API + OAuth 連携が重いため初手は対象外）。媒体別配信制御（backlog RIGHTS-02（`design/ICS-TV_backlog.md`） の `allow_youtube`）の送出実装にもなる。

### 1.2 配信ポリシー（exposure_policy）= 4プリセット
番組（Program）に単一の enum を持たせ、3 面の挙動を**導出**する。チェックボックス群でなく**プリセット1択**にして無意味な組合せ（本編公開＋メンバー本編 等）を排除する。

| プリセット (enum) | YouTube本線(公開) | YouTubeメンバー限定 | サイトHLS | encode増分 |
|---|---|---|---|---|
| `public`（全公開・既定） | 本編 | — | 公開 | 0 |
| `site_public`（サイト限定・公開） | フィラー＋案内 | — | 公開（誰でも） | +1 |
| `site_members`（サイト会員限定） | フィラー＋案内 | — | 会員限定 | +1 |
| `members_yt_site`（会員限定・YT+サイト） | フィラー＋案内 | 本編 | 会員限定 | +2 |

3 つの直交軸をこの enum から導出する:
- **軸①（YouTube 本線に本編/フィラー）**: `public` 以外で常にフィラー＋案内。= 旧 `site_only` の実体。
- **軸②（サイトのアクセス: 公開/会員限定）**: `site_members`/`members_yt_site` で会員限定。**encode コスト 0**（プレイヤー＋HLS エッジ認証のソフトゲート、§4.7）。
- **軸③（YouTube メンバー本編）**: `members_yt_site` のみ。専用ミラー ch＋encoder（VAAPI +1）。

## 2. 採用方式 = 方式B（CasparCG ミラーチャンネル）×2

YouTube 送出用に CasparCG ミラーを **2 本**増設する（公開用 M／メンバー用 P）。本線(N)＝サイト用 HLS は常に本編。

- **公開ミラー M** → YouTube 公開: 通常 `route://N`（本編。channel 合成出力＝L バー/スレート含む）。フィラー系プリセット中は案内フィラーの LOOP 再生へ差替。
- **メンバーミラー P** → YouTube メンバー限定（永続 broadcast の stream key）: 既定は案内フィラー（待機画）。`members_yt_site` 番組中だけ `route://N`（本編）へ。
- CasparCG が内部でシームレスに切替えるため **YouTube ingest は再接続しない**（M/P とも方式B）。

```
方式B×2:
  CasparCG ch N (本線/サイト)   ── icstv-encoder@chN          ─→ MediaMTX(HLS) ─→ サイト
  CasparCG ch M (YT公開ミラー)  ── icstv-yt-mirror-encoder@chN ─→ YouTube公開
        通常:            PLAY M-10 route://N            (本編を複製)
        フィラー系:      PLAY M-10 "site_only/.." LOOP   (案内フィラー)
  CasparCG ch P (YTメンバーミラー)── icstv-yt-members-encoder@chN ─→ YouTubeメンバー限定
        members_yt_site中: PLAY P-10 route://N           (本編を複製)
        それ以外(既定):    PLAY P-10 "members/.." LOOP    (待機案内)
```

### 検討した代替（不採用）
- **方式A（stream-copy リレー＋事前エンコード済みフィラー）**: GPU 増分ゼロにできるが、PROGRAM⇄FILLER 切替で YouTube ingest が数秒再接続する。
- **メンバー leg を stream-copy リレー（方式A流, VAAPI 増分 0）**: メンバー配信は間欠なので窓頭の reconnect は許容できるが、**シームレス一貫性を優先しユーザー選択で不採用**。代償として VAAPI 計 +2＝GPU 容量は専用 GPU が前提条件（§5-1）。

### YouTube members-only 配信の用意
**永続の members-only ライブを 1 本 YouTube Studio で手動作成**し、その stream key を P encoder の固定 ingest に常用する。番組窓ごとの可視性制御は **ミラー P の route/filler 切替**で行う（番組ごとに members-only 配信を API で都度作成する方式は、members-only 可視性の Data API 対応が不確実なため初手は採らない＝§5-2 要検証）。

### 確定した設計判断（ユーザー回答 2026-06-19 / 2026-06-20）
- フラグは**新設**（既存 `rights.allow_youtube` は流用しない）。**シリーズ既定も含める**。
- **解錠はプラットフォーム別**（横断連携なし）。
- **公開サイト限定 `site_public` プリセットも残す**（非会員も視聴可。4 プリセット構成）。
- **メンバー leg＝専用 ch＋encoder（VAAPI +1）／members-only 配信は永続 1 本を手動作成**。
- **実装は Case1(YT メンバー)＋Case2(サイト会員) をまとめて設計・実装**。
- GPU 容量は別 ops で実機計測だが、本選択で **専用 GPU は実質前提**（§5-1）。
- 案内素材は**専用 FK 新設**: `Channel.site_only_filler`（公開ミラー用）＋ `Channel.members_filler`（メンバー待機用）。

## 3. 設計の骨子

- **本線は不変**: `emit_recorded`/`emit_live`（`server/scheduling/resolver.py`）の本線イベント(PLAY_ASSET 等)は `exposure_policy` でも変えない。ミラー制御イベントを**追加**するだけ。
- **新 PlayoutAction 2 種を 2 ミラーで共用**（`PLAY_SLATE`/`CLEAR_SLATE` と同型の割り込み流儀）:
  - `YT_MIRROR_FILLER` … ミラーを案内フィラーへ差し替え
  - `YT_MIRROR_ROUTE` … ミラーを `route://N` へ復帰
  - 対象ミラー（公開 M / メンバー P）は **seg で識別**（`yt_public` / `yt_members`）。`_ikey(channel_id, scheduled_at, action, seg)` が seg を含むため、2 ミラー分・同時刻の PLAY_ASSET 等と衝突しない。
- **channel_id は論理 channel（本線と同じ）**。ミラー channel 番号(M/P)は送出ノード固有なのでサーバは知らず、agent 側 env で本線↔各ミラーを紐づける（`emit_live` の rtmp を agent がローカル補完するのと同じ思想）。
- **各ミラーは既定 mode が異なる**: 公開 M 既定 = `route`（本編複製）、メンバー P 既定 = `filler`（待機）。`MirrorController` を**ミラー単位（seg 別）にインスタンス化**し、各々の既定 mode・CasparCG ch・filler clip を持たせる。
- **mode は永続化**: 窓は数十分〜数時間続くため、最中の CasparCG/agent 再起動で本編が公開 YouTube に漏れない／メンバー枠が外れないよう、各ミラーの現 mode を agent ローカル(`queue.set_state`)に seg 別保存し、再接続時に再適用。

## 4. 変更詳細

### 4.1 proto / アクション定義
- `proto/icstv/v1/playout.proto`: `PlayoutAction` に `PLAYOUT_ACTION_YT_MIRROR_FILLER = 10` / `PLAYOUT_ACTION_YT_MIRROR_ROUTE = 11` を追記（番号追記＝後方互換）。対象ミラーは seg で運ぶため payload 追加は不要。`buf generate` で server 側 `icstv/v1/` と agent 側 `agent/icstv_proto/` を**両方**再生成。
- `server/playout/models.py`: `PlayoutAction(TextChoices)` に `yt_mirror_filler`/`yt_mirror_route` 追加。`AlterField` migration を新規追加。
- `server/playout/grpc_service.py`: `_ACTION_DB_TO_PROTO` に 2 エントリ追加（無いと UNSPECIFIED に落ちる）。

### 4.2 データモデル（ポリシー + 案内素材）
- `server/scheduling/models.py`:
  - `Program.exposure_policy = CharField(choices=ExposurePolicy, default="public")`（権威フィールド）。**※初版の `Program.site_only: bool` は廃止し本 enum へ統合。**
  - `Series.exposure_policy_default = CharField(...)`
  - `SeriesSlot.exposure_policy = CharField(...)`（週間展開時に Program へコピー。genre/cast の継承と同流儀。Phase C 未配線なら拾うフックだけ用意）
  - migration（未実装なので新規 AddField のみ。初版 bool が既に入っている場合は RemoveField + AddField）
- `server/core/models.py`:
  - `Channel.site_only_filler = ForeignKey("medialib.Asset", on_delete=SET_NULL, null=True, blank=True, related_name="+")`（公開ミラー用。`slate_asset` と同型）
  - `Channel.members_filler = ForeignKey("medialib.Asset", ...)`（メンバーミラー待機画用）
  - AddField migration ×2

### 4.3 resolver（ミラー制御イベント発行）
- `server/scheduling/resolver.py` の `resolve()` ループで、`emit_recorded`/`emit_live` を `pending` に積んだ直後、`prog.exposure_policy` に応じて新ヘルパ `emit_exposure_mirror(prog)` の戻りも積む:
  - **公開フィラー**（`public` 以外）: 公開ミラーに `YT_MIRROR_FILLER@start` / `YT_MIRROR_ROUTE@end`（seg `yt_public`）。
  - **メンバー本編**（`members_yt_site` のみ）: メンバーミラーに `YT_MIRROR_ROUTE@start` / `YT_MIRROR_FILLER@end`（seg `yt_members`）。※既定 filler なので「窓中だけ route」。
- **連続区間最適化（フリッカ防止）**: 同 seg・背中合わせ番組の境界で `ROUTE`(end)↔`FILLER`(start) が同時刻に並ぶと `due_for_take` の同時刻順序が不定（`agent/icstv_agent/queue_db.py`）なので本編が一瞬露出しうる。resolver 側で **seg ごとに**連続区間をまとめ、区間先頭/末尾で 1 回ずつだけ発行する（公開フィラー連続・メンバー本編連続を独立に判定）。
- `_commit()` の掃除は既存どおり: `exposure_policy` 変更の再解決で `YT_MIRROR_*` が自動 tombstone 化（resolver 管轄）。

### 4.4 agent（ミラー制御の実体）
- 新規 `agent/icstv_agent/mirror.py`: `MirrorController`。**ミラー単位（seg 別）にインスタンス化**。
  - 状態 `_mode ∈ {"route","filler"}`、既定 mode をコンストラクタ引数で受ける（公開 = route、メンバー = filler）。`handle_event(action)` で mode 更新＋AMCP 発火、`reapply()` で現 mode 再送（冪等）。
  - mode を `queue.set_state(f"yt_mirror_mode:{seg}", mode)` で seg 別永続化、起動時に既定 mode で復元。
  - `mirror_channel is None` なら全メソッド no-op（既存 1ch/2ch ノードを壊さない）。
- `agent/icstv_agent/amcp_planner.py`: 純粋関数追加（`slate_command` と同流儀。公開/メンバーで `mirror_ch` と `filler_clip` が違うだけで同関数）:
  - `yt_mirror_filler_command(mirror_ch, filler_clip) -> 'PLAY {mirror_ch}-10 "{clip}" LOOP'`
  - `yt_mirror_route_command(mirror_ch, main_ch) -> 'PLAY {mirror_ch}-10 route://{main_ch}'`（layer 無し＝channel 合成）
- `agent/icstv_agent/config.py`: `yt_mirror_caspar_channel`(公開M, env `ICSTV_YT_MIRROR_CASPAR_CHANNEL`)、`yt_members_caspar_channel`(メンバーP, env `ICSTV_YT_MEMBERS_CASPAR_CHANNEL`)、各 既定 None。`site_only_filler_clip`/`members_filler_clip`（各 env、既定フォールバック clip）。
- `agent/icstv_agent/channel_media.py`: `site_only_filler_clip`/`members_filler_clip` 属性＋setter（`slate_clip` と同型）。
- `agent/icstv_agent/main.py`:
  - `run()`: `ChannelMedia(...)` 拡張、`MirrorController` を **seg 別に 2 つ**生成し各ループへ渡す。
  - `_do_take`/`_do_loadbg` 冒頭: action が `YT_MIRROR_*` なら seg で対象 controller へ委譲（`plan()` に通さず、loadbg 段は `mark_loaded` 素通り、take 段は `mark_executed`＋DONE 報告で return。slate/yt_transition 経路に倣う）。
  - `_dispatch_loop` の「切断→再接続」ブロックと初回接続で **全 controller `reapply()`**（route 確立・mode 復帰を 1 経路で）。
  - `_prefetch_manifest`: manifest item の `site_only_filler`/`members_filler` フラグを拾い各 setter（`slate` フラグ処理と同型）。

### 4.5 prefetch（案内素材の常駐 pin）
- `server/playout/grpc_service.py` `_build_prefetch_manifest`: slate 項目と同型で `channel.site_only_filler` と `channel.members_filler` を各 1 件追加（presigned URL、各 flag 付）。manifest 同梱トリガを `PLAY_FILLER`/`YT_MIRROR_FILLER` で発火。`select_related` に両 FK 追加。
- 二重化: manifest 未着時も各 env clip（ノードローカル実体）にフォールバック。404 でもミラーが黒/待機画になるだけで**本線（サイト）は無傷**＝停波にはならない。

### 4.6 deploy（送出ノード）— 機微なカットオーバー
- `deploy/playout-node/casparcg/casparcg.config`: ミラー `<channel>` を **2 本**（M/P）追加。**video-mode は本線と同一（720p6000）必須**（route producer 前提）。UDP は採番継続（ch1 公開ミラー=5006 / メンバーミラー=5007 等）。
- 新規 `icstv-yt-mirror-encoder@.service`（公開 ingest）＋ `icstv-yt-members-encoder@.service`（members ingest）＋ env example: 各ミラー UDP → h264_vaapi → **YouTube のみ**（tee なし単一出力）。本線 unit の実証済みパラメータ（`-bf 0` / `-flags +global_header` / VAAPI）を踏襲。
- **本線 encoder のカットオーバー**: `deploy/playout-node/systemd/icstv-encoder@.service` の tee から **YouTube leg を外し MediaMTX(HLS) leg のみ**にする。本番 YouTube 経路に触れるので、**検証ノード(dev)で先に確認 → 本番は監視下でカットオーバー**。
- `deploy/playout-node/env/agent.env.example`: `ICSTV_YT_MIRROR_CASPAR_CHANNEL` / `ICSTV_YT_MEMBERS_CASPAR_CHANNEL` / 各 filler clip を追記。`scripts/install.sh` に案内素材 placeholder ×2 生成・ミラー unit ×2 enable を追記。
- **導入前提（既設ノードの落とし穴）**: 案内フィラー既定 clip（`filler/site_only_default` / `filler/members_default`）の placeholder を生成するのは `install.sh`（プロビジョニング専用・稼働ノードでは再実行しない）だけなので、#27 実装以前に構築済みの現行の送出ノードには**実体が存在しない**（2026-09-02 実査: `/opt/casparcg/media/filler/` に default 系 0 件）。exposure_policy 有効化前に media-path の `filler/` へ実体を置くか、`Channel.site_only_filler` / `members_filler` の manifest を設定すること（両方無いとミラーが黒/待機画のまま。本線は無傷）。
- **GPU**: N+M+P で VAAPI 連続エンコードが 3 セッション同時。UHD630 は本線+公開ミラーの 2 本で既に RCS ~90% のため、3 本目は**ほぼ確実に容量超過 → 専用 GPU 増設が実質前提**（§5-1）。

### 4.7 管理 UI / アクセスゲート
- 番組編集フォーム / Series 編集 / `server/templates/admin_ui/channel_settings.html` に **`exposure_policy` セレクタ（ラジオ 4 プリセット）**＋各面挙動の説明文。`members_yt_site` には GPU/ops 警告（専用 GPU・members 配信手動作成が前提）を表示。
- channel_settings に `site_only_filler` / `members_filler` セレクタを追加。
- **サイト会員ゲート（重要）**: `site_members`/`members_yt_site` の本線 HLS は **エッジで認証**する（MediaMTX path 認証 or 署名 URL）。プレイヤー島は member/subscription の entitlement("exclusive") で再生可否を判定し、非会員にはゲート（ログイン/入会への導線）。**プレイヤー UI を隠すだけでなく manifest 自体を保護**しないと URL 流出で素通りになる（公開プリセットは無認証）。
- **ファンクラブ ティア ゲート（2026-07-28 追加、docs/fanclub.md §6.4）**: 上記の「会員か否か」の
  2値ゲートに加え、`Program.fc_required_level`（NULL=完全公開/0=無料会員以上/n=有料ティアn以上）
  でクリエイター単位のティアレベル軸も判定する（`scheduling/exposure_gate.py`）。共有ライブ再生 URL
  (`Channel.cf_playback_hls_url` は全番組で同一URL。実体は自前 HLS = §5 リスク#3) の露出経路すべて（`/api/v1/channels/{slug}`・
  `/api/v1/home`・`/live/<slug>/poster.jpg` のライブ静止画）を同じ判定関数経由に統一し、
  一部だけゲートを素通りする事故を防ぐ設計にした。署名トークンもこの軸単独のゲートに対して発行する。
- フィラーの案内コピーは媒体別: 公開 YouTube フィラーは「本編は会員限定／本サイトで配信中」＋概要欄リンク（インタラクティブ不可なのでテロップ＋概要欄＋固定コメントに限る）、メンバー待機画は「次のメンバー限定番組まで」。

## 5. リスク

1. **GPU エンコードセッション枯渇（前提条件化）**: メンバー leg 専用 ch 採用で VAAPI 計 **+2**（N+M+P の 3 セッション同時）。UHD630 は 2 本で RCS ~90% のため 3 本目はほぼ超過＝**専用 GPU 増設が実質前提**。実機計測（`intel_gpu_top`）で確認、超過時はミラーを 720p30 化 or 専用 GPU（MEZZ_VCODEC 残課題）。
2. **YouTube members-only の API 制御**: 可視性のプログラム的設定が不確実 → **永続 1 本を手動作成**して常用、窓制御はミラー P の route/filler 切替で行う（per-window 作成は要検証）。
3. **サイト会員限定の HLS 保護**: UI ゲートのみでは manifest 流出で素通り。**エッジ認証 / 署名 URL 必須**。
   **2026-07-28 追記（実装後のアドバーサリアル レビューで具体化）**: 署名トークン
   (`core/hls_auth.py`) は channel + 有効期限のみを検証し番組/ティアを見ないため、エッジ強制が
   未配線だった当時は、公開番組の時間帯に取得した URL（署名無し）や下位ティアで正当発行された
   トークンを、同じ共有ライブ HLS 上で後から始まるゲート対象番組へもそのまま使い回せる
   （「流出」のような悪意を要さず、通常利用の延長で突破できる）。ファンクラブ ティア軸
   （docs/fanclub.md §6.4）にもこの限界はそのまま引き継がれる。
   **2026-07-29 訂正・具体化（本番実測に基づく）**:
   - 旧記述は「真の対処 = Cloudflare Stream `requireSignedURLs` 有効化」としていたが、**これは
     現行アーキテクチャには適用できない**。§4.7 当時の想定 (CF Stream が視聴者向け HLS も配信する)
     から ABR ladder 移行で実配信経路が変わっており、本番 DB の
     `Channel.cf_playback_hls_url` は ch1/ch2 とも `https://tv.yagamin.net/hls2/<slug>/master.m3u8`
     = 送出ノードの nginx (:8889) 静的配信を k8s ingress-nginx で逆プロキシしたもの。
     CF Stream (Live Input/Output) はブラウザ再生経路にいない（さらに現行は本線 YouTube 送出も
     encoder の直 push で CF を経由せず、CF オブジェクトは studio 手動操作と fanclub simulcast
     用の残存のみ — [youtube.md](youtube.md) §本線 egress）。
   - この経路は**固定パスで、認証なしで誰でも 200 が取れる**（2026-07-29 実測。`?token=` の有無は
     無関係で、そもそも検証する主体がいない）。つまり本線ライブのゲートは実質「プレイヤー UI に
     URL を出さない」だけで、URL を一度でも知った相手には無効。署名トークンは現状 100% 装飾的で、
     `DEFAULT_EXPIRES_SEC` の短縮を含むアプリ層だけの変更では実効性は生まれない。
   - 実効性のある選択肢は3つあった: (a) k8s ingress-nginx の `auth-url` アノテーションで既存
     `/api/v1/hls-auth` を実配線 (新規の外部サービスは不要だが、HLS 配信の可用性が icstv-web に
     連結する)、(b) Cloudflare Worker で HMAC を検証 (icstv-web の可用性と切り離せる)、
     (c) 送出ノード側 nginx の `secure_link` 等。
   - **2026-07-29 決定・実装: (b) Cloudflare Worker を採用**
     (`deploy/cloudflare-worker-hls/`)。24/7 送出の可用性を
     Django に連結させない点を最優先した。Worker は ①`?token=` を検証し、②オリジンへは token を
     外した URL で取得して CF のキャッシュキーを視聴者間で共有させ、③`.m3u8` を書き換えて
     子プレイリスト/セグメントの相対 URI へ token を伝播する (相対 URI は親のクエリを
     引き継がないため)。段階導入のため `HLS_AUTH_ENFORCE=false` の監視モードを持つ。
   - **2026-07-30: enforce へ切替 (本番稼働中)**。監視モードで判定を確認 (正当 token=ok /
     無し・改竄・期限切れ・別 channel=invalid) し、実再生の連鎖 (master → 子プレイリスト →
     セグメント) も確認したうえで `HLS_AUTH_ENFORCE = "true"` へ。**token 無しのアクセスは
     エッジで 403** になり、「固定パスで誰でも 200 が取れる」状態は解消した。設定変更後は
     伝播待ちが要る (数秒〜十数秒)。ロールバックは `false` に戻して `wrangler deploy`。
     403 の監視手順は `deploy/cloudflare-worker-hls/README.md` に記載。
   - 併せてサーバ側を **完全公開の番組にも署名を付ける**方式へ変更した。トークン無しで再生できる
     経路が1つでも残るとエッジ強制が成立しないため (§4.7 の「公開はエッジ認証不要=素通し」は撤回)。
   - **残る限界**: トークンが束縛するのは channel と期限だけで、エッジからは番組/ティアを判定
     できない。したがって**エンタイトルメント変更が効くまでの最大遅延は TTL**
     (`ICSTV_HLS_TOKEN_TTL_SEC`、既定 3600 秒) になる。TTL 短縮が唯一の縮め方で、hls.js 側は
     再取得したトークンを再生を維持したまま差し替える (`@icstv/api` の `createHlsTokenLoader`)。
     Safari のネイティブ HLS はこの介入ができず、TTL が連続視聴時間の上限になる。
4. **本番 YouTube 経路のカットオーバー**: 本線 tee の YouTube leg 除去とミラー ×2 投入。dev 先行＋監視下で。
5. **同時刻 ROUTE↔FILLER の露出**: §4.3 の連続区間最適化を **seg ごとに**根治。
6. **再起動中の mode 喪失**: §4.4 の `queue.set_state` を **seg ごとに**永続化（必須）。
7. **route の layer/channel 指定**: `route://N`（channel 合成）。`route://N-10`(layer) だとサイトと画が食い違う。
8. **proto 再生成漏れ**: server/agent 両方で `buf generate`。

## 6. 検証

1. **単体（CasparCG 不要）**: `amcp_planner.yt_mirror_*_command` の文字列、`resolver.emit_exposure_mirror` の `_ikey` 非衝突（seg 別）・連続区間最適化、`MirrorController`（seg 別 `handle_event/reapply/mode 永続化`）の pytest。
2. **resolver 結合**: 4 プリセット各窓で `resolve()` を回し、公開/メンバー各ミラーイベントが正しい seg・時刻で出て**本線イベントが不変**、`exposure_policy` 解除の再解決で tombstone 化、連続区間最適化が seg 独立に効くことをアサート。
3. **AMCP smoke（実 CasparCG）**: `deploy/playout-node/scripts/amcp-smoke.py` 拡張。M/P 両ミラーで `route://N` 一致・filler 差替・route 復帰、メンバー既定が待機 filler であることを確認。
4. **再起動復帰**: 窓中に `systemctl restart casparcg-server`（再接続 reapply）/ `restart icstv-agent@ch1`（state 復元）で **M=filler・P=route** が維持されることを確認。
5. **E2E**: filler⇄route 差替で**公開/メンバー両 YouTube ingest が再接続しない**ことを YouTube Studio で確認。サイト HLS は本編継続＋会員ゲートで非会員がブロックされることを確認。
6. **容量**: N+M+P の VAAPI セッション数と GPU 使用率を計測。

## 7. 実装順（フロント作業が落ち着いてから）

1. proto / モデル（`exposure_policy` enum, `members_filler`）/ migration / grpc enum（§4.1,§4.2）→ buf 再生成
2. resolver（§4.3、seg 別ミラーイベント＋連続区間最適化）+ 単体/結合テスト
3. agent: `MirrorController`（seg 別 ×2）・amcp_planner・config・main 統合・prefetch（§4.4,§4.5）+ テスト
4. deploy: casparcg.config 2 ミラー・encoder unit ×2・env（§4.6、本番カットオーバーは dev 先行）／**GPU 計測・専用 GPU 手当**
5. 管理 UI（§4.7、`exposure_policy` セレクタ・channel filler ×2）＋**サイト会員 HLS エッジゲート**
6. 実機 AMCP smoke / 再起動復帰 / 容量計測（§6）
