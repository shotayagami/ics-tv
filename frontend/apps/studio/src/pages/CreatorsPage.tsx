// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
/** クリエイター一覧 + 新規作成 (#27 ファンクラブ)。詳細編集は CreatorDetailPage へ。 */

import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { api } from "@icstv/api";
import { EmptyState, Notice, StudioPage } from "../atoms";

import type { components } from "@icstv/api";

type CreatorAdminOut = components["schemas"]["CreatorAdminOut"];

function NewCreatorForm({ onCreated }: { onCreated: () => void }) {
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  async function create() {
    setBusy(true);
    setErr("");
    const { error } = await api.POST("/api/v1/admin/fanclub/creators", {
      body: {
        name,
        slug,
        description: "",
        status: "active",
        onboarding_status: "pending",
        legal_name: "",
        is_individual: true,
        representative_name: "",
        address: "",
        phone: "",
        hide_contact_details: true,
        contact_email: "",
        invoice_registration_number: "",
      },
    });
    setBusy(false);
    if (error) {
      setErr((error as { detail?: string })?.detail || "作成に失敗しました");
      return;
    }
    setName("");
    setSlug("");
    onCreated();
  }

  return (
    <div className="card" style={{ marginBottom: "1rem", padding: "1rem" }}>
      <div style={{ display: "flex", gap: ".5rem", alignItems: "center", flexWrap: "wrap" }}>
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="クリエイター名" style={{ width: "12rem" }} />
        <input value={slug} onChange={(e) => setSlug(e.target.value)} placeholder="slug (URL用)" style={{ width: "10rem" }} />
        <button className="btn" type="button" onClick={create} disabled={busy || !name || !slug}>
          ＋ 新規作成
        </button>
        {err && <span style={{ color: "var(--warn)", fontSize: ".78rem" }}>{err}</span>}
      </div>
      <p className="muted" style={{ fontSize: ".75rem", marginTop: ".4rem", marginBottom: 0 }}>
        作成すると無料ティア(level0)が自動整備されます。番組(Series)の紐付け・ティア設計・招待は詳細画面から行えます。
      </p>
    </div>
  );
}

export function CreatorsPage() {
  const [creators, setCreators] = useState<CreatorAdminOut[] | null>(null);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");

  const load = useCallback(() => {
    api
      .GET("/api/v1/admin/fanclub/creators")
      .then(({ data, error }) => (error ? setErr("読み込み失敗 (staff 権限が必要)") : data && setCreators(data)))
      .catch(() => setErr("読み込み失敗"));
  }, []);
  useEffect(load, [load]);

  if (err && !creators) return <p className="st-loading">{err}</p>;
  if (!creators) return <EmptyState loading>読み込み中…</EmptyState>;

  return (
    <StudioPage title="クリエイター管理">
      {msg && <Notice variant="success">{msg}</Notice>}
      <NewCreatorForm
        onCreated={() => {
          setMsg("作成しました");
          load();
        }}
      />
      <div className="card">
        <table>
          <thead>
            <tr>
              <th>名前</th>
              <th>slug</th>
              <th style={{ textAlign: "right" }}>番組</th>
              <th style={{ textAlign: "right" }}>会員</th>
              <th style={{ textAlign: "right" }}>稼働中の枠契約</th>
              <th>状態</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {creators.map((c) => (
              <tr key={c.id}>
                <td>{c.name}</td>
                <td className="muted">{c.slug}</td>
                <td style={{ textAlign: "right" }}>{c.series_count}</td>
                <td style={{ textAlign: "right" }}>{c.member_count}</td>
                <td style={{ textAlign: "right" }}>{c.active_contract_count}</td>
                <td>
                  {c.status === "active" ? (
                    <span style={{ color: "#4ade80" }}>● 有効</span>
                  ) : (
                    <span className="muted">○ 停止中</span>
                  )}
                </td>
                <td>
                  <Link to={`/creators/${c.id}`} className="btn" style={{ fontSize: ".78rem", padding: ".2rem .6rem", textDecoration: "none" }}>
                    詳細 ›
                  </Link>
                </td>
              </tr>
            ))}
            {creators.length === 0 && (
              <tr>
                <td colSpan={7} className="muted">
                  クリエイターがまだ登録されていません。
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </StudioPage>
  );
}
