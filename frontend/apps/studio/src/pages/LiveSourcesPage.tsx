// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useCallback, useEffect, useState } from "react";

import { api } from "@icstv/api";
import { EmptyState, Notice, OldScreenLink, StudioPage } from "../atoms";

import type { LiveSourceRow, LiveSourcesOut } from "../hooks";

/** クリップボードへコピーするボタン。コピー後 1.2s だけ「コピー済」表示。 */
function CopyBtn({ value }: { value: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      className="btn"
      type="button"
      style={{ fontSize: ".72rem", padding: "0 .45rem", marginLeft: ".4rem" }}
      onClick={() => {
        navigator.clipboard?.writeText(value).then(
          () => {
            setDone(true);
            window.setTimeout(() => setDone(false), 1200);
          },
          () => undefined,
        );
      }}
    >
      {done ? "✓ コピー済" : "コピー"}
    </button>
  );
}

/** ラベル付き値 1 行 (コピー可)。code 表示で長い URL も折返し。 */
function Field({ label, value, copy = true }: { label: string; value: string; copy?: boolean }) {
  return (
    <div style={{ display: "flex", alignItems: "baseline", gap: ".4rem", margin: ".25rem 0" }}>
      <span className="muted" style={{ minWidth: "8.5rem", fontSize: ".8rem" }}>
        {label}
      </span>
      <code style={{ wordBreak: "break-all", flex: 1 }}>{value}</code>
      {copy && <CopyBtn value={value} />}
    </div>
  );
}

/** 1 ソースのカード。SRT (推奨) と RTMP (Server/Stream key 分割) を OBS に貼れる形で並べる。 */
function SourceCard({ src }: { src: LiveSourceRow }) {
  // OBS の RTMP は Server と Stream key に分かれる。rtmp_url=rtmp://host:1935/<app>/<key> の
  // 末尾 /<key> を剥がして Server (= .../<app>)、key を取り出す。
  const cut = src.rtmp_url.lastIndexOf("/");
  const rtmpServer = src.rtmp_url.slice(0, cut);
  const rtmpKey = src.rtmp_url.slice(cut + 1);
  return (
    <section className="card" style={{ marginBottom: "1rem" }}>
      <h2 style={{ display: "flex", alignItems: "center", gap: ".6rem", margin: "0 0 .5rem" }}>
        {src.name}
        <span className="muted" style={{ fontSize: ".78rem", fontWeight: "normal" }}>
          path: {src.mediamtx_path}
        </span>
      </h2>

      <h3 style={{ margin: ".4rem 0 .1rem", fontSize: ".82rem" }}>
        SRT <span className="muted" style={{ fontWeight: "normal" }}>(推奨・OBS: サービス=カスタム / キーは空欄)</span>
      </h3>
      <Field label="サーバー" value={src.srt_url} />

      <h3 style={{ margin: ".6rem 0 .1rem", fontSize: ".82rem" }}>
        RTMP <span className="muted" style={{ fontWeight: "normal" }}>(LAN/サブ卓向け)</span>
      </h3>
      <Field label="サーバー" value={rtmpServer} />
      <Field label="ストリームキー" value={rtmpKey} />

      <div className="muted" style={{ fontSize: ".78rem", marginTop: ".5rem" }}>
        SRT latency: {src.srt_latency_ms}ms · passphrase:{" "}
        {src.has_passphrase ? (
          <span style={{ color: "var(--warn)" }}>設定あり (上記 SRT URL に含む / MediaMTX env と一致必須)</span>
        ) : (
          "なし"
        )}
        {src.note && <> · {src.note}</>}
      </div>
    </section>
  );
}

/** 生入力 (OBS ingest) 接続情報の閲覧。現場 OBS/サブ卓へ渡す SRT/RTMP push URL を確認・コピー。
 * LiveSource は Channel 非依存なのでチャンネル選択は持たない (全ソース一覧)。CRUD (作成/編集/
 * passphrase 設定) は当面 Django admin に据え置き、ヘッダの「Live sources (作成/編集) ›」へ誘導。 */
export function LiveSourcesPage() {
  const [d, setD] = useState<LiveSourcesOut | null>(null);
  const [err, setErr] = useState("");

  const load = useCallback(() => {
    api
      .GET("/api/v1/admin/live-sources")
      .then(({ data, error }) =>
        error ? setErr("読み込み失敗 (staff 権限が必要)") : data && (setD(data), setErr("")),
      )
      .catch(() => setErr("通信に失敗しました"));
  }, []);
  useEffect(load, [load]);

  if (err && !d) return <EmptyState loading>{err}</EmptyState>;
  if (!d) return <EmptyState loading>読み込み中…</EmptyState>;

  return (
    <StudioPage
      title="生入力 (OBS ingest)"
      actions={<OldScreenLink href={d.admin_url}>Live sources (作成/編集)</OldScreenLink>}
    >
      {!d.host_configured && (
        <Notice variant="error">
          送出ノードの host が未設定です (環境変数 ICSTV_INGEST_NODE_HOST)。URL のホスト部が雛形のままです。
        </Notice>
      )}

      {d.sources.length === 0 ? (
        <EmptyState>
          生入力ソースがありません。「Live sources (作成/編集)」から LiveSource を作成してください。
        </EmptyState>
      ) : (
        d.sources.map((s) => <SourceCard key={s.id} src={s} />)
      )}

      <p className="muted" style={{ fontSize: ".75rem", marginTop: ".6rem" }}>
        OBS エンコードは送出パイプライン (CasparCG {`720p60`}) に合わせる。外部 (現場) からは Cloudflare WARP 経由。
        ソースの作成/編集・passphrase 設定は「Live sources (作成/編集)」へ。
      </p>
    </StudioPage>
  );
}
