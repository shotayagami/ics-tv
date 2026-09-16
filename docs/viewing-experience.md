# 視聴体験の深化 — 字幕(自動生成) + タイムシフト（決定 #24）

Phase 2 相当の視聴体験強化。バックログ `design/ICS-TV_backlog.md` の
PLAYER-02/03（追っかけ/タイムシフト）・PLAYER-04（字幕・複数音声）・ADMIN-04（Whisper 字幕）が
いずれも「要設計/要検討」のまま残っていたため、本書で技術方針を確定する。

背景（2026-07 時点）: 急激な登録者減を受けた 4h 枠化・放送休止導入（overview #3 補足 / §3.6）の一方で、
**視聴体験の向上をメインに据える**方針（2026-06-19）は継続。初期機能（リニア送出・EPG・VOD・会員・
サブスク課金）は固まったので、次の一手として字幕とタイムシフトを実装する。

## 0. 大原則: 2 領域は性質が正反対

| | 字幕/Whisper (PLAYER-04 + ADMIN-04) | タイムシフト/追っかけ (PLAYER-02/03) |
|---|---|---|
| ボトルネック | 無し（k8s + フロントで完結） | **送出ノードの LXC**（4GB RAM / 3 コア / iGPU RCS ~100% 飽和） |
| 送出経路リスク | ゼロ（別ワーカー・別 pod） | 中〜高（ライブ HLS 原点の変更が要る） |
| 制約の本質 | CPU（Whisper）だが別 pod 隔離で解決 | **ストレージ**（retention は計算増ゼロ・tmpfs=RAM が壁） |

要点: タイムシフトは「計算」ではなく「保持（RAM/ディスク）」問題。現状の DVR 窓は
`hls_list_size=7 × hls_time=2s = 14 秒`、しかも ladder 出力先が **tmpfs（RAM）**
（`deploy/playout-node/systemd/icstv-hls-ladder@.service`）。
4GB 筐体で 1h 窓（約 3.1GB）を RAM に載せると CasparCG/encoder が OOM = 停波。よって本格 DVR は
ディスク退避が前提で、これは実ノード作業（pct exec・非 git `/opt`）になる。

## 1. フェーズ分割

- **Phase A（字幕/Whisper）**: 純ソフト・送出無風。PLAYER-04（VOD 字幕）+ ADMIN-04（自動生成）を
  エンドツーエンドで完結。**最初に実装**。
- **Phase B（短窓タイムシフト）**: RAM 内に収まる ~60 秒窓の一時停止/巻き戻し/ライブ復帰（PLAYER-03）。
  フロントの mode-aware 化 + ladder の `hls_list_size` 小幅拡大（isolated unit・`systemctl stop` で
  即ロールバック）。
- **Phase C（本格 DVR + 頭出し）**: PLAYER-02。番組開始からの追っかけ。**別 issue**。
  - C1: ladder 出力を tmpfs→ディスク（100GB mp0）へ退避し `hls_list_size` を 1800（1h）へ。実ノード作業。
  - C2（推奨）: 生放送録画→VOD（v0.8.72）が**放送終了後**に頭出し再生を既に実現。加えて YouTube 側 DVR
    （`enable_dvr` は `server/youtube/api.py` に配線済）で放送中シークを無コストで提供。
    → 新インフラ不要。バックログ PLAYER-02 の依存「タイムシフトバッファ or VOD-01」が許容する経路。

## 2. Phase A — 字幕（VOD）の設計

VOD プレイヤー（`frontend/apps/detail/src/VodPlayer.tsx`）は native `<video controls>`
なので `<track>` を足すだけでブラウザ標準の字幕メニューが出る。線形ライブへの字幕は encoder manifest 改変
（送出経路）につき**対象外** — 録画が VOD 化した時点で字幕が付く（`playback_asset` 経由で録画番組・
生放送録画の両方に均一適用）。

### 2.1 生成（ADMIN-04）
- エンジン = **faster-whisper**（CTranslate2・pip・int8 CPU・日本語対応）。`whisper.cpp`（要コンパイル）より
  image に馴染む。`WHISPER_MODEL`（既定 `small`）/`WHISPER_DEVICE=cpu`/`WHISPER_COMPUTE_TYPE=int8` を
  configmap で切替（MEZZ_* と同じ override パターン）。
- **専用キュー `captions` + 専用 pod**（`deploy/k8s/base/45-captions.yaml`・`-Q captions -c 1`）。
  正規化（`normalize` キュー）を絶対に飢餓させないための隔離が肝。concurrency-1 で 1 本ずつ。
- トリガ: `server/medialib/tasks.py` の `normalize_asset` 成功後、READY かつ
  `kind=program` の素材にだけ `transcribe_asset.delay()` を鎖（CM/フィラー/スレート/バンパーは除外＝
  ボリュームの大半は Whisper を回さない）。
