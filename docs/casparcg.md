# #4 CasparCG AMCP コマンド設計

本書は ICS-TV の送出ノード（自宅 Proxmox LXC）で稼働する CasparCG Server（2.x 系, Linux headless）を、playout agent が AMCP（TCP 5250）で制御するためのコマンド設計を規定する。[scheduler.md](scheduler.md) のスケジューラが解決する `playout_event`（as-run）の各 `action` を、agent の 3 プリミティブ `amcp_loadbg(ev)`（背面ロード）/ `amcp_take(ev)`（本線へテイク）/ `goto_slate(channel)`（緊急退避）へ落とし込み、`LOADBG`（PREROLL=5s 先読み）→ `PLAY`（`scheduled_at` でテイク）という継ぎ目のない切替モデルに統一する。データモデルは [datamodel.md](datamodel.md)、送出イベントの解決・割り込み優先度・agent 実行ループは [scheduler.md](scheduler.md)、YouTube 枠連携（Data API 経由の `yt_transition`、本書対象外）は [youtube.md](youtube.md)、送出全体のアーキテクチャは [overview.md](overview.md) を参照する。本書に記載する AMCP コマンドはすべて CasparCG 2.x 系で実在するものに限定し、確証の持てない書式・パラメータは末尾「未確定・論点」に退避する。

## 1. レイヤ / チャンネル アーキテクチャ

原則として **1 出力 channel = 1 配信 ch**（[datamodel.md](datamodel.md) の `channel` テーブル 1 行に対応）。送出は CasparCG channel の出力 consumer（localhost UDP、mpegts intra）から、サイドカー `icstv-encoder@<slug>`（h264_vaapi）が **YouTube ingest**（`rtmp://a.rtmp.youtube.com/live2/<key>`）とローカル MediaMTX（自前 HLS 配信元）へ `tee` で二重送出する 1 系統（実体は §6.4、`deploy/playout-node/systemd/icstv-encoder@.service` が正本）。**Cloudflare Live Input は本線経路には無い**。CF Live Input/Output オブジェクト自体は studio の手動操作（`core/admin_views.py`）と fanclub simulcast（`fanclub/tasks.py`）向けに残るのみで、通常運用の送出とは無関係。Phase 1 は 1ch で全機能を検証し（[overview.md](overview.md) 意思決定ログ #11）、その後に最大 4ch へ複製する構想だったが、**現行の本番構成は ch1 単独**（2026-08-18、送出ノードの GPU/CPU 負荷削減のため casparcg.config から ch2 以降の `<channel>` 宣言を削除。§6.6）。本節以降は ch=1 を例に `<channel>-<layer>` 表記で記述し、マルチ ch 復活時は channel 番号を読み替える。

### 1.1 層割当（layer map）

CasparCG は **数値の大きい layer ほど前面**で合成される。本システムは下表の固定割当を全 ch 共通の規約とし、agent のコマンド生成を channel 番号の差し替えだけで済ませる。背景（本編 / CM / フィラー / 生入力）は相互排他で同一の「本線レイヤ」を共有し、`LOADBG`→`PLAY` のテイクで継ぎ目なく差し替える。CG（テロップ / Lバー / バンパー / 提供）は本線の上に別レイヤとして重ね、本線の差し替え中も途切れないようにする。

