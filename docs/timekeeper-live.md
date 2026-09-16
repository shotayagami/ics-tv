# タイムキープ（生放送 進行管理）— 設計方針

> ステータス: **確定・Phase 0–3 実装/本番反映済み（prod v0.8.89, 2026-07-08）**。初版 2026-07-03。
> 目的: TV の「タイムキーパー」業務を**少人数（現場にスマホ 1 台だけ）で回せる**ようにする、生放送スタジオ向けの**タイムキープ画面**を新設する。放送の残り時間・今出ているもの・次のもの・CM 入り/明けと開始/終了のカウントダウン・**枠内に流し切るべき CM の残本数と残尺**・**生番組内の録画セグメント（VT）の送出管理**を、一目で把握し親指で操作できるようにする。本ドキュメントは実装前の**方針の正本**。
> 関連: [overview.md](overview.md)（意思決定 #25）, [operations.md](operations.md)（APC 運行・押え/巻き・feed 監視）, [ui-operations.md](ui-operations.md)（ops.* モバイルコンソール）, [scheduler.md](scheduler.md)（resolver / PlayoutEvent / ad_break）, [cg-layers.md](cg-layers.md)（CM バンパー layer20 / OVERLAY_OP）, [sales.md](sales.md)（CmCreative / aired_count）, [datamodel.md](datamodel.md), [refactor-service-split.md](refactor-service-split.md) §11（放送面モバイル）。

---

## 1. 目的・背景

### 1.1 やりたいこと（要件）
生放送スタジオ側で、少人数運用でも次を瞬時に確認・操作できるタイムキープ用画面:

- **現在時刻**と**放送の残り時間**。
- **今出ているもの**（本線 / CM / 録画セグメント）と**次に出るもの**。
- キューシートがあれば、放送残り時間に加えて**次の CM まで / 次のセクションまで**の残り時間。
- **CM 入り・明けのカウントダウン**、**放送開始前・終了前のカウントダウン**。
- 生放送では**任意タイミングの「CM 入り」「本線復帰」ボタン**が必須。
- 番組内で流し切らねばならない**CM の残本数と残尺**の表示。
- CM と同様、**生番組内の録画番組（VT）の送出管理**。
- 上記のため、**生番組にもキューシートが必要**になる（現状は録画のみ）。
- 現場に PC が無い前提 → **スマホファースト**。

### 1.2 確定した方針判断（ユーザー回答 2026-07-03）
- **CM/VT 発火 = 手動＋任意で自動発火**。手動発火が主。cue 単位で「予定オフセットで自動 CM 入り」を選べる。手動は常に自動を上書きできる。→ §6。
- **カウントダウンの出力先 = オペレータ画面のみ**。視聴者向けオンエア CG カウントダウンは作らない（CEF テンプレ新設なし）。ただし CM 入りの layer20 バンパーは手動経路でも発火するよう塞ぐ。→ §5.3・§7。
- **枠と尺 = 枠（`end_at`）固定＋押し/巻き表示**。放送枠がハード境界。キューシート積算は「予定尺」で、枠との差＝押し/巻きとして見せる。積算で `end_at` を自動算出はしない。→ §3・§7。

---

## 2. 現状の制約（コード実査）

### 2.1 送出の「今」には 2 つの真実がある（意図的に併存）
- **編成** = `Program.start_at/end_at`（`server/scheduling/models.py`）。放送枠の開始/終了。押え/巻き（`extend_program`/`shorten_program`（`server/scheduling/services.py`））で動く。**「残り時間」の基準はこちら**。
- **実行チェーン** = `PlayoutEvent`（`server/playout/models.py`）。実際にオンエアされた as-run。「今何が出ているか」は `on_air_info`（`server/core/now_playing.py`）＝`scheduled_at<=now` の非 CANCELLED 最新イベント（overlay/transition 除外）。手動 CM 入り・フィラー継続などを反映する。

