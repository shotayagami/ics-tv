// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { api } from "@icstv/api";
import { Notice, StudioPage } from "../atoms";

import type { AdminChannel } from "../hooks";

/** 1チャンネルの編集行。name/slug/略称/識別色/公開 を編集 → 保存 (POST)。 */
function ChannelRow({ ch, onSaved }: { ch: AdminChannel; onSaved: (m: string) => void }) {
  const [name, setName] = useState(ch.name);
  const [slug, setSlug] = useState(ch.slug);
  const [short, setShort] = useState(ch.short);
  const [tint, setTint] = useState(ch.tint || ch.tint_color);
  const [enabled, setEnabled] = useState(ch.enabled);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const dirty = name !== ch.name || slug !== ch.slug || short !== ch.short || (tint || "") !== (ch.tint || "") || enabled !== ch.enabled;

  async function save() {
    if (busy) return;
    setBusy(true);
    setErr("");
    try {
      const { error } = await api.POST("/api/v1/admin/channels/{slug}", {
        params: { path: { slug: ch.slug } },
        body: { name, slug, short, tint, enabled },
      });
      if (error) {
        setErr((error as { detail?: string })?.detail || "保存に失敗しました");
        return;
      }
      onSaved(`「${name}」を保存しました`);
    } catch {
      setErr("通信に失敗しました");
    } finally {
      setBusy(false);
    }
  }

  return (
    <tr>
      <td>
        <input value={name} onChange={(e) => setName(e.target.value)} style={{ width: "11rem" }} />
      </td>
      <td>
        <input value={slug} onChange={(e) => setSlug(e.target.value)} style={{ width: "7rem" }} title="変更すると送出 agent の再設定が必要" />
      </td>
      <td>
        <input value={short} onChange={(e) => setShort(e.target.value)} maxLength={20} placeholder={ch.name} style={{ width: "5rem" }} />
      </td>
      <td>
        <span style={{ display: "inline-flex", gap: ".3rem", alignItems: "center" }}>
          <input type="color" value={/^#[0-9A-Fa-f]{6}$/.test(tint) ? tint : ch.tint_color} onChange={(e) => setTint(e.target.value)} style={{ width: 28, height: 24, padding: 0, background: "none", border: "1px solid var(--line)" }} />
          <input value={tint} onChange={(e) => setTint(e.target.value)} placeholder="#rrggbb" style={{ width: "5.5rem" }} />
        </span>
      </td>
      <td style={{ textAlign: "center" }}>
        <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
      </td>
      <td style={{ whiteSpace: "nowrap" }}>
        <button className="btn" type="button" onClick={save} disabled={busy || !dirty} style={{ fontSize: ".78rem", padding: ".2rem .6rem", opacity: dirty ? 1 : 0.5 }}>
          保存
        </button>
        <Link to={`/channels/${ch.slug}/settings`} style={{ marginLeft: ".5rem", fontSize: ".78rem", color: "var(--muted)" }}>
          詳細設定 ›
        </Link>
        {err && <span style={{ color: "var(--warn)", marginLeft: ".5rem", fontSize: ".78rem" }}>{err}</span>}
      </td>
    </tr>
  );
}

/** チャンネル管理 (#Phase2d-5)。一覧 + 基本編集 (name/slug/略称/識別色/公開)。
 * 既定メディア割当・CF 再生URL・YouTube 接続状態は SPA 内「詳細設定」(#2e-4) へ。 */
export function ChannelsPage() {
  const [chs, setChs] = useState<AdminChannel[] | null>(null);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");

  const load = useCallback(() => {
    api
      .GET("/api/v1/admin/channels")
      .then(({ data, error }) => (error ? setErr("読み込み失敗 (staff 権限が必要)") : data && setChs(data)))
      .catch(() => setErr("読み込み失敗"));
  }, []);
  useEffect(load, [load]);

  function onSaved(m: string) {
    setMsg(m);
    load();
  }

  if (err && !chs) return <p className="st-loading">{err}</p>;
  if (!chs) return <p className="st-loading">読み込み中…</p>;

  return (
    <StudioPage title="チャンネル管理">
      {msg && <Notice variant="success">{msg}</Notice>}
      <div className="card">
        <table>
          <thead>
            <tr>
              <th>表示名</th>
              <th>slug</th>
              <th>略称</th>
              <th>識別色</th>
              <th style={{ textAlign: "center" }}>公開</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {chs.map((c) => (
              <ChannelRow key={c.id} ch={c} onSaved={onSaved} />
            ))}
            {chs.length === 0 && (
              <tr>
                <td colSpan={6} className="muted">
                  チャンネルがありません。
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <p className="muted" style={{ fontSize: ".75rem", marginTop: ".6rem" }}>
        slug 変更は送出ノードの agent 再設定を伴います。既定メディア割当 / Cloudflare 再生URL / YouTube 接続は各行の「詳細設定」へ。
      </p>
    </StudioPage>
  );
}