- 実体 `server/medialib/captions.py`: ①mezzanine の R2 署名 URL を ffmpeg に渡し
  16kHz mono WAV を抽出（映像デコード回避で桁違いに安い）②faster-whisper（`language="ja"`, `vad_filter`）
  ③WebVTT 整形 ④`r2.put_object("captions/{kind}/{id}.<lang>.vtt", …)` ⑤`Asset.caption_*` 更新。
  モデルはモジュール singleton でキャッシュ。失敗は `caption_status=failed`・retry は 1 回（決定論的
  失敗の poison loop 回避）。

### 2.2 保存
`Asset`（`server/medialib/models.py`） にサイドカーフィールドを追加（migration 0010・既定空＝挙動不変）:
`caption_status`（none/processing/ready/failed）・`caption_r2_key`・`caption_lang`（既定 ja）。
多言語は将来 `AssetCaption` サイドカーへ拡張可（v1 は単一言語で足りる）。

### 2.3 配信 & 再生（PLAYER-04）
- `<track>` の CORS を避けるため **同一オリジンの Django プロキシビュー**で配信
  （`r2.get_object`→`text/vtt`）。既存のサムネ配信 `thumb_serve` と同じアプリ経由パターン。VTT は小さいので
  プロキシで十分。VOD 本体と同じ視聴ゲート（`can_watch`）を通す（gated 番組の文字起こし流出防止）。
- ルート `vod/<program_id>/captions.vtt` → `server/core/views.py`。
- `server/templates/public/vod_detail.html` に `data-captions` → `frontend/apps/detail/src/main.tsx`
  → VodPlayer に `<track kind="subtitles" srclang="ja" label="日本語" default>`。字幕 ON/OFF の永続化
  （PLAYER-06 残件）も localStorage（`icstv:player:captions`）で同時に実装。
- 既存 VOD 資産への遡及は管理コマンド `transcribe_missing_captions`（ready な program 資産で未生成のものを
  captions キューへ投入）で行う。

## 3. Phase B — 短窓タイムシフトの設計（PLAYER-03）

- ノード: `deploy/playout-node/systemd/icstv-hls-ladder@.service` の
  `-hls_list_size` を 7→30（約 60 秒窓）。tmpfs コスト ≈ 60×0.87MB/s ≈ 52MB（4GB に対し無視可）。
  `delete_segments` 維持で上限固定。`program_date_time` は既に発行済（頭出しの布石）。isolated unit ゆえ
  `systemctl stop icstv-hls-ladder@<slug>` で本線無傷ロールバック。
- フロント（`frontend/apps/player/src/VideoPlayer.tsx`）: **最重要は mode-aware 化**。
  既存 `userPaused` に倣い `behindLive`/`userSeeking` フラグを追加し、`seekLive()`・5s watchdog・
  `recover()`（visibility/focus/stalled）・`pendingLiveSync` が意図的な巻き戻しを live 端へ引き戻すのを
  止める。CSS に予約済の未使用 `.ctrl .progress` にスクラバ描画。`seekLive()` を「ライブに戻る」ボタンに。
  `backBufferLength` を窓幅（60s+）に合わせ引き上げ。live watchdog の停波復帰能力は温存する
  （凍結時＝behindLive でないときのみ発火）。

## 4. 制約・リスク（実装時の必読）

- **k8s に GPU 無し**: faster-whisper int8 CPU は ~0.1–0.3x realtime。concurrency-1 隔離で normalize と
  競合させない。クラスタ CPU は逼迫（k8s のコントロールプレーンノード 4c + ワーカーノード 3c）なので captions pod の request は小さく、limit で burst。
- モデル配布: faster-whisper は初回に HuggingFace からモデル DL（small ~0.5GB）。pod 再起動での再 DL を
  避けるためモデルキャッシュを PVC（`icstv-whisper-cache`）へ。`HF_HOME` を PVC に向ける。
- **線形ライブ字幕は対象外**: encoder/manifest 改変 = 送出経路。録画 VOD 化で字幕は付く。リアルタイム字幕は
  別プロジェクト。
- **複数音声（EXT-X-MEDIA AUDIO）も当面対象外**: 自主制作リニアの素材は複数音声を持たないことが多く
  費用対効果が薄い（PLAYER-04 の「素材にある場合」）。字幕のみ先行。
- Phase B の live 巻き戻しは、停波復帰 watchdog（実障害対策）を退行させないことが最大のリスク。behindLive
  ゲートを厳密に。

## 5. 対応バックログ

PLAYER-02（Phase C）・PLAYER-03（Phase B）・PLAYER-04（Phase A・字幕のみ）・PLAYER-06 字幕永続化（Phase A）・
ADMIN-04（Phase A）。複数音声（PLAYER-04 の一部）は当面見送り。