### 2.2 キューシートは録画専用（＝生放送に無い構造的理由）
- `CueSheet`（`server/medialib/models.py`） は `Asset` と **OneToOne**。行 `CuePoint`（`kind ∈ content|ad_break`, `duration_ms`(ms), `grid`, `label`）。積算尺は `program_airtime_ms`（`server/medialib/services.py`）（cuesheet あり→素材尺＋ΣCM枠尺 / なし→素材尺）。
- 生放送 `Program` は `chk_program_source` 制約で **`asset=NULL` 強制** → cuesheet を持てない。
- CM は**録画番組だけ** `emit_recorded`（`server/scheduling/resolver.py`） が `ad_break` を `PLAY_CM` として**時刻付きで先に並べる**。生放送は `emit_live`（`server/scheduling/resolver.py`） が **`CUT_LIVE` を 1 個出すだけ**（CM 予定も残数も無い）。
- 生放送の CM は今、手動の `CmBundle` reel（`op_cm_in`→`PLAY_CM_BUNDLE`）のみ。**予定・義務・残数の概念が一切無い**。

### 2.3 既に使える資産（新規に作らず流用する）

| 要件 | 既存資産 | 場所 |
|---|---|---|
| CM 入りボタン | `op_cm_in`（`CmBundle`→`PLAY_CM_BUNDLE`、reel 末尾で `return_rtmp_url` 自動本線復帰） | `server/core/ops_views.py` |
| 本線復帰ボタン | `op_cm_return` / `op_cut_live_return`（`CUT_LIVE` 再発行＋slate 解除） | `server/core/ops_views.py` |
| 押え/巻き | `op_extend`/`op_shorten`（生は end_at 変更・GUARD 60s） | `server/core/ops_views.py` / `server/scheduling/services.py` |
| 即時発火の土台 | `insert_immediate_event`（冪等 uuid5・now に SCHEDULED を 1 行） | `server/core/views.py` |
| 「今出す＋N秒後に自動で消す」型 | `fire_chime` / `fire_breaking_telop`（now＋未来 OVERLAY_OP のペア） | `server/core/views.py` |
| CM 入りバンパー | layer20 `cg_cm_in`/`cg_cm_out`（**録画のみ**発火・手動経路は未発火＝穴） | `agent/icstv_agent/amcp_planner.py` |
| モバイル操作面 | `@icstv/ops` React SPA（ops.* ホスト・5 秒ポーリング・親指ボタン・staff_auth） | `frontend/apps/ops/src/pages/OpsConsole.tsx` |
| epoch＋1 秒 tick の時計 | 公開プレイヤーが実装済（サーバ epoch→クライアント毎秒補正） | `frontend/apps/player/src/hooks.ts` |
| now/next 解決 | `_now_playing_ctx`（on_air＋upcoming＋live_program） | `server/core/ops_views.py` |

### 2.4 決定的な穴（＝新規に埋めるもの）
1. **生放送に進行表（キューシート）を持てるモデルが無い** → 次 CM/次セクション/残 CM の源泉が無い。
2. **ops の JSON が時刻を `HH:MM:SS` 文字列でしか返さない**（`ops_status`（`server/api/routers/admin_ops.py`） の `_t()`）→ カウントダウン不能。machine-readable epoch＋`server_now` が要る。
3. **生放送の録画セグメント（VT）挿入の第一級モデルが無い**（今は `CmBundle` 手動のみ）。
4. **手動 CM 入りで layer20 バンパーが出ない**（録画の `_annotate_cg` だけが `cg_cm_in` を立てる）。
5. **枠内に流し切るべき CM の義務台帳が無い**（`CmCreative.aired_count` はキャンペーン全体の集計で per-broadcast ではない）。

---

## 3. 中心思想

> **生放送のキューシート = 「進行表（ランダウン）＝義務台帳＋参照タイムライン」であって、固定スケジュールではない。**

TV のタイムキープの本質は「①枠は決まっている ②進行は人が動かす ③画面が“義務”と“残り時間”を監視して人を守る」。生放送で固定時刻に CM を自動発火させるのは筋が悪い（トークは伸び縮みする）。よって:

- **枠（`Program.end_at`）= ハード境界**。押え/巻き・YouTube ライフサイクル・EPG がこれに従う。**「残り時間」= `end_at − now`**。押え/巻きで動くので**毎ポール読み直す**（固定 epoch をキャッシュしない）。
- **キューシート積算 = 予定尺**（あくまで参考）。枠との差＝**「押し／巻き」**として表示する（積算で end_at は決めない＝確定判断③）。
- **CM/VT は手動発火が主**（要件通り）。cue 単位で任意に自動発火を有効化できるが、**手動は常に上書き可能**（確定判断①）。
- 画面は「まだ流していない CM＝残本数/残尺」を**義務台帳**として監視し、枠内に収まらなければ赤で警告する。
- タイムキーパー画面は**生・録画 両対応**。ただし**手動発火セクションは生放送のみ**（録画は自動なので閲覧＋緊急操作＝slate/本線復帰だけ）。データ源は「進行表があればそれ／なければ録画の ad_break・PLAY_CM から投影」で切り替え、UI は同一。

---

## 4. データモデル（新規・既存を壊さない）

`CueSheet`（asset-scoped）はいじらず、`chk_program_source` も温存。**`Program` に紐づく別モデル**を足す（`server/scheduling/models.py`）。

```
LiveRundown        (Program と OneToOne)
  program, note, updated_at

LiveCue            (LiveRundown に FK — 進行表の 1 行)
  seq                                 順序（unique(rundown, seq)）
  kind ∈ { section本編 | cm | vt録画 }
  label                               表示名（「トークB」「CM枠②」「VT特集」）
  planned_duration_ms                 予定尺（次セクション/次CMまでの算出源・押し/巻き算出源）
  cm_bundle  (FK CmBundle, kind=cm時) 発火する CM reel（op_cm_in を流用）
  asset      (FK Asset,    kind=vt時) 送出する録画素材（正規化 READY 前提）
  grid       (kind=cm時, 15s/20s)     枠尺の丸め（CmGrid 流用）
  state ∈ { pending | firing | aired | skipped }
  fired_event (FK PlayoutEvent, 発火時に結線＝確実な as-run 台帳)
  auto_fire (bool, 既定 false)        任意自動発火（確定判断①）
  auto_offset_ms (int, nullable)      放送開始からのオフセット（auto_fire=true時）
```

- **残 CM** = `state=pending` の `cm` cue の本数 / Σ`planned_duration_ms`。**残 VT** も同様。→ 要件「流さないといけない CM の残本数と時間」を**per-broadcast の義務**として満たす。
- 発火＝`insert_immediate_event` ＋当該 cue を `firing`→`aired` 化（`fired_event` 結線）。**as-run の曖昧マッチに頼らず確実**。`aired` 化は `select_for_update` で原子的（多人数の二重発火抑止）。
- **録画番組が今オンエア中の場合**は新モデル不要。既存 `AdBreak`／未発火 `PLAY_CM` を同じ `rundown[]` 形に投影して同じ画面で見せる。
- `end_at` は**変更しない**（確定判断③）。積算尺は表示専用の派生値。
- マイグレーションは**field 追加のみ**（既存挙動不変）。`SeriesSlot` から雛形展開する場合も既存 `expand_series_slots` に live 分岐を足す（別 PR 可）。

---

## 5. サーバ API

### 5.1 新エンドポイント（読み取り）
`GET /api/v1/admin/ops/{slug}/timekeeper`（`server/api/routers/admin_ops.py` に併設・`staff_auth`）。`ops_status` は文字列専用なので**別建て**。machine-readable（全時刻 epoch）:

```
server_now                          クライアント時計ズレ補正の基準
broadcast: { program_id, title, state:pre|onair|post,
             start_at, end_at,       枠（押え/巻きで動くので毎回読む）
             next_on_air }           休止明けの再開時刻（Channel.next_on_air）
now:  { kind: line|cm|vt|filler|slate, label, segment_end_at|null }
next: { kind, label, at|null }
next_cm_at, next_section_at          進行表（or 録画 ad_break）から算出、null 可
cm_remaining:  { count, seconds }    義務台帳（生=pending cue / 録画=未発火 PLAY_CM）
vt_remaining:  { count, seconds }
planned_total_ms, over_under_ms      予定尺 と 枠 の差（押し=+/巻き=−）
rundown: [ { seq, kind, label, planned_at, planned_dur, state, auto_fire } ]
health: { online, feed_state, slate_active, auto_return }   既存 _health_ctx 流用
```