| layer | 役割 | producer 種別 | 備考 |
|-------|------|---------------|------|
| `N-10` | **本線（PGM, 背景）**: 録画本編 / CM / フィラー / 生入力 | FFmpeg producer（素材 / MediaMTX）| 相互排他。`LOADBG`→`PLAY` でテイク。フィラーは `LOOP` |
| `N-11` | **生退避**: CM IN 中に生 producer を止めず保持 | FFmpeg producer（MediaMTX）| 通常は空。CM IN 中のみ使用（§4） |
| `N-20` | **CMバンパー**（CM IN/OUT のつなぎ）| HTML テンプレ(CEF) | 本編⇔CM 境界の短尺。常駐せずイベント時のみ |
| `N-30` | **Lバー / 番組情報テロップ** | HTML テンプレ(CEF) | `playout_event` の番組メタを `CG ... UPDATE` で動的更新 |
| `N-35` | **次番組予告** | HTML テンプレ(CEF) | フィラー常時 / 番組残り3分〜終了。OverlayManager が管理 ([cg-layers.md](cg-layers.md) #18) |
| `N-36` | **朝・夕の時計**（左上 daypart 時計）| HTML テンプレ(CEF, `clock/corner`) | 朝夕の時間帯のみ。resolver が `clock_windows` を `cg_cues` に合成し OverlayManager が show/hide ([cg-layers.md](cg-layers.md)) |
| `N-37` | **津波ミニマップ**（地図+凡例のみ・画面右下・常時表示） | HTML テンプレ(CEF, `map/tsunami-corner`) | 地震サブシステム（別リポ icstv-earthquake）/ studio 手動が内部 API `POST /api/v1/internal/hazard-map`（`kind=tsunami_corner`）で発火。`N-38` と独立に同時表示可（`agent/icstv_agent/amcp_planner.py` `LAYER_HAZARD_CORNER=37`） |
| `N-38` | **災害フルスクリーン地図**（津波沿岸 / 震度 / 震度ズーム / 地方別自動選択 / EEW 横書き・縦書き。排他） | HTML テンプレ(CEF, `map/tsunami`\|`seismic`\|`seismic-zoom`\|`seismic-regional`\|`eew-panel`\|`eew-band`) | 同上の内部 API（`kind=tsunami\|seismic\|…`）で発火。`40`（速報テロップ）の下＝速報テロップが前面（`LAYER_HAZARD_MAP=38`） |
| `N-40` | **速報・割り込みテロップ** | HTML テンプレ(CEF) | 任意タイミングの差し込み。全局面で最前面の CG |
| `N-41` | **速報チャイム**（音声） | FFmpeg producer（音声クリップ）| 速報テロップと同時に鳴らす効果音。`PLAY`/`STOP` のみ（映像は出さない）。§3.6 |
| `N-45` | **手動フリーグラフィック**（ベース画像/動画＋文字を複数枚組める） | HTML テンプレ(CEF, 複数要素) | 手動 op（[cg-layers.md](cg-layers.md) #18 §B.3） |
| `N-50` | **提供表示**（スポンサークレジット）| HTML テンプレ(CEF) | 番組頭・尻のクレジット位置 |
| `N-90` | **緊急スレート** | FFmpeg producer or HTML | 最前面・最優先。`goto_slate(channel)` の到達先 |

設計意図:

- 本線を 1 レイヤ（`N-10`）に集約することで、録画→CM→生→フィラーの遷移が「同一レイヤ上の `LOADBG`/`PLAY`」に統一され、any→any の遷移が単一レイヤの差し替えで完結する。これが [scheduler.md](scheduler.md) の `amcp_loadbg(ev)` / `amcp_take(ev)` プリミティブと素直に対応する。
- **CG 系（20–50）を独立した video layer に分ける**のは、HTML producer が cg_layer（テンプレの内部多重）をサポートせず 1 video layer = 1 HTML テンプレに限られるためである（後述 §3）。同時表示が要る Lバー / テロップ / バンパー / 提供をそれぞれ別 video layer に割り当てる本規約は、この制約から必須となる。
- 生退避（`N-11`）を分けるのは、RTMP の再ハンドシェイクコストが高いため CM IN 中も生 producer を止めずに保持するためである（§4）。
- スレート（`N-90`）を最前面に置き、本線で何が再生中でも上から被せて即時退避できる（feed 断・送出異常時）。

レイヤ番号は連番ではなく 10 刻みを基本とし、将来の差し込み（二段テロップ、ウォーターマーク等）に余地を残す。レイヤ番号は本システムの運用規約であり AMCP 上の意味づけではない。**レイヤ未指定時の AMCP 既定はレイヤ 0** であるため、本システムでは常に明示指定する。

### 1.2 AMCP トークン順序の前提

CasparCG 2.x の `LOADBG` / `PLAY` は、clip 引数の後ろに以下の順でオプションを取る（いずれも省略可。本書で使う範囲のみ抜粋）。

```
LOADBG [ch]-[layer] [clip] {LOOP} {[transition:CUT|MIX|PUSH|WIPE|SLIDE] [duration:frames] {tween} {direction:LEFT|RIGHT}} {SEEK [frames]} {LENGTH [frames]} {AUTO}
PLAY   [ch]-[layer] {[clip]} {LOOP} {[transition] [duration:frames] {tween} {direction}}
```

- `LOOP` は **clip の直後**に置く独立トークン（`MY_FILE LOOP MIX 12` の順）。transition の後ろではない。
- transition の `duration` は **フレーム単位**（channel のフレームレート依存。30fps なら `MIX 15` ≒ 0.5s、25fps なら `MIX 25` = 1s）。
- `CUT` は独立コマンドではなく、`LOADBG`/`PLAY` の transition 位置に取り得るトークンの一つである。`CUT 1-10` のような単独コマンドは存在せず `400` になる。トランジション無し（瞬時切替）は transition を省略するのが既定で、明示したい場合のみ transition 位置に `CUT` を置く。
- `AUTO`（`LOADBG` のみ）は「現フォアグラウンド終了時に背面プロデューサを **1 段だけ** 自動テイク」する seamless join 用トークン。**同一 layer に複数 `LOADBG` を積んでも背面スロットは毎回上書きされる**ため、3 段以上を一括キューする鎖状予約はできない（AMCP 仕様）。
- `PLAY` を **引数なし**で発行すると、直前に `LOADBG` した背面を prepared transition 付きでテイクする（`SEEK`/`LENGTH` は背面プロデューサに保持されるため再ロード不要）。

### 1.3 合成順序・MIXER

合成は layer 昇順（10→90）で、上のレイヤが下を覆う。各レイヤの位置 / 不透明度は `MIXER` で制御する。本線は全画面・不透明（既定）。Lバー併用で本編を縮小・オフセットする「窓表示」をする場合は本線を `FILL` で縮め、空いた領域に Lバー（`N-30`）を敷く:

```
MIXER 1-10 FILL 0.0 0.0 0.82 0.82 12 easeinoutsine
MIXER 1-30 FILL 0.0 0.82 1.0 0.18
```

> `FILL [x] [y] [x-scale] [y-scale] {[duration] [tween]}` は正規化座標（0.0–1.0）。引数順は value（x y x-scale y-scale）→ duration → tween で、duration はフレーム数（AMCP 2.x）。上記は本編を 82% に縮め下 18% 帯に Lバーを配置する例。全画面へ戻すときは `MIXER 1-10 FILL 0 0 1 1 12 easeinoutsine`。

テロップ / バンパー / 提供のフェードは `OPACITY` で行う:

```
MIXER 1-40 OPACITY 0.0
MIXER 1-40 OPACITY 1.0 12 linear
```

> `OPACITY [opacity] {[duration] [tween]}` も value→duration→tween の順。tween 名（`linear` / `easeinoutsine` / `easeoutquad` / `easeinquad` 等）は CasparCG がサポートする小文字表記。

レイヤの変形をまとめて初期化するときは `MIXER 1-10 CLEAR`（当該レイヤの全 transform をクリア）、channel 全体なら `MIXER 1 CLEAR`。`KEYER` は本構成では既定で不使用（HTML テンプレはアルファ付きで合成されるため別レイヤをキー源にする必要がない）。クロマキー等が必要になった場合のみ、キー源レイヤ n に対し `MIXER 1-<n+1> KEYER 1` を検討する（KEYER は「layer n を layer n+1 のアルファ源に使う」動作）。

## 2. action → AMCP コマンドマッピング

[scheduler.md](scheduler.md) の `playout_event` 各 `action` を、本線レイヤ `1-10` 上の `LOADBG`（背面 / PREROLL 内）→ `PLAY`（`scheduled_at` でテイク）に落とし込む。layer 番号は §1.1 の割当を前提とする。

### 2.1 in/out とフレーム換算

`SEEK` / `LENGTH` は **フレーム単位**で、`SEEK` は開始フレーム、`LENGTH` は再生する**フレーム数（区間長）**を表す（[datamodel.md](datamodel.md) の `duration_ms` はミリ秒）。`out_ms` は終端であり長さではないため、`LENGTH` には区間長を渡す。agent は素材の `fps` を用いて変換する。

```text
seek_frames   = round(in_ms  * fps / 1000)
length_frames = round((out_ms - in_ms) * fps / 1000)
```

> `SEEK`/`LENGTH`/`LOOP` は **video file 入力にのみ**有効で、stream / device 入力（生）には付さない。正規化パイプライン（[overview.md](overview.md) 3.2）で全素材の `fps` を固定する前提のため、可変フレームレートによる丸め誤差は発生しない想定。

### 2.2 play_asset（録画本編セグメント）

`params: in_ms, out_ms`。素材の一部区間 `[in_ms, out_ms)` を本線に載せる。ファイル参照は CasparCG メディアフォルダ配下のローカルキャッシュ（[overview.md](overview.md) 3.2 の prefetch、拡張子は省略可）。

**LOADBG 時（PREROLL 内に先読み）** — 区間を `SEEK`/`LENGTH` で確定して背面ロード:

```
LOADBG 1-10 "asset/<asset_id>" SEEK <seek_frames> LENGTH <length_frames>
```

**TAKE 時（scheduled_at で発火）** — 背面のクリップを本線へ。継ぎ目を作らないため瞬時切替（transition 省略）:

```
PLAY 1-10
```

> 録画→CM など映像が切り替わる箇所はハードカットを基本とする。番組頭など演出上フェードしたい場合のみ `LOADBG ... MIX <frames>` を付し、テイクは同じく引数なし `PLAY 1-10`。

> **フィラー↔番組境界の黒フェード（dip to black）**: フィラーと実編成を差し替える瞬間のハードカットは視聴体験上やや唐突なため、agent はフィラー境界（フィラー↔非フィラーを跨ぐ切替）に限り本線を黒+無音へ落としてからテイクする。`MIX` のクロスフェードではなく本線レイヤ自体の MIXER で行う: フェードアウト `MIXER 1-10 OPACITY 0.0 <f> linear` / `MIXER 1-10 VOLUME 0.0 <f> linear` を発火し、黒に落ちきってから `PLAY 1-10` でテイク、続けて `MIXER 1-10 OPACITY 1.0 <f> linear` / `VOLUME 1.0 <f> linear` で番組をフェードインする（MIXER transform は producer 入替で保持されるため、この順序で「黒落とし→差し替え→立ち上げ」になる）。番組同士・CM のハードカット既定は維持。片側のフェード長は `ICSTV_TRANSITION_MS`（既定 500ms、`0` でハードカットに戻す）、`<f>` は fps 連動のフレーム数。outgoing 判定は agent の直近 executed 本線イベント（`current_main_event`）。実装は `agent/icstv_agent/main.py` の `_do_take`/`_should_dip` と `amcp_planner.main_dip_cmds`。

CM 枠を跨いだ本編の後半は **同じファイルの別区間**を新たに `SEEK`/`LENGTH` で開く。同一ファイルでも「別途 SEEK して開き直したプロデューサ」として扱うのが確実で、CM 挿入位置が編成変更されても各セグメントが独立に解決できる（[scheduler.md](scheduler.md) の `emit_recorded`）。

```
LOADBG 1-10 "asset/<asset_id>" SEEK <resume_seek_frames> LENGTH <resume_length_frames>
PLAY   1-10
```

### 2.3 play_cm（CM 1 本: 15s / 20s）

`params: advertiser`。`asset_id` が CM クリエイティブを指す。通常は全尺再生のため `SEEK`/`LENGTH` を省く（区間指定が必要なら同様に付与）。手順は `play_asset` と同形。

```
LOADBG 1-10 "cm/<asset_id>"
PLAY   1-10
```

> CM 間 / 本編⇔CM の境界はハードカットを既定とする。`cm_creative.aired_count` の増分はテイク確定後の as-run 返送時（[scheduler.md](scheduler.md)）に行い、AMCP 側に副作用は持たせない。

### 2.4 play_cm_bundle（生番組の事前バンドル CM リール / CM IN）

`cm_bundle`（[datamodel.md](datamodel.md)）の `cm_bundle_item` を `seq` 順に**連続再生**する。CM IN は任意タイミングの割り込みで、リール内は途切れさせない。本線 `1-10` 上で先頭をテイクし、残りを順送りする。

**重要**: 1 レイヤにつき AUTO で自動キューできるクリップは 1 本のみであり（§1.2）、bundle の数珠つなぎを `LOADBG ... AUTO` の多段スタックで実装してはならない。実装は次のいずれかとする。

- **(a) 逐次テイク（既定・確実）**: 各 CM は `LENGTH`（残尺）が既知なので、agent が CM 終了時刻を算出（または OSC で監視）して次の `LOADBG`→`PLAY` を逐次発行する（壁時計 / 残尺キュー）。
- **(b) AUTO 一段予約（最適化）**: 前面再生中に直後の 1 本だけを `AUTO` 予約し、AUTO 発火後に次の 1 本を改めて予約する。常に「現前面の直後 1 本」のみ。

逐次テイク案 (a) の例（先頭は生入力からの切替のため演出上 `MIX` を付すことが多い）:

```
LOADBG 1-10 "cm/<item[0].asset_id>" MIX 15
PLAY   1-10
LOADBG 1-10 "cm/<item[1].asset_id>"
PLAY   1-10
LOADBG 1-10 "cm/<item[2].asset_id>"
PLAY   1-10
```

AUTO 一段予約案 (b) の例:

```
LOADBG 1-10 "cm/<item[0].asset_id>" MIX 15
PLAY   1-10
LOADBG 1-10 "cm/<item[1].asset_id>" AUTO   # item[0] 終了で item[1] が自動前面化
# item[1] 再生に入ったら、その時点で item[2] を AUTO 予約する … 以下反復
```

**戻り（生入力へ復帰）**: リール末尾の終了時に `cut_live` 相当のテイクを発火する（§2.5、§4）。末尾の「戻り」を AUTO で自動化する場合は、最後の CM を再生中に **1 段だけ** `LOADBG 1-10 "rtmp://.../cam1" AUTO` を積めば、その CM 終了で自動テイクされる。bundle の 1 プロデューサ化（連結クリップ / playlist 化）と逐次テイクの優劣・冪等性は [scheduler.md](scheduler.md) の割り込み設計と突き合わせて確定する。

### 2.5 cut_live（生入力へ切替）

`params: cm_bundle_id, live_source`。生入力は MediaMTX のローカル RTMP を FFmpeg producer で参照する（[overview.md](overview.md) 3.5）。詳細な切替・退避・復帰・feed 断は §4 で扱う。基本形:

```
LOADBG 1-10 "rtmp://127.0.0.1:1935/<rtmp_app>/<rtmp_key>" MIX 15
PLAY   1-10
```

- `live_source.rtmp_app` / `rtmp_key`（[datamodel.md](datamodel.md)）から URI を組み立て、空白・特殊文字対策として引用符で囲う。`LOOP` / `SEEK` などの AMCP トークンは引用符の外（URL の後ろ）に置く。
- 生はストリームのため `LOOP`/`SEEK`/`LENGTH` を付さない。離脱は次イベント（フィラー / 次番組）の `PLAY 1-10` が担う（[scheduler.md](scheduler.md) の `emit_live`）。
- `cm_bundle_id` は CM IN 時に流すリールの参照で、`cut_live` 時点では使わない（CM IN 発火時に `play_cm_bundle` として実行）。
- **生はプリロードが効きにくい**: stream producer は接続確立に時間を要するため、生用に長めのプリロールを取り `INFO` で背面を確認してからテイクする（§4）。

### 2.6 play_filler（隙間フィラーをループ）

`params: filler_playlist_id, loop=true, until`。次イベント（`until` = 次番組 `start_at`）までフィラーを繰り返す。フィラーは**ファイル**なので `LOOP` が使える。単一ループ素材を既定・推奨とする:

```
LOADBG 1-10 "filler/<asset_id>" LOOP
PLAY   1-10
```

- ループ停止＝次イベントのテイク。`until` 到来時に次の `play_asset` / `cut_live` 等を `PLAY 1-10` でテイクすれば、フィラーは前面置換で自然に終わる（明示停止は不要）。
- 複数アイテムのプレイリストを途切れず周回したい場合、同一 layer への多段 `LOADBG` は背面を上書きし、AUTO は一段限定で「末尾→先頭」の無限自己ループも組めない。複数素材を周回させたいときは各アイテム終了直前に agent が逐次 `LOADBG`→テイクするか、フィラーを単一ループ素材にして `LOOP` 一発を既定とする。`LOOP` と `AUTO` の併記は LOOP 素材が終端に達せず AUTO が発火しないため矛盾するので行わない。

**実番組による中断からの再開**（[scheduler.md](scheduler.md) 「実番組による中断からの再開」参照）: `params: in_ms, out_ms` が付くのは、実番組（生放送・休止明け窓オープン）にクリップ途中で中断されたフィラーを、中断位置から残り尺だけ再開する場合のみ。通常のフィラーは `in_ms` を持たない。

```
LOADBG 1-10 "filler/<asset_id>" LOOP SEEK <seek_frames> LENGTH <length_frames>
PLAY   1-10
```

- `seek_frames`/`length_frames` の換算は §2.1 と同じ（`out_ms` はクリップの自然尺、`LENGTH` には `out_ms - in_ms` の区間長を渡す）。`LOOP` は安全策として維持する（境界ジッタで次イベントがわずかに遅れても黒落ちしない）が、`until` 到来時の次イベントテイクで置換されるため実質的にはループしない（通常フィラーと同じ設計）。
- `LOOP`/`SEEK`/`LENGTH` の同時指定は AMCP 文法上有効（`LOADBG` の `{LOOP}` は transition の前、`{SEEK}`/`{LENGTH}` はその後というトークン順に従う）だが、実機での挙動確認は推奨（§9 末尾参照）。

### 2.7 play_slate（緊急スレート即時 CUT）

最優先割り込み（[scheduler.md](scheduler.md) の優先度 SLATE ＞ 生 cut ＞ CM IN ＞ 通常）。本線の状態に依存せず**即時に最前面で出す**。独立レイヤ `1-90` を使い、本線 `1-10` の異常（生の feed 断、producer エラー等）に左右されないようにする。`goto_slate(channel)` プリミティブの実体は §5.4 で定義する。

```
PLAY 1-90 "slate/<slate_asset_id>" LOOP
```

- clip 付き `PLAY` は「まず背面ロードし続けて即前面化」を 1 コマンドで行うため、事前 `LOADBG` 無しの即応に適する。スレートは静止 / ループ素材を想定し `LOOP` を付す。
- **復帰**: 本線が回復したら本線側で正規イベントをテイクし、スレート層を撤去する。

```
CLEAR 1-90
```

> スレート時の音声方針は明示する。スレート音声を出し本線を黙らせるなら `MIXER 1-10 VOLUME 0`（復帰時 `MIXER 1-10 VOLUME 1`）を併用する。master audio の扱いは [overview.md](overview.md) / [scheduler.md](scheduler.md) と整合させる。

### 2.8 まとめ（action × プリミティブ対応表）

transition の duration はフレーム単位（fps 連動）。本線テイクは引数なし `PLAY 1-10`（背面のテイク）に統一し、演出上のフェードが要る境界のみ `LOADBG ... MIX <frames>` を付す。

| action | LOADBG（背面 / PREROLL 内）| TAKE（scheduled_at で発火）|
|--------|------------------------------|----------------------------|
| `play_asset` | `LOADBG 1-10 "asset/<id>" SEEK <f> LENGTH <f>` | `PLAY 1-10` |
| `play_cm` | `LOADBG 1-10 "cm/<id>"` | `PLAY 1-10` |
| `play_cm_bundle` | 各 item を逐次 `LOADBG`（AUTO 使用時も背面は一段のみ）| 先頭 `PLAY 1-10`（MIX 可）→ 以降 item 毎にテイク |
| `cut_live` | `LOADBG 1-10 "rtmp://…/<app>/<key>" MIX <f>`（LOOP/SEEK/LENGTH 不可）| `PLAY 1-10` |
| `play_filler` | `LOADBG 1-10 "filler/<id>" LOOP`（単一ループ素材が既定。実番組中断からの再開時のみ `LOOP SEEK <f> LENGTH <f>`）| `PLAY 1-10` |
| `play_slate` | （省略・即応）| `PLAY 1-90 "slate/<id>" LOOP`（最優先 CUT）/ 復帰時 `CLEAR 1-90` |

## 3. CG（テロップ / Lバー / CMバンパー / 提供）

テロップ / Lバー / CMバンパー / 提供表示は CEF の HTML テンプレートで、番組メタを動的注入して合成する。これらの CG は本線（`1-10`）と別の video layer に置き、本線を `LOADBG`/`PLAY`/`STOP` で差し替えても巻き込まないようにする。

### 3.1 HTML テンプレと video layer の対応

**HTML producer は cg_layer（テンプレの内部多重）をサポートせず、`cg_layer` 引数は常に 0 を指定する**（cg_layer は Flash producer 専用の概念）。したがって同時表示が必要な CG は §1.1 の規約に従い、それぞれ別の video layer に常駐させる（1 video layer = 1 HTML テンプレ）。

| video layer | 役割 | 主な局面 |
|------------|------|---------|
| `1-20` | CMバンパー（「ここからお知らせ」等のブリッジ CG）| CM IN / CM OUT |
| `1-30` | Lバー / 番組情報テロップ（番組名・出演者・コーナー名・時刻・ロゴ）| 録画 / 生で常時 |
| `1-36` | 朝・夕の時計（左上 daypart 時計。`clock/corner`）| 朝夕の時間帯のみ（`channel.clock_windows`）|
| `1-37` | 津波ミニマップ（`map/tsunami-corner`）| 地震サブシステム / 手動発火時（常時表示・§1.1）|
| `1-38` | 災害フルスクリーン地図（`map/tsunami`等）| 同上（`1-37` と排他ではなく独立）|
| `1-40` | 速報・割り込みテロップ | 全局面で最前面（緊急・運用割り込み）|
| `1-45` | 手動フリーグラフィック（`graphic/freeform` 等）| 手動 op 時のみ |
| `1-50` | 提供表示（スポンサークレジット）| 番組頭・尻のクレジット位置 |

### 3.2 CG コマンドの使い分け

検証済みの AMCP 2.x CG コマンド（`CG` の後に channel-layer 指定、続いて動詞）。いずれも HTML テンプレでは cg_layer に 0 を指定する。

```
CG [ch]-[layer] ADD    [cg_layer] [template] [play-on-load:0,1] {[data]}
CG [ch]-[layer] PLAY   [cg_layer]
CG [ch]-[layer] STOP   [cg_layer]
CG [ch]-[layer] NEXT   [cg_layer]
CG [ch]-[layer] UPDATE [cg_layer] [data]
CG [ch]-[layer] INVOKE [cg_layer] [method]
CG [ch]-[layer] REMOVE [cg_layer]
CG [ch]-[layer] CLEAR
CG [ch]-[layer] INFO   {[cg_layer]}
```

- **ADD**: テンプレートをロードする。パラメータ順は `ADD [cg_layer] [template] [play-on-load] {data}` で、`play-on-load` フラグはテンプレート名の **後** に置く（順序を誤らないこと）。`play-on-load=1` ならロード直後に `play()` が走る。常設物（Lバー・提供）は番組頭で一度 `ADD` し、以後は `UPDATE` で内容だけ差し替えて再ロードのちらつきを避ける。
- **PLAY**: テンプレートのイン・アニメーション（JS の `play()`）をテイクする。`ADD ... 0`（play-on-load=0）で背面準備し、見せたい瞬間に `PLAY` で出す。
- **UPDATE**: 新データを送る（JS の `update(data)` を呼ぶ）。番組情報の動的注入の中核。ロードや出し直しを伴わず表示文字列だけ差し替える。
- **INVOKE**: テンプレートのカスタムメソッドを名指しで呼ぶ（引数なし・戻り値 void）。多段アニメーションの局面切替に用いる。
- **STOP**: アウト・アニメーション（JS の `stop()`）を経て消す。**NEXT**: テンプレート内の `next()`（多段送り）。**REMOVE**: アウト・アニメーションを伴わず即時撤去。
- **CLEAR**: その video layer 上の全 cg_layer を一括撤去する（cg_layer を取らない）。`CG 1-30 CLEAR` は当該 video layer の CG のみを掃く。本線も含めた全消去は CG ではなく基本コマンドの `CLEAR 1`（channel 全レイヤ）であり、混同しない。
- **INFO**: cg_layer の状態取得。ウォッチドッグの生存確認に用いる（§6）。

### 3.3 番組データの JSON 動的注入

データは JSON に統一する。AMCP は単一引用符を文字列として認識しないため、データはダブルクオートで囲み、内部のダブルクオートをエスケープする。CasparCG コアは data 文字列をパースせずテンプレートへそのまま渡す（`JSON.parse` はテンプレート側 JS の責任）。

```
CG 1-30 ADD 0 "telop/lower-third" 0 "{\"title\":\"夜のニュース\",\"sub\":\"特集 地域経済\"}"
CG 1-30 PLAY 0
CG 1-30 UPDATE 0 "{\"title\":\"夜のニュース\",\"sub\":\"中継 駅前から\"}"
CG 1-30 STOP 0
```

テンプレート側（HTML+JS）は CasparCG が呼ぶグローバル関数 `play()` / `update(data)` / `next()` / `stop()` / `remove()` と任意名のカスタムメソッド（`INVOKE` 用）を実装する。agent は [datamodel.md](datamodel.md) の `program`（`title` / `description`）・`cm_creative`（`advertiser`）・`youtube_slot`（枠タイトル）等から JSON を組み立てて `UPDATE` で注入する。`playout_event.params`（jsonb）に CG 用補助（テンプレート名・差し込み文言）を載せておけば、agent は本線テイクと同じイベントから CG コマンドを派生できる。

### 3.4 局面別の出し分けロジック（録画 / 生 / CM）

[scheduler.md](scheduler.md) の状態機械（FILLER / PROGRAM_RECORDED / PROGRAM_LIVE / CM_BREAK / SLATE）に CG を対応づける。

**録画番組（PROGRAM_RECORDED）**: 番組頭で Lバーと（必要なら）提供表示を立て、本編セグメント先頭で番組名テロップを出す。本線素材のセグメント切替（[scheduler.md](scheduler.md) の `emit_recorded`）に CG を巻き込まないよう、CG は別 video layer に常駐させ `UPDATE` で内容だけ追従させる。

```
CG 1-30 ADD  0 "lbar/standard"     1 "{\"channel\":\"ICS-TV\",\"clock\":true}"
CG 1-50 ADD  0 "credit/sponsor"    0 "{\"sponsor\":\"…\"}"
```

**CM 枠（CM_BREAK; 録画枠の play_cm / 生の play_cm_bundle）**: CM 直前にバンパー CG（`1-20`）を出し、Lバー・テロップを退避する。CM 中は番組由来の CG を出さない。バンパーは番組頭で一度 `ADD 0 ... 0`（背面ロード）しておく。

```
CG 1-20 ADD  0 "bumper/cm-in" 0        # 番組頭で背面ロード
... (CM IN) ...
CG 1-20 PLAY 0                         # CMバンパー「ここからお知らせ」
CG 1-30 STOP 0                         # Lバー / 番組テロップ退避
... (本線 1-10 で CM を順次テイク) ...
CG 1-30 PLAY 0                         # CM OUT: Lバー復帰
CG 1-20 STOP 0                         # バンパーアウト
```

Lバーの「半透明退避」「位置移動」は CG ではなく当該 video layer の MIXER で行う（§1.3）。個々の表示要素単位の不透明度はテンプレート側 CSS で扱い、MIXER は video layer 全体に作用する点に留意する。

```
MIXER 1-30 OPACITY 0.0 12 easeoutquad     # Lバーを 12 フレームでフェードアウト退避
MIXER 1-30 OPACITY 1.0 12 easeinquad      # 復帰
```

**生番組（PROGRAM_LIVE）**: 本線が MediaMTX producer に切替わっても CG は独立に維持される。番組名・中継地点・出演者などの可変情報は運用 UI からの操作で `UPDATE` 注入する。CM IN では上記 CM 枠と同じ退避・復帰を行う。

**フィラー（FILLER）/ スレート（SLATE）**: フィラーループ中は最小限の Lバー（時刻・ロゴ）のみとし、番組テロップは出さない。緊急スレートへ遷移する際は CG を一掃してから本線をスレートに切替える。

```
CG 1-20 CLEAR
CG 1-30 CLEAR
CG 1-40 CLEAR
CG 1-50 CLEAR
... (本線 1-10 をスレートにテイク、もしくは 1-90 にスレートを被せる) ...
```

### 3.5 提供表示・速報の扱い

- **提供表示（`1-50`）**: 番組頭・尻のクレジット位置で `ADD`→`PLAY`、所定秒で `STOP`。スポンサー名は番組に紐づく出稿情報から JSON 注入する。録画番組では as-run 計画（[scheduler.md](scheduler.md)）の番組頭イベントに CG 派生を載せる。
- **速報・割り込みテロップ（`1-40`）**: 全局面で最前面。運用 UI から任意タイミングで `ADD`→`PLAY`、`UPDATE` で文言更新、`STOP` で撤去する。本線・CM の状態に依存せず独立に出せるよう最上位の CG レイヤに固定する。
- **速報チャイム（`1-41`・音声）**: 速報テロップに気づきやすくするための効果音。テロップ層（`1-40`）とは独立に、音声クリップを FFmpeg producer として `PLAY 1-41 <clip>` で鳴らし、数秒後に `STOP 1-41` で後始末する（映像は出さず、チャンネル音声ミキサ §1.3 が本線に重畳する）。

  **音源は「ライブラリ＋選択」の 2 層**で扱う（Web 管理）。`ChimeSound`（局共通の音源ライブラリ）に音声をアップロードして貯め、`ChannelChime`（per-channel）でカテゴリ（`eew`/`weather`/`general`）ごとに「ライブラリのどれを使うか」を選ぶ。音源は正規化せず R2 に **content-addressed**（`chime/lib/<uuid>.<ext>`）で保存し、clip 名（= 拡張子を除いた r2_key）が一意になるため、選択替え／差し替えで送出ノードの MediaCache が古い音を使い続ける事故（`ensure` は既存ファイルがあれば再DLしない）を避けられる。

  | 種別 | 解決順 |
  |------|--------|
  | 発火時その場選択（手動速報の `snd:<id>`） | `ChimeSound.clip` を最優先 |
  | カテゴリ既定（`eew`/`weather`/`general`） | `ChannelChime`→`ChimeSound.clip` |
  | フォールバック | `settings.CHIME_CLIPS`（既定 `sfx/<category>`・プレースホルダ用） |

  発火経路は `core.views.fire_chime`（速報テロップ `fire_breaking_telop` と手動 op `op_overlay` の双方が呼ぶ）。地震速報サブシステム（別リポ）は内部 API `/internal/breaking-telop` の `chime`（カテゴリ）で指定する。連打抑止（クールダウン）はサーバ側では行わず呼び出し側（地震リポの dedup / 手動運用）に委ねる。

  > **配備**: 音源は studio の「チャンネル詳細設定 › 速報チャイム」からアップロードする（R2 保存）。送出ノードへの配布は **standing prefetch manifest**（slate と同列）でライブラリ全体を agent が pin+DL するため、ノードへの手置きは不要。**緊急地震速報の NHK チャイムは著作権物のため流用不可** — 自作またはライセンス取得した独自音源を使う。

### 3.6 全画面 HTML producer との区別

CEF の使い分けには二系統あり、混同しない。

- **CG レイヤ（テンプレートホスト）**: `CG ... ADD/PLAY/UPDATE/...` で制御できる。データ注入と出し入れを AMCP から制御する CG はすべてこちら。
- **HTML producer**: video layer に直接ロードする全画面ページ。URL を渡す場合は `[HTML]` を前置して他 producer と区別する（テンプレートフォルダ配下の相対パスなら前置不要）。`CG` コマンドで制御できない（出して消すだけ）。アニメーション主体でデータ注入が不要な全画面バンパー映像などに限り採用しうる。可変文字を含むものは CG レイヤ側で実装する。

```
PLAY 1-20 [HTML] "http://127.0.0.1:8080/bumper/cm-in.html"   # データ注入不要の全画面バンパー（URL ロードは [HTML] 必須）
```

## 4. 生入力切替（live cut / CM IN・復帰 / feed 断退避）

送出ノード（自宅 Proxmox LXC）の MediaMTX でローカル終端した RTMP/SRT を CasparCG の FFmpeg producer で参照し、`cut_live` で本線へテイクする。外部（現場 OBS）からの到達経路は [overview.md](overview.md) 決定#21（Cloudflare One/WARP）を参照。データモデルは [datamodel.md](datamodel.md)（`live_source` / `cm_bundle` / `playout_event`）、割り込み優先度は [scheduler.md](scheduler.md) を参照。

### 4.1 生入力ソースの参照書式（MediaMTX ローカル RTMP）

FFmpeg producer は libavformat が解釈できる URL（`rtmp://`, `srt://`, `udp://` 等）を clip 引数として直接受ける。MediaMTX が同一ホスト（送出ノード = 自宅 Proxmox LXC 上）で終端しているため参照先は `localhost`。

```
LOADBG 1-10 "rtmp://127.0.0.1:1935/<rtmp_app>/<rtmp_key>"
```

> 入力バッファ深さ・低遅延化・自動再接続の制御は、CasparCG 2.x FFmpeg producer が解釈する名前付きパラメータ（`LOOP` / `SEEK` / `START` / `LENGTH` / `FILTER` / `AUTO`）の範囲を超える。`-fflags nobuffer` のような任意 ffmpeg 入力オプションを末尾に足してもパススルーされる保証はなく実機でエラーになる報告もあるため、本書の cut_live コマンド列は URL 直渡しに留め、低遅延・バッファ調整は MediaMTX 側で行うのを基本とする（§4.6、末尾の論点）。

### 4.2 生は LOADBG プリロードが効きにくい

録画素材（ファイル）は `LOADBG` 時点でデコーダが先頭フレームを掴み `PLAY` を即テイクにできる。一方ライブ入力は、`LOADBG` を発行しても最初の有効フレームが揃うのは RTMP ハンドシェイク＋GOP 境界待ち以降であり、プリロードの先読み効果（PREROLL=5s）が素材ほど効かない。そこで `cut_live` は `scheduled_at − PREROLL` よりさらに前倒し（生専用の長めプリロール）で背面ロードを始め、`INFO` で取得できる範囲の背面状態を確認してからテイクする。具体秒数は実機計測（[scheduler.md](scheduler.md) の PREROLL 論点）。

> `INFO 1-10` はレイヤの再生状態（playing/stopped）とフォーマット情報を返すが、「背面 producer が最初の有効フレームを供給し始めたか」を機械判定できるフィールド粒度は版依存で確実な保証がない。よって背面確認は INFO で取れる範囲に留め、生の有効化判定は MediaMTX 側 publisher 検知を一次とする（§4.5）。

### 4.3 cut_live のコマンド列

生番組開始（`playout_event.action = cut_live`）。生番組頭は基本ハードカット（transition 省略）。

```
# 1) 背面ロード（前倒し）
LOADBG 1-10 "rtmp://127.0.0.1:1935/<rtmp_app>/<rtmp_key>"
# 2) 背面の状態確認（取得できる範囲で。掴めなければ MediaMTX 側検知を主に再 LOADBG / feed 断扱い）
INFO 1-10
# 3) テイク（壁時計 scheduled_at 到来時）
PLAY 1-10
```

トランジション付きで載せたい場合は `LOADBG` 側に transition を prepared しておき引数なし `PLAY` でテイクする（`LOADBG 1-10 "rtmp://…" MIX 12` → `PLAY 1-10`）。

### 4.4 CM IN（バンドル再生）→ 生復帰

生放送中に運用 UI から「CM IN」を押すと、agent が生入力を退避させ、バンドル CM リール（`cm_bundle` / `cm_bundle_item`）を本線へ載せ、終了後に生入力へ戻す。実送出を `playout_event(action="play_cm_bundle", actual_at=now)` として as-run 記録する（[scheduler.md](scheduler.md) 割り込み節）。

設計上の要点は **生 producer を止めずに残す**こと。RTMP は再接続コストが高く、CM 中に `STOP` で破棄すると復帰時に再ハンドシェイクが要る。そこで生は退避レイヤ `1-11` へ移し、本線 `1-10` を CM に明け渡す。

```
# 1) 生入力を退避レイヤ 1-11 へロードしテイク（生を止めない。退避中はミュート推奨: MIXER 1-11 VOLUME 0）
LOADBG 1-11 "rtmp://127.0.0.1:1935/<rtmp_app>/<rtmp_key>"
PLAY   1-11
# 2) 本線 1-10 にバンドル CM リールを再生（§2.4 の逐次テイク or AUTO 一段予約）
LOADBG 1-10 "cm/<item[0].asset_id>" MIX 15
PLAY   1-10
LOADBG 1-10 "cm/<item[1].asset_id>"
PLAY   1-10
# … item 末尾まで …
```

**生復帰（戻り）**: バンドル最終 CM 終了の検知（残尺自動）または運用 UI の手動「戻り」で、本線を生へ戻す。退避中の生を本線として扱い直すより、本線 `1-10` に生を載せ直してカットするのが状態を単純化できる。

```
LOADBG 1-10 "rtmp://127.0.0.1:1935/<rtmp_app>/<rtmp_key>"
INFO   1-10
PLAY   1-10
STOP   1-11
```

> 残尺自動の「戻り」トリガは、CM 側（ファイル素材）の残フレームを `INFO 1-10` で監視して判定する（ファイルは尺が確定しているため残尺判定が可能）。**OSC 経由の監視は未実装**（§5.2 参照。casparcg.config に `<osc>` 設定が無く agent にも購読実装が無いため、現状は `INFO` ポーリングが唯一の経路）。

### 4.5 feed 断検知 → スレート自動退避

生入力の断（OBS 落ち、WAN 断、MediaMTX セッション切れ）を検知し、本線が黒画面化する前に緊急スレートへ退避する。検知は二系統を併用する。

- **検知 A（一次・推奨）**: MediaMTX のセッション / パス状態（publisher の有無）を API / メトリクスで監視し、publisher 消失を即座に feed 断とみなす（CasparCG 非依存で最速。[scheduler.md](scheduler.md) の「feed 断（生）= MediaMTX のフレーム監視で検知 → 即 SLATE」）。
- **検知 B（二次・保険）**: CasparCG の `INFO 1-10` で本線レイヤの状態を周期ポーリング、または OSC のフレーム系パスを購読し、フレーム供給停止で断と判定。版差が大きいため一次は MediaMTX 側に置く。

検知後、`goto_slate(channel)`（§5.4）が最前面スレートレイヤ `1-90` にスレートをループ再生で載せる。生を確実に捨てたい場合はフォアグラウンドのみ除去する `STOP` ではなく、背面も含めてレイヤを空にする `CLEAR 1-10` を用いる。

```
PLAY  1-90 "slate/<slate_asset_id>" LOOP
CLEAR 1-10                            # 復帰を諦める場合のみ、死んだ生を破棄
```

feed 回復時（MediaMTX に publisher 再出現）は運用ポリシー（自動 or 手動）に従い本線へ生を戻し、スレートを退かす。

```
LOADBG 1-10 "rtmp://127.0.0.1:1935/<rtmp_app>/<rtmp_key>"
INFO   1-10
PLAY   1-10
CLEAR  1-90
```

### 4.6 バッファ / 遅延 / 再接続の方針

- リニアチャンネルは絶対遅延より**継ぎ目の無さ**を優先するため、過度な低遅延化は避け、CM IN / 復帰の継ぎ目が安定する範囲でバッファを取る。CasparCG 2.x FFmpeg producer は任意の ffmpeg 入力オプションをパススルーしないため、バッファ調整は MediaMTX 側で行うのを基本とする。
- RTMP/SRT の瞬断は、まず MediaMTX 側で publisher セッションを終端・再受けする層で吸収し、CasparCG 側 producer の再接続挙動に依存しすぎない。CasparCG が producer を落とした場合は agent が `LOADBG`→`INFO`→`PLAY` で載せ直す（feed 断シーケンスと同経路）。
- 生はプリロードが効かない点を運用前提に織り込み、`cut_live` は常に長めのプリロールと `INFO` 確認を挟む。CM や録画（ファイル）は従来どおり PREROLL=5s のプリロードが有効。

## 5. agent AMCP プリミティブ実装

playout agent は [scheduler.md](scheduler.md) の `playout_event` を AMCP コマンド列へ展開し、TCP 5250 へ送出する。プリミティブは 3 つに集約される: `amcp_loadbg(ev)`（PREROLL=5s 前に背面ロード）/ `amcp_take(ev)`（`scheduled_at` でテイク）/ `goto_slate(channel)`（障害時の最後の砦）。冪等キー（`ev.idempotency_key`）で二重実行を防ぐ。

### 5.1 TCP トランスポートとコマンド送出

AMCP は TCP 5250 へ接続し、1 コマンド = 1 行を **CRLF（`\r\n`）終端**で送る。接続は常時維持し、コマンドごとに張り直さない。生存確認は標準コマンド `VERSION`（先頭 2xx を生存とみなす）を用いる（`PING` は版依存。後述のウォッチドッグでは `PING` も併用するが、agent の制御接続は `VERSION` を基本とする）。

応答ヘッダの先頭整数（ステータスコード）でクラス分けして処理する。公式 AMCP リファレンスで確認済みのコード定義は以下のとおり。

| コード | 区分 | 意味 | 後続データ | agent の扱い |
|--------|------|------|-----------|--------------|
| 200 | 成功 | 実行完了、複数行データが後続（INFO 等）| 複数行（空行まで）| 後続行を読み切る |
| 201 | 成功 | 実行完了、1 行データが後続（VERSION 等）| 1 行 | 次 1 行を読む |
| 202 | 成功 | データなし（PLAY/LOADBG/LOAD/STOP/CLEAR）| なし | ヘッダのみで完結 |
| 400 | クライアント誤り | コマンド解釈不能（コマンド名をエコーしない `400 ERROR`）| 1 行 | 構文バグ。リトライ不可、要修正 |
| 401 | クライアント誤り | 不正な video_channel | なし | 設定不整合。リトライ不可 |
| 402 | クライアント誤り | パラメータ欠落 | なし | 同上 |
| 403 | クライアント誤り | 不正なパラメータ | なし | 同上 |
| 404 | クライアント誤り | メディアファイル未検出 | なし | 素材未到達。スレート退避＋再スケジュール |
| 500 / 501 | サーバ誤り | 内部サーバエラー | なし | 一過性とみなし限定リトライ |
| 502 | サーバ誤り | メディアファイル読込不能 | なし | 破損 / 未完アップロード。404 同様に退避 |
| 503 | サーバ誤り | アクセスエラー | なし | 権限 / I-O。リトライ＋アラート |

PLAY / LOADBG / LOAD / STOP / CLEAR は成功時 `202 [command] OK`、`INFO` は `200 INFO OK`、`VERSION` は単行（200/201 系）を返す。成功時はヘッダが `[code] [command] OK` の形でコマンド名をエコーするが、`400 ERROR` のみエコーしない。実装はコマンド名のエコー有無に依存せず**先頭整数のみで分岐**させる。データ後続は「200=複数行」「201=1 行」「400=1 行」のみで、202/401-404/5xx はヘッダ行で完結する。

```
function send_amcp(conn, line):
    conn.write(line + "\r\n")
    header = conn.read_line()              # 例: "202 PLAY OK" / "404 PLAY ERROR" / "200 INFO OK" / "400 ERROR"
    code   = parse_leading_int(header)
    klass  = code // 100                   # 2=成功, 4=クライアント誤り, 5=サーバ誤り
    body   = []
    if code == 200:                        # 複数行データ（空行終端）
        while (l := conn.read_line()) != "":
            body.append(l)
    elif code == 201 or code == 400:       # 1 行データ
        body.append(conn.read_line())
    return AmcpResult(code=code, klass=klass, header=header, body=body)
```

接続維持: TCP KeepAlive を有効化し、無トラフィック区間で周期的に `VERSION` を送り、2xx 応答が所定時間内に返らなければ接続断とみなす。再接続はバックオフ（0.5s → 1s → 2s … 上限 30s）。再接続成立後は `VERSION` で世代を再確認し、OSC ステートストアを stale として一旦無効化する。断中に発生したイベントは store-and-forward キュー（[datamodel.md](datamodel.md)）に滞留させ、再接続後に `scheduled_at` 順で再評価し、放送時刻を過ぎたイベントは破棄してスレートへ退避する。

### 5.2 OSC（UDP）フィードバック購読

> **未実装（2026-09 時点）**: 本節は設計案として残すが、`deploy/playout-node/casparcg/casparcg.config` に `<osc>` 設定は無く、`agent/icstv_agent/` にも OSC 購読の実装は無い（CasparCG は誰にも OSC を push していない）。現行の残尺・固着判定は代わりに **AMCP `INFO` ポーリング**（agent の `_output_watchdog` / `caspar.parse_foreground`、送出ヘルス監視の `watchdog.sh` の `layer_state`）と **MediaMTX API・HLS セグメント鮮度**で行っている（§6.7）。OSC 購読を実装する場合は本節を実装の起点として使える。

CasparCG は v2.0.4 以降、再生位置・状態を **OSC over UDP** で一方向に push する（agent からの OSC は受け付けない）。送信先は `casparcg.config` の `<osc><predefined-clients>`（IP とポートを静的指定。ホスト名不可）で確定させる。AMCP 接続元 IP へ自動 push させる方式もあるが、NAT / 複数接続で宛先が揺れるため、静的宛先を推奨する（既定ポートは 6250）。

agent が購読する主なアドレスパターン（`[c]` = channel, `[l]` = layer）。バージョン / プロデューサによっては `foreground/` を挟む系統で配信されるため、起動時に実パケットでアドレス体系を確定させる。

```
/channel/[c]/stage/layer/[l]/paused                       # 当該レイヤが一時停止中か (bool)
/channel/[c]/stage/layer/[l]/profiler/time                # 実フレーム時間 | 期待フレーム時間 (2 引数, 描画遅延検知)
/channel/[c]/stage/layer/[l]/foreground/file/path         # 再生中ファイルのパス（2.3 系は foreground/ を挟む）
/channel/[c]/stage/layer/[l]/foreground/file/time         # 経過秒 / 全体秒（引数個数・単位は実機確定）
# 版によっては foreground/ 無しの /channel/[c]/stage/layer/[l]/file/time でも配信される
```

`file/time`（経過秒 / 全体秒）を `out_ms`（[scheduler.md](scheduler.md) の `play_asset` パラメータ）に対する残尺の主信号とする。OSC は UDP ゆえ欠落・順不同・重複しうる前提で「最後に受信した値」で上書きするステートストアに集約し、しきい値判定（残尺 ≤ PREROLL）にのみ用いる。**テイクの正否判定は OSC ではなく AMCP 応答コードを正とする**。

```
function on_osc_packet(pkt):
    (chan, layer, leaf, args) = parse_osc_address(pkt)   # foreground/ の有無を正規化して leaf を得る
    state = layer_state[(chan, layer)]
    switch leaf:
        case "file/time":     state.elapsed_s, state.total_s = args[0], args[1]
        case "file/path":     state.path  = args[0]
        case "paused":        state.paused = args[0]
        case "profiler/time": state.render_actual, state.render_expected = args[0], args[1]
    state.updated_at = now()
    if state.total_s and (state.total_s - state.elapsed_s) <= PREROLL_S:
        notify_preroll_window(chan, layer)               # 次イベントの LOADBG 起動を scheduler 連携層へ通知
```

### 5.3 amcp_loadbg(ev) / amcp_take(ev)

`amcp_loadbg(ev)` は `scheduled_at` の PREROLL 秒前に呼ばれ、背面へロードのみ行う（前面はまだ動かさない）。生差し込みとの整合のため `AUTO` は使わず、テイクは agent が明示制御する。冪等キー（phase 別）で二重ロードを防ぐ。action 別の `LOADBG` 文は §2 の各コマンド列に対応する。

```
function amcp_loadbg(ev):
    if idempotency.seen(ev.key, phase="loadbg"): return ACK_DUP
    c = channel_of(ev)
    switch ev.action:
        case "play_asset":
            seek   = ms_to_frames(ev.params.in_ms, ev.fps)
            length = ms_to_frames(ev.params.out_ms - ev.params.in_ms, ev.fps)
            cmd = f'LOADBG {c}-10 "asset/{ev.asset_id}" SEEK {seek} LENGTH {length}'
        case "play_cm" | "play_cm_bundle":
            cmd = f'LOADBG {c}-10 "cm/{ev.asset_id}"'
        case "play_filler":
            cmd = f'LOADBG {c}-10 "filler/{ev.params.filler_playlist_id}" LOOP'
        case "cut_live":
            cmd = f'LOADBG {c}-10 "rtmp://127.0.0.1:1935/{ev.live_source.rtmp_app}/{ev.live_source.rtmp_key}"'
        case "play_slate":
            return                                  # スレートは即応（goto_slate）。背面ロードしない
    r = send_amcp(conn, cmd)
    if r.klass == 2: idempotency.mark(ev.key, phase="loadbg"); return ACK
    if r.code in (404, 502): enqueue_slate_fallback(ev); return FAIL_MEDIA
    return retry_or_fail(ev, r)                      # 5xx は限定リトライ
```

`amcp_take(ev)` は `scheduled_at` 到来時に背面から前面へテイクする。引数なし `PLAY {c}-10` は直前に `LOADBG` した背面プロデューサ（`SEEK`/`LENGTH` 保持）をそのまま前面化する。テイク成立は AMCP 応答を正とし、以後の残尺監視は OSC へ委譲する。

```
function amcp_take(ev):
    if idempotency.seen(ev.key, phase="take"): return ACK_DUP
    c = channel_of(ev)
    switch ev.action:
        case "play_slate": r = send_amcp(conn, f'PLAY {c}-90 "slate/{SLATE_ASSET}" LOOP')
        default:           r = send_amcp(conn, f'PLAY {c}-10')   # cut_live も本線レイヤを引数なしでテイク
    if r.klass == 2: idempotency.mark(ev.key, phase="take"); return ACK
    return goto_slate(c)                             # テイク失敗は即スレート退避
```

> LOADBG を経ていない緊急テイク（store-and-forward 復帰直後など）では `PLAY {c}-10 "{clip}" SEEK {seek} LENGTH {length}` のようにクリップ指定 `PLAY` を直接用いる。クリップ引数付き `PLAY` は内部で LOADBG 相当の背面ロードを行ってから即時前面化する。

### 5.4 goto_slate(channel)

あらゆる失敗経路（テイク失敗、feed 断、ウォッチドッグ検知）の収束先。スレートレイヤ `N-90` を即時前面化し、主映像の異常を遮蔽する。冪等キーには依存させず、何度呼ばれても同一状態へ収束する。clip 直指定の `PLAY` により事前ロード不要で、`LOOP` でループ再生する。

```
function goto_slate(channel):
    r = send_amcp(conn, f'PLAY {channel}-90 "slate/{SLATE_ASSET}" LOOP')
    if r.klass != 2:
        send_amcp(conn, f'STOP {channel}-10')       # 最終手段: 主レイヤを停止して黒/設定済み背景へ
    alert("slate engaged", channel=channel)
    return ACK
```

### 5.5 プリミティブ × action 対応（生入力含む）

| プリミティブ | AMCP マッピング |
|--------------|-----------------|
| `amcp_loadbg(ev)`（play_asset）| `LOADBG c-10 "asset/<id>" SEEK <f> LENGTH <f>` |
| `amcp_loadbg(ev)`（play_cm / play_cm_bundle）| `LOADBG c-10 "cm/<id>"` |
| `amcp_loadbg(ev)`（play_filler）| `LOADBG c-10 "filler/<id>" LOOP`（実番組中断からの再開時は `LOOP SEEK <f> LENGTH <f>`）|
| `amcp_loadbg(ev)`（cut_live）| `LOADBG c-10 "rtmp://…"`（前倒し）＋ `INFO c-10` で背面確認 |
| `amcp_take(ev)`（通常 / cut_live）| `PLAY c-10`（引数なし＝背面テイク）|
| CM IN | `LOADBG c-11 "rtmp://…"` / `PLAY c-11`（生退避）→ 本線 `c-10` で CM を逐次テイク（§2.4）|
| 生復帰 | `LOADBG c-10 "rtmp://…"` → `INFO c-10` → `PLAY c-10` → `STOP c-11` |
| `goto_slate(channel)`（play_slate / 障害）| `PLAY c-90 "slate/<id>" LOOP`（＋必要時 `CLEAR c-10`）|

すべて `idempotency_key`（[scheduler.md](scheduler.md)）で二重実行を防ぐ。生の割り込み（CM IN・feed 断）は事前生成イベントではなく実行時割り込みのため、as-run は `actual_at=now` で記録する。

## 6. Linux headless 固有・運用

送出ノード（CasparCG Server を headless で常駐させる Linux ホスト = 自宅 Proxmox LXC）に固有の要件と、24/7 リニア配信を維持する運用上の留意点を扱う。記載する AMCP はすべて 2.x 系で実在するものに限定する。

> 本節（§6.1〜§6.5）は送出ノード（本番稼働中の Proxmox LXC）の実機構成を正として記述する。宣言ファイルの正本は
> `deploy/playout-node/casparcg/casparcg.config` と `deploy/playout-node/systemd/casparcg-server.service`（配備手順は
> [deploy/playout-node/README.md](../deploy/playout-node/README.md)）であり、本節はそれを実機の実測込みで解説する。
> 差異が出た場合は宣言ファイル側を正とする。

### 6.1 採用版とプラットフォーム前提（実機: 2.5.0 PPA / Intel UHD630 VAAPI）

実機は **CasparCG Server 2.5.0**（`ppa:casparcg/ppa`、Ubuntu 24.04、パッケージ `casparcg-server-2.5` / `casparcg-cef-142` / `casparcg-scanner`）を採用している。2.5.0 以降は EGL で SFML を置換しており、**X サーバ（Xorg）無しの完全 headless 起動**をサポートする（旧 2.3 系は Xorg 前提だったため 2.5.0 を選定した経緯がある）。GPU は **NVIDIA ではなく Intel UHD630（iGPU、Mesa/VAAPI、`/dev/dri/renderD128`）**。NVIDIA ドライバの導入・`h264_nvenc` は本構成では使わない（h264 エンコードはサイドカー `icstv-encoder` が VAAPI で行う。§6.4）。

起動には `LIBVA_DRIVER_NAME=iHD`（Intel iHD VAAPI ドライバ）を渡す（systemd unit の `Environment=`。§6.5）。導入手順は `deploy/playout-node/scripts/install.sh` を正本とする。

### 6.2 GL コンテキスト要件（CEF/EGL、実機: enable-gpu=false）

CEF（HTML テンプレ CG）の GPU モードは実機では **`<enable-gpu>false</enable-gpu>`**（`casparcg.config:40`）。理由は headless（セッション無し）環境で CEF の GPU サブプロセスが segfault するため（`casparcg.config:20`、`deploy/playout-node/README.md` の実証記録）。CEF は CPU（software）レンダリングで描画する。

CasparCG 本体の初期化には EGL が使われ、起動 env `EGL_PLATFORM=surfaceless`（これが無いと `eglChooseConfig` が失敗し起動不能）と `LC_ALL=C.UTF-8` / `LANG=C.UTF-8`（無いと locale エラーで abort）を渡す（§6.5）。この構成で Intel UHD630 上に「OpenGL 4.6 (Compatibility Profile) Mesa Intel」が初期化されることを実機で確認済み。

GL リソースの状態は AMCP から確認・回収できる。長時間運用時の GL リソース診断・手動解放に用いる。

```
GL INFO
GL GC
```

`GL INFO` は割り当て済み・プール済みの OpenGL リソース情報を返し、`GL GC` はプール済み GL リソースを解放する。

### 6.3 casparcg.config（実機構成: 1ch・UDP consumer・template パス）

実機（送出ノード）の `/etc/icstv/casparcg.config` は `deploy/playout-node/casparcg/casparcg.config` と byte 一致（sha256 照合済）。要点を抜粋する（正本はリポジトリのファイル自体を参照。コメントに実証事項が書かれているので削らないこと）:

```xml
<configuration>
  <log-level>info</log-level>

  <paths>
    <media-path>/opt/casparcg/media/</media-path>
    <log-path>/opt/casparcg/log/</log-path>
    <data-path>/opt/casparcg/data/</data-path>
    <template-path>/opt/casparcg/template/</template-path>
  </paths>

  <lock-clear-phrase>icstv</lock-clear-phrase>

  <html>
    <enable-gpu>false</enable-gpu>
  </html>

  <channels>
    <!-- channel 1 = ICS-TV ch1 (ICSTV_CASPAR_CHANNEL=1, UDP 5004, icstv-encoder@ch1) -->
    <channel>
      <!-- 720p60 既定 (ICSTV_FPS=60 と一致)。1080p30 にするなら 1080p3000 + ICSTV_FPS=30 -->
      <video-mode>720p6000</video-mode>
      <consumers>
        <!-- 送出は localhost UDP(mpegts intra) のみ。HW エンコードは icstv-encoder が担う。 -->
        <ffmpeg>
          <path>udp://127.0.0.1:5004?pkt_size=1316</path>
          <args>-format mpegts -vcodec mpeg2video -b:v 15M -g 1 -bf 0 -pix_fmt yuv420p -filter:a pan=stereo|c0=c0|c1=c1 -acodec mp2 -b:a 192k -ar 48000</args>
        </ffmpeg>
      </consumers>
    </channel>
  </channels>

  <controllers>
    <tcp>
      <port>5250</port>
      <protocol>AMCP</protocol>
    </tcp>
  </controllers>
</configuration>
```

- `<log-path>` … ディスクログは**有効のまま**（`disable` 属性を付けていない）。journald 併用だが実機ではディスクログも残す運用（旧ドラフトの「journald 集約のため内蔵ログ無効化」は不採用）。
- `<enable-gpu>false</enable-gpu>` … §6.2 のとおり headless での CEF GPU segfault 回避のため false。
- `<video-mode>720p6000</video-mode>` … 720p60 が現行の唯一の稼働値。1080p 化する場合は `1080p3000`（30fps。容量の壁で 60fps は不可）＋ `icstv-encoder` 側のビットレート/GOP 調整が必要（容量検証の手順は運用 runbook にあるが、このツリーには含まれない）。
- consumer は **UDP のみ**（RTMP/CF 送出も NVENC も無い）。`-vcodec mpeg2video`（CasparCG 側の記法は `-codec:v`/`-vcodec` どちらも通る。本設定は伝統的な `-vcodec` 表記）で軽量 intra mpeg2video にエンコードし、実 H.264 化は §6.4 のサイドカーへ委ねる（CasparCG の ffmpeg consumer は VAAPI 不可 = [CasparCG Issue #1300](https://github.com/CasparCG/server/issues/1300)）。
- 音声は `-filter:a pan=stereo|c0=c0|c1=c1` で 16ch（CasparCG の既定 audio mixer）→ stereo へダウンミックス（mp2 は 2ch までのため必須。無いと consumer 初期化が失敗する）。
- `<lock-clear-phrase>` … `LOCK`/`UNLOCK` AMCP コマンドの解除フレーズ（誤ロック時の保険）。
- `<controllers><tcp><port>5250</port>` … AMCP listener。**全 channel で共有**（§6.6）。
- **1ch 構成である旨**: 実機 config のコメント（44-54 行付近）に「2026-08-18 に実機を正として 1ch 構成へ揃えた。以前は channel 2（ICS-TV ch2）と exposure_policy(#27) 用の channel 3/4 が宣言されていたが、送出ノードの CPU/GPU 負荷削減のため削除済み。宣言するだけで channel ごとに mixer/consumer のコストが乗るため」と明記されている。4ch 構想（§6.6）を復活させる場合はここへ追記する。
- screen consumer（`<screen>`）はプレビュー用途であり、headless 構成では利用しない。

### 6.4 出力 consumer（UDP → サイドカー `icstv-encoder` → tee: YouTube + ローカル MediaMTX）

**CasparCG channel の consumer は UDP mpegts のみ**（§6.3）。外部への H.264 送出は CasparCG の外、`icstv-encoder@<slug>.service`（`deploy/playout-node/systemd/icstv-encoder@.service` が正本）という別プロセスが担う。理由は CasparCG の ffmpeg consumer が VAAPI に対応しないため（[Issue #1300](https://github.com/CasparCG/server/issues/1300)）。

サイドカーの処理: `udp://127.0.0.1:${UDP_PORT}`（CasparCG の当該 channel の UDP 出力）を読み、`/dev/dri/renderD128` 上の `h264_vaapi` でエンコードし、`-f tee` で 2 系統へ同時送出する。

```
ffmpeg -i udp://127.0.0.1:${UDP_PORT}?fifo_size=1000000&overrun_nonfatal=1&timeout=10000000 \
  -vaapi_device /dev/dri/renderD128 -vf format=nv12,hwupload \
  -c:v h264_vaapi -b:v ${VBITRATE} -maxrate ${VBITRATE} -bufsize ${VBUFSIZE} -g ${GOP} -bf 0 \
  -c:a aac -b:a 160k -ar 48000 -flags +global_header \
  -f tee -use_fifo 1 -fifo_options "attempt_recovery=1:recover_any_error=1:drop_pkts_on_overflow=1:recovery_wait_time=1" \
  "[onfail=ignore:f=flv]${CF_INGEST_URL}|[onfail=ignore:f=flv]rtmp://127.0.0.1:1935/hls/${ICSTV_CHANNEL_SLUG}"
```

- **1 本目 (`${CF_INGEST_URL}`) = YouTube ingest**（`rtmp://a.rtmp.youtube.com/live2/<key>`。変数名 `CF_INGEST_URL` は historical で Cloudflare 由来だが、値は YouTube ingest URL。実際にリネームはしていない＝`env/encoder.env.example` 参照）。当面のメインの公開配信経路。
- **2 本目 = ローカル MediaMTX**（`rtmp://127.0.0.1:1935/hls/<slug>`）。MediaMTX が HLS 化し、自前プレイヤー（`tv.yagamin.net/hls`）と HLS ABR ladder（構築手順は運用 runbook にあるが、このツリーには含まれない）の入力になる。
- **Cloudflare Live Input はこの経路のどこにも登場しない**。studio の手動操作（CF Live Input/Output 管理カード）と fanclub simulcast 用に別途 CF オブジェクトは存在するが、本線 tee のどちらの leg でもない。
- `-bf 0` 必須: VAAPI 既定の B-frame が出るとフレーム並び替えにより MediaMTX の HLS muxer がクラッシュし、`icstv-hls-ladder` を巻き込んで `/hls2` が flap する（実証済、`icstv-encoder@.service` のコメント参照）。
- `-flags +global_header` 必須: tee は offline format 扱いで global_header が伝播せず、無いと MediaMTX が "unable to parse H264 config" で publish を拒否する。
- 各 leg の `use_fifo 1` は独立スレッド分離＋自動再接続（`attempt_recovery`）を意味し、片方が詰まっても他方を阻害しない。ただし CasparCG が wedge して UDP 入力自体が枯れると両 leg とも進まなくなるため、入力側に `timeout=10000000`（10秒）を付けて枯渇を検知・エラー終了させ `Restart=always` に拾わせる（§6.8 の wedge 対策）。
- ウォッチドッグ（§6.7）は `icstv-encoder@<slug>` の `systemctl is-active` と MediaMTX の publish 状態・HLS セグメント鮮度で生存判定する（CF Live Input のヘルスは見ない＝経路に無いため）。
- exposure_policy（#27）導入チャンネルのカットオーバー時は、この共有テンプレートは変えず対象チャンネルの instance だけ systemd drop-in で ExecStart を MediaMTX leg のみへ上書きする（`icstv-encoder@ch1.service.d.example/` 参照）。

### 6.5 systemd 常駐（実機構成）

CasparCG Server 本体と、メディアメタデータ生成を担う scanner をそれぞれ systemd サービスとして常駐させる。実機の unit は `deploy/playout-node/systemd/casparcg-server.service` と sha256 完全一致（正本はリポジトリのファイル自体を参照）。`RestartForceExitStatus=5` は AMCP `RESTART`（終了コード 5）による自己再起動要求を systemd 側で再起動として扱うための指定である。

```ini
# /etc/systemd/system/casparcg-server.service（実機構成の要点抜粋。全文は deploy/playout-node/systemd/casparcg-server.service）
[Unit]
Description=ICS-TV CasparCG Server (headless EGL, Intel UHD630)
After=network-online.target casparcg-scanner.service
Wants=network-online.target

[Service]
Type=simple
User=casparcg
Group=casparcg
WorkingDirectory=/opt/casparcg
ExecStart=/usr/bin/casparcg-server /etc/icstv/casparcg.config
Restart=on-failure
RestartForceExitStatus=5
RestartSec=3
# --- §6.1/§6.2 の実証済み必須 env ---
Environment=EGL_PLATFORM=surfaceless
Environment=LIBVA_DRIVER_NAME=iHD
Environment=LC_ALL=C.UTF-8
Environment=LANG=C.UTF-8
# --- §6.8 の wedge 対策（2026-08-18 見直し） ---
MemoryMax=3G
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
```

- **設定ファイル引数**: `ExecStart` は `/etc/icstv/casparcg.config` を明示指定する（PPA バイナリの既定はカレントディレクトリの `casparcg.config` を探すため、`install.sh` が配置する `/etc/icstv/casparcg.config` を明示しないと違う設定が読まれうる）。
- **実行ファイル名**: 実機は `/usr/bin/casparcg-server`（PPA `casparcg-server-2.5` パッケージ。`/usr/bin/casparcg-server-2.5` も併存）。`casparcg-server-beta` 等の名前は版によって変わるため配備時に実体へ合わせる。
- **`MemoryHigh` は意図的に設定しない**: high を超えるとプロセスがリクレイムで throttle され、AMCP 無応答のまま OOM kill もされず wedge する（§6.8）。`MemoryMax=3G` は「watchdog が取りこぼした場合の最後の砦」。
- **`TimeoutStopSec=20`**: wedge した casparcg は SIGTERM に応答しないため、既定の 90s を待たず早めに SIGKILL へ落とす（`scripts/watchdog.sh` のエスカレーションと対）。
- casparcg-scanner（`/usr/bin/casparcg-scanner`）も実機で稼働中。unit は `deploy/playout-node/systemd/casparcg-scanner.service` を参照。

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now casparcg-scanner
sudo systemctl enable --now casparcg-server
```

### 6.6 実機は 1ch・単一プロセス（複数 ch 構成は将来案）

**現行の実機構成は 1ch・単一 CasparCG プロセス・AMCP 5250 を（唯一の ch で）専有**（§6.3 の config コメントどおり、2026-08-18 に ch2〜4 を削除して 1ch へ揃えた）。当初案の「最大 4ch・ch 毎にプロセス分離」は実装されておらず、既定方針としても取り下げる。CasparCG は 1 プロセス内に複数 channel を宣言でき、実機はこの **1 プロセス N channel** 方式（当面 N=1）を採る。

| 観点 | 1 プロセス N channel（**現状**） | プロセス分離（将来案） |
|------|---------------------|--------------|
| 障害分離 | 1 プロセス停止で全 ch 停止 | ch 単位で独立。1ch クラッシュが他に波及しない |
| AMCP ポート | 単一（5250）に全 ch | ch 毎に分ける（agent の接続先も分離）|
| GPU/EGL | 1 EGL/CEF コンテキスト共有 | プロセス毎に EGL/CEF を確保 |
| 運用 | systemd unit 1 つ（casparcg-server は非テンプレート） | systemd template unit（`@`）で ch 毎 |

マルチ ch（2ch 以上）へ復活させる場合の留意点（実機で GPU 容量が確認できるまでは着手しない）:

- **UHD630 の実測余裕は乏しい**: 本線（720p60）合成だけで GPU の render エンジン（RCS）を約 80% 使う（実測値。測定の記録はこのツリーには含まれない）。exposure_policy(#27) の YT ミラー検証では、本線 + 公開ミラーの 2 本で RCS ≈ 90% に達しており（[site-only-broadcast.md](site-only-broadcast.md) §5 リスク1）、3 本目は専用 GPU 増設が実質前提。
- **NVENC は使わない**: 本構成は Intel VAAPI（サイドカー `icstv-encoder`）のみで、CasparCG consumer・NVENC いずれも経路に無い（§6.3/§6.4）。NVENC 同時セッション数の上限論は本構成には該当しない。
- ch を復活させる場合は `video-mode` を本線と一致させること（route producer は同一 framerate/sync の channel 間のみ対応するため）。UDP ポートは `5004 + (N-1)` で採番する。

共通の CG（全 ch 一斉の速報テロップ等）を 1 か所で作って各 ch に配る場合は **route producer** を使える。route は同一 framerate / 同一 sync の channel 間のみ対応するため、全 channel の `<video-mode>` を揃える前提で使う（Phase 1 では不要）。

```
PLAY 1-40 route://9-40
```

### 6.7 ウォッチドッグ（実機構成: agent 内蔵の出力監視 + ノードの再起動エスカレーション）

24/7 配信の健全性監視は、playout agent 内蔵の監視（層0）と、agent とは別プロセスの `icstv-watchdog.timer`（層1〜3、30s 周期。`deploy/playout-node/scripts/watchdog.sh` が正本）の二段構成で行う。**OSC 無音監視は未実装**（§5.2）で、AMCP `INFO` ポーリングと MediaMTX API・HLS セグメント鮮度が実際の監視手段。

**層0: agent 内蔵の出力 watchdog（`agent/icstv_agent/main.py` `_output_watchdog`）**

playout agent 自身が本線（`N-10`）の foreground を `ICSTV_WATCHDOG_POLL_SEC`（既定 3s）周期でポーリングし、`producer=empty`（黒）または `name`/`time` が進まない（フリーズ）状態が `ICSTV_WATCHDOG_BAD_TICKS`（既定 3）回連続したら、agent 自身が現行イベントを本線へ再 take する（`_retake_current`。ヒステリシス ≈ 3×3s = 9s）。スレート中（feed 断退避 / 手動緊急）は意図的な出力なので skip、`INFO` 取得失敗は判定不能として skip。これが「AMCP は健全と言うが出力が黒/静止」という盲点への一次対応で、下記の層1〜3（ノード再起動）が動くよりずっと早く・軽い介入で復旧する。

**層1〜3: `icstv-watchdog.timer`（`scripts/watchdog.sh`、30s 周期・全 channel 共有の単一 CasparCG プロセスを対象）**

0. **計画的メモリリサイクル**: `casparcg-server.service` の cgroup メモリが `CASPAR_MEM_RECYCLE_BYTES`（既定 2.2GiB）を超えたら、`MemoryMax`（3G、§6.5）に当たる前に自主的に再起動する（§6.8）。
1. **AMCP 応答（疎通・パーサ生存）**: `VERSION` を送り応答有無を見る。無応答が `AMCP_FAIL_THRESHOLD`（既定 3、30s×3≈90s）回連続したら再起動へ。
2. **本線（`N-10`）が黒か**: `INFO {ch}-10` の foreground を agent 実装（`icstv_agent.caspar.parse_foreground`）と同一ロジックで解釈し、`empty` かつ層 90（緊急スレート）も `playing` でない状態が `MAIN_BLACK_TICKS`（既定 3）回連続したら「層0（agent の再 take）が効いていない」と判断し介入する。同時に `icstv-agent@<slug>` 自体の生存も確認し、落ちていれば restart する。
3. **サイドカー `icstv-encoder@<slug>` の生存**: `systemctl is-active` で確認し、落ちていれば restart。
4. **MediaMTX**: API 死活に加え、publish 中の path があるか（`itemCount`/`ready`）を見る。API が正常応答でも publisher 不在（`itemCount:0`）のケースを別途検知する。
5. **HLS セグメント鮮度**: `/run/icstv-hls/<slug>/` の `.ts` 更新が `HLS_STALE_SEC`（既定 30s）を超えて止まっていないかを検知専用でログに出す（自動復旧はしない。ladder 自身が再試行するため二重に再起動すると振動する）。

**介入アクション**: 本線黒への介入は **緊急スレートへの退避ではなく `casparcg-server.service` の再起動**（`systemctl restart` → 25s で終わらなければ `SIGKILL` へエスカレーション → `Restart=on-failure` で自動復帰）。casparcg を落とすと agent が「切断→再接続」を観測し、現行イベントを本線へ貼り直す（agent 自体の restart では現行を貼り直さない設計のため、casparcg 側を落とす）。再介入は `MAIN_BLACK_COOLDOWN_SEC`（既定 600s）を空ける。

> ⚠️ **緊急スレート（`PLAY N-90 ... LOOP`）へ監視系から退避してはいけない**（2026-08-21 の 35 分黒落ちを受けて廃止）。層 90 は層 10 を覆うため、上げると次の予定 `TAKE` が来ても画面はスレートのまま止まり、解除は編成の `CLEAR_SLATE` イベントだけ。監視系が放送中に誤って上げると次の休止帯まで数時間画面が固まる、黒より悪い結果になる。緊急スレートは feed 断検知（§4.5）等、能動的な `goto_slate(channel)` プリミティブからの意図的な発火にのみ用いる。

```
VERSION
INFO {ch}
INFO {ch}-10
```

GL リソース枯渇の兆候時は `GL GC` を手動実行する（watchdog.sh の自動介入経路には無い）。AMCP からの自己再起動要求は `RESTART`（終了コード 5、`RestartForceExitStatus=5` と組み合わせる）、最終手段の停止は `KILL`。

### 6.8 フレーム精度の限界と 24/7 の落とし穴

AMCP はテキストプロトコルであり、コマンド到達のタイミングはネットワーク遅延・パーサ処理に依存する。このため「コマンド送出時刻」だけでフレーム精度の切替を保証することはできない。対策として agent 側で次を徹底する（[scheduler.md](scheduler.md) と整合）。

- **事前ロード（PREROLL=5s）**: `LOADBG` で先読みし、FFmpeg producer のオープン遅延を切替フレームから排除する。プリロードを省くと黒みやコマ落ちとなるため必須。
- **フレーム正確なテイク**: 背面ロード済みイベントは `scheduled_at` で引数なし `PLAY` により切り替える。
- **冪等キーによる二重テイク防止**: ウォッチドッグの再送・リトライがフレームを乱さないよう、テイクは冪等に扱う。

長時間連続運用の既知問題と緩和策:

- **CEF/HTML の VRAM リーク**（GitHub Issue #1265, #1363）: テンプレの追加・削除を繰り返すと VRAM が解放されず増加し、最終的にレンダリングが破綻する。緩和策は (a) VRAM 監視＋しきい値超過で計画的なサービス再起動、(b) 常設レイヤ＋`CG UPDATE` でテンプレ再生成を減らす、(c) `GL GC` の定期実行。
- **FFmpeg producer のメモリ / CPU リーク**: `LOAD`/`LOADBG` 反復により漸増するリーク報告がある。本システムは素材ごとに producer をロードし続けるため累積で顕在化しうる。監視＋計画再起動＋版更新の取り込みで緩和する。
- **FFmpeg consumer の `cannot allocate memory` 失敗**（Issue #1530）: 送出 consumer のヘルスをウォッチドッグ第二層で監視し、欠落・失敗時に `ADD`/`REMOVE` で再追加または再起動する。
- **ログのディスク肥大**: 実機は内蔵ログを無効化せず journald と併用している（§6.3）。ディスク使用量は別途監視し、動的なログ調整は `LOG LEVEL` / `LOG CATEGORY`（有効カテゴリは `calltrace` / `communication`）で行う。
- **計画再起動の前提**: 上記リーク群は完全には回避できないため、配信に影響しない時間帯（または別 ch のバックアップ送出と組み合わせ）で計画再起動を行い、systemd（`RESTART` + `RestartForceExitStatus=5`）で自動回復可能な形にしておく。

```
LOG LEVEL info
LOG CATEGORY communication 1
```

#### 2026-08-18 の実障害（1h35m 無配信）と恒久対策

CasparCG 本体の anon メモリアリーナが単調増加する現象（実測: 仮想 3.0GiB/実 2.01GiB。CEF 子プロセスは合計 100MiB 未満で犯人ではない）に対し、systemd の `MemoryHigh` で上限を設定していたところ、`MemoryHigh` 超過時のメモリリクレイムでプロセスが throttle され、**AMCP が無応答のまま OOM kill もされずに wedge**する事故が起きた（1h35m のオンエア断）。throttle 中は SIGTERM にも応答しないため、通常の `systemctl restart` は `TimeoutStopSec` まで固まる。

恒久対策（3点、いずれも `deploy/playout-node/` の実ファイルへ反映済み）:

1. **`icstv-encoder@.service` の入力 UDP に `timeout=10000000`（10秒）を付与**（§6.4）。CasparCG が wedge して UDP 入力が枯れても、既定の UDP timeout=0（無限待ち）だと ffmpeg 側が入力枯渇を検知できず `attempt_recovery`/`Restart=always` のどちらも発火しない。10 秒で入力なしを検知しエラー終了させ、`Restart` に拾わせる。
2. **`casparcg-server.service` は `MemoryHigh` を設定せず `MemoryMax=3G` を最後の砦にする**（§6.5）。`MemoryMax` に当たれば確実に OOM kill され `Restart=on-failure` で復帰する（wedge するよりましという判断）。`TimeoutStopSec=20` で wedge 時の SIGKILL エスカレーションを早める。
3. **`icstv-watchdog.timer`（`scripts/watchdog.sh`）の計画的リサイクル**（§6.7 層1〜3の手順0）: メモリが 2.2GiB（`MemoryMax` の手前）を超えたら、AMCP がまだ応答するうちに自主的に再起動する。天井に当たってからでは stop/start とも詰まるため、天井の手前で計画的に回すのが肝。

2026-08-21 には別件で「本線が黒になっても watchdog が検知できていない」バグ（`layer_state` の判定ロジック不備。§6.7 参照）による 35 分黒落ちも発生し、同時に「緊急スレートへの退避」という当時の対応方針そのものが問題（層 90 が層 10 を覆い、放送中に上げると編成の `CLEAR_SLATE` まで戻らない）と判明したため、casparcg 再起動方式へ切り替えた（§6.7 の警告ボックス参照）。

## 未確定・論点

- **CG プレーンの配層方式**: 本書は「HTML producer は cg_layer をサポートせず（cg_layer は常に 0）、同時表示が要る CG は別 video layer に分ける」を canonical とした。実機（§1.1: 30/35/36/37/38/40/41/45/50 を別レイヤに割当済み）はこの前提で運用されており、cg_layer 多重を試す動機は薄い。単一 video layer 上に複数 cg_layer を積む流儀は未採用のまま。
- **`enable-gpu` 無効化時の CEF**: **解決済み**。実機は `<enable-gpu>false</enable-gpu>`（CPU レンダリング）で稼働しており、テロップ / Lバー / 地図 CG 等すべて本番で問題なく描画できている（§6.2）。GPU モードは headless での CEF サブプロセス segfault のため採用していない。
- **X11 非依存**: **解決済み**。CasparCG 2.5.0 を採用し、Xorg 無しの完全 headless（EGL `surfaceless`）で本番稼働中（§6.1）。2.3 系の Xorg 要件検討は不要になった。
- **`h264_nvenc` / NVENC 関連の未確定事項**: **本構成には該当しない**。CasparCG consumer は UDP のみで NVENC を使わず、H.264 化はサイドカー `icstv-encoder` の VAAPI（`h264_vaapi`）が担う（§6.3/§6.4）。GPU も Intel UHD630（iGPU）で NVIDIA ではない。将来 NVIDIA GPU 搭載ノードを追加する場合に再検討する。
- **SRT 経由の CF Live Input 送出 / STREAM consumer index**: **本構成には該当しない**。CasparCG channel の consumer は UDP mpegts のみで、`ADD [ch]-[idx] STREAM ...` 形式の FFmpeg consumer は現行経路で使っていない（§6.4）。Cloudflare Live Input 自体は studio 手動操作 / fanclub simulcast 用に残るが、これらは AMCP の `STREAM` consumer ではなく別経路（`core/cloudflare_api.py` の CF API 直叩き）。
- **生入力の FFmpeg producer 解決とチューニング**: MediaMTX の RTMP を `LOADBG "rtmp://127.0.0.1:1935/<app>/<key>"` で直接参照する方式は実運用で機能している（§4.4 の生復帰シーケンス。OBS 側の接続情報とエンコード設定を扱う文書はこのツリーには含まれない）。ただし入力バッファ深さ・許容ジッタ・再接続を制御する producer パラメータの詳細は未確認のまま。低遅延・バッファ調整は MediaMTX 側で行う運用を基本とする。
- **CM バンドルの実装方式**: 「1 プロデューサ化（連結クリップ / playlist）」と「agent 逐次テイク」の優劣・冪等性は [scheduler.md](scheduler.md) の割り込み設計と突き合わせて確定する（AUTO 多段スタックは仕様上不可と確定したため不採用）。
- **OSC / INFO の粒度**: OSC は §5.2 のとおり**未実装**のため当面は据え置き（採用する場合は `foreground/` の有無、`file/time` の引数個数・単位、`INFO` 返却の XML 構造を採用版で要確認）。残尺 / feed 断判定は現状どおり MediaMTX 側監視と `INFO` ポーリングを一次とする。
- **引数なし `PLAY` の SEEK/LENGTH 保持**: 仕様上は保持される想定だが、頭出し / 尺の挙動は実機確認推奨。
- **生退避中のミュート**: `MIXER 1-11 VOLUME 0`（書式 `MIXER [ch]-[layer] VOLUME [float] {duration tween}` で実在）の要否は退避運用で実機確認。スレート時の master audio 方針も併せて確定する。
- **利用可能な tween / transition の網羅**: `linear` / `easeinoutsine` / `easeoutquad` / `easeinquad`、`MIX` / `CUT` / `PUSH` / `SLIDE` / `WIPE` は確認済だが、当該ビルドでの網羅リストと正確な対応は未検証。