算出は `_now_playing_ctx`（`server/core/ops_views.py`） と `on_air_info`（`server/core/now_playing.py`） を拡張して再利用（now/next を再実装しない）。

### 5.2 新オペレーション（書き込み・`server/core/urls.py` の `OPS_OPERATIONS` に追加＝ops.* から POST 可）
- `op_cm_now` … 進行表の**次の `cm` cue をワンタップ発火**。bundle 事前割付済なので内部で `op_cm_in` 相当を流用。**同時に layer20 バンパー（＋任意チャイム）発火**（§5.3 の穴を塞ぐ）。cue を `aired` 化。
- `op_roll_vt` … **録画セグメント送出**。`op_cm_in` の兄弟。`asset` を `return_rtmp_url` 付きで即挿入（PLAY_CM_BUNDLE の返し reel 機構を流用、または PLAY_ASSET＋return 版）。cue を `aired` 化。
- 既存 `op_cm_return`/`op_cut_live_return`（本線復帰）、`op_extend`/`op_shorten`（押え/巻き）はそのまま流用。
- `op_skip_cue` … 進行表の cue を `skipped` に（自動発火 cue の取り消し・残義務から除外）。

### 5.3 CM 入りバンパー（手動経路の穴を塞ぐ）
`op_cm_now`/`op_cm_in` 発火時に `cg_cm_in`（layer20）を併発し、reel 末尾/本線復帰時に `cg_cm_out`。`fire_chime` の「now＋未来ペア」idiom（`server/core/views.py`）を流用。**視聴者向けの新規カウントダウン CG は作らない**（確定判断②）。

---

## 6. 生成側・自動発火（確定判断①「手動＋任意で自動発火」）

- **既定は手動**。進行表は「次の CM まで（予定）」を**助言表示**し、実発火はオペレータのボタン。`emit_live` は基本いじらない。
- **cue 単位 `auto_fire=true` の場合のみ**、resolver `emit_live`（`server/scheduling/resolver.py`） を拡張し、`start_at + auto_offset_ms` に `PLAY_CM_BUNDLE`/`PLAY_ASSET`（return 付き）を**予約発行**する。`emit_recorded` の実装がそのまま雛形。
  - `_commit` 対策: これらは `emit_live` が発行するので `new_keys` に入り、次の resolve でも**キャンセルされない**（読み取りリーダーが指摘した穴を回避）。
  - **手動上書き**: `op_cm_now` は当該 cue の予約 SCHEDULED を先にキャンセルしてから即時挿入（早出し）。`op_skip_cue` は予約をキャンセルし `skipped` 化。
- **ドリフト注意**: 生放送は伸び縮みするので `auto_offset_ms`（開始基準）は「開始が予定通り」を仮定する脆さがある。**確実な固定時刻 cue（ネット CM の定時ジョインなど）だけ auto_fire を推奨**し、可変尺のトーク明け CM は手動のままにする、という運用指針を UI ヘルプに明記。実行（emit_live 拡張＋agent 予約）は **Phase 2**（§10）。

---

## 7. 画面（モバイルファースト・親指操作）

DS は現行 ops の Bootstrap5 `@icstv/theme` ＋ `ops-*`/`st-*` クラスに合わせる（**旧 `@icstv/ui`/`@icstv/studio-ui` は削除済**）。時刻はサーバ epoch＋**毎秒ローカル tick**（公開プレイヤー方式）。

```
┌─────────────────────────────┐
│ ● 生 ICS-TV1        14:32:07 │  健全性ドット＋壁時計(大)
├─────────────────────────────┤
│         番組終了まで         │
│           27:41             │  枠残り(最大) 緑>5分/橙<5分/赤<1分・超過は反転
│   「夕方ライブ」〜15:00  押し+0:45 │  枠固定＋予定との差(押し/巻き)
├─────────────────────────────┤
│ NOW  ● 本線(トーク)          │
│        次のCMまで  03:12     │  生=予定(助言)、録画=実スケジュール
│ NEXT   CM枠②(30秒)→トークB   │
├─────────────────────────────┤
│ 残CM 3本/1:30 ⚠  残VT 1本/5:00│  義務台帳(枠内に収まらない=赤)
├─────────────────────────────┤
│ 進行表                       │
│ ✓ OP          0:00  済       │
│ ▶ トークB      6:30  ON      │  現在行ハイライト
│ ○ CM枠②(30")  10:00 待 [自動]│  auto_fireは[自動]チップ
│ ○ VT特集(5')  10:30 待      │
│ ○ CM枠③(60")  22:00 待      │
│ ○ ED          23:00 待      │
├─────────────────────────────┤
│ [  CM入り  ]  [  本線復帰  ] │  固定フッタ・大ボタン
│ [  VT送出  ]  [ 自動復帰:入 ]│
└─────────────────────────────┘

▼ CM中は全画面カウントダウンに切替
        CM明けまで  0:18
      [ 今すぐ本線復帰 ]

▼ 放送前/終了間際（放送開始前・終了前カウントダウン要件）
   オンエアまで 02:45   /   終了まで 00:45 ⚠残CM2本
```

- 状態色・全画面カウントダウン（CM 入り/明け・開始/終了）・**残 CM が枠内に収まらない時の赤警告**が一目で分かること、ボタンは**下段で親指が届く**ことが肝。
- 「CM 入り」は進行表に次 CM cue があればワンタップ（`op_cm_now`）、無ければ従来の bundle 選択（`op_cm_in`）にフォールバック。

---

## 8. リアルタイム・多人数・権限

- **時刻**: サーバは `server_now`＋各 epoch を返し、クライアントは毎秒ローカル tick＋ズレ補正。ポーリングは 5 秒→**2〜3 秒**。WS push（`server/members/consumers.py` の channels 基盤流用・`playout_<slug>` group）は **Phase 2 の任意強化**。
- **残り時間は毎ポール `end_at` 読み直し**（押え/巻きで動く）。
- **多人数**: 発火は冪等（uuid5）、cue の `aired` 化は `select_for_update`、`firing` 表示で二重発火抑止。
- **認証**: 既存 `staff_member_required`（ops.* は CF Access 前段）。

---

## 9. 編成側（studio）での作成 UI

進行表の**作成は当日より前・PC 作業**なので studio（🟡編成）側に置く（ops.* はあくまで当日の操作面）。

- 生放送 `Program` に「**生放送キューシート**」エディタを新設。`cuesheet_editor`（`server/medialib/views.py`）（HTMX）のパターンを流用し、`section`/`cm`（`CmBundle` 割付）/`vt`（`Asset` 割付）行を並べ替え。各 cue に `planned_duration_ms`・任意 `auto_fire`＋`auto_offset_ms`。
- 積算尺と枠（`end_at`）の差を編成時にも表示（押し/巻きの事前把握）。
- `SeriesSlot` に定番進行表（雛形）を持たせ、`expand_series_slots` の live 分岐で毎回展開（別 PR 可）。

---

## 10. 段階リリース（migration 無を先に、の方針に沿う）

- **Phase 0 — 読み取りタイムキーパー（migration 無・生録両対応で即戦力）**
  `timekeeper` エンドポイント（既存データのみ：`Program.start_at/end_at`＋on_air＋録画の `PLAY_CM`）＋ops.* タイムキーパー画面（大時計／枠残り／now・next／開始・終了カウントダウン／押し・巻き）＋既存 `op_cm_in`(bundle 選択)/`op_cm_return` ボタン。進行表・残 CM・生の次 CM・VT はまだ無し。
- **Phase 1 — 生キューシート＋義務台帳（migration 有＝field 追加のみ）**
  `LiveRundown`/`LiveCue`＋studio エディタ＋残 CM/残 VT＋次 CM/次セクション＋ワンタップ `op_cm_now`/`op_roll_vt`＋`op_skip_cue`＋手動 CM 入りバンパー（§5.3）。**ここが本体**。auto_fire は**フラグ保持のみ**（実行は Phase 2）。
- **Phase 2 — 自動発火実行＋磨き込み**
  `emit_live` の auto_fire cue 予約発行＋agent 予約＋手動上書き（§6）、WS push、多人数ロック強化、押し/巻き差分の精緻化。**視聴者向けオンエア CG カウントダウンは対象外（確定判断②）**。
- **Phase 3 — §7/§11 の残タスク（prod v0.8.88/89, 2026-07-08）**
  - **A**: CM/VT 送出中に**全画面カウントダウン**へ切替（`Timekeeper.tsx` cmMode・「◯明けまで」大表示＋今すぐ本線復帰）。§7 の全画面テイクオーバーを実装。
  - **B**: ops の予定尺/押し/残尺を studio と同じ **mm:ss 秒精度**へ（`fmtClock`）。
  - **C**: **定番進行表テンプレ**（§9）。`LiveRundownTemplate`/`…Cue`（mig 0026・SeriesSlot OneToOne）＋`expand_series_slots` の live 分岐で展開時に各回 LiveRundown/LiveCue へ複製。read API `rundown-template`＋CRUD `template_cue_*`（`_validate_cue`/`_apply_auto_fields` を live と共有）＋studio 共有 `RundownEditor`/`RundownTemplatePage`。
  - **D1**: auto_fire **壁時計アンカー**（§11 ドリフト緩和・確定判断＝壁時計方式）。`LiveCue`/`…TemplateCue` に `auto_anchor`(start|wallclock)＋`auto_wall_time`（mig 0027）。`resolver._auto_fire_at` が壁時計時は番組日の固定時刻に発火（開始ズレ非依存）。
  - **D3**: **CM grid 動的充填**（§11）。生 CM cue は `CmBundle` 事前割付を**任意化**し、未割付なら発火時に `select_free_cms`（`fill_break` のフリー割付を AdBreak 非依存に切り出し）で在庫充填。`fire_cm_dynamic`/`_fire_cm_clips`。
  - **D4**: **CM 入りチャイム**（§5.3）。`_fire_cm_clips` が `fire_chime(category="cm_in")` を併発。`ChimeCategory.CM_IN` を追加し studio で音源割付可（未設定なら no-op＝任意）。

---

## 11. 未決事項・リスク（Phase 3 で解決済みを追記）

- ~~**auto_fire のドリフト**（§6）~~ → **解決（Phase 3 D1・壁時計アンカー）**。cue 単位で `auto_anchor=wallclock`＋`auto_wall_time` を選ぶと番組日の固定時刻に発火し開始ズレ非依存。既定は従来の `start`（開始相対 offset）。「前セクション終了からの相対」は resolver が実際の終了時刻を持たないため未採用（将来検討）。
- ~~**VT 送出の返し方式**~~ → **確定（Phase 1）**。新規 `PLAY_VT`＋agent の `_return_rtmp_step`（`PLAY_CM_BUNDLE` reel 流用を拒否しクリーン実装）。
- ~~**CM 実素材の割付**（grid 動的）~~ → **解決（Phase 3 D3）**。`CmBundle` 事前割付は任意化し、未割付 CM cue は発火時に `select_free_cms` で grid 充填。
- **録画中オンエア時の残 CM**: 未発火 `PLAY_CM` からの投影（Phase 0 で実装済み・`AdBreakItem` の aired 状態追加は不要と判断）。
- ~~**チャイム音源**~~ → **配線済み（Phase 3 D4）**。`ChimeCategory.CM_IN` を追加し studio でカテゴリ「CM入り」に音源を割り付けられる。未割付なら `fire_chime` は no-op（無音）＝任意。node への音源投入＋studio でのカテゴリ選択が運用側の有効化手順（[project_breaking_chime] 同様）。
