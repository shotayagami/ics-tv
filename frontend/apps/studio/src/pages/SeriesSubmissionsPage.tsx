// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * 投稿一覧 — AudienceSubmission のモデレーション画面
 *
 * URL: /series/:channelSlug/submissions/:seriesId/:formId
 */

import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { api } from "@icstv/api";
import { EmptyState, Notice, StudioPage } from "../atoms";

import type { components } from "@icstv/api";

type AudienceSubmissionAdminOut = components["schemas"]["AudienceSubmissionAdminOut"];
type AudienceFieldDef = components["schemas"]["AudienceFieldDef"];

const STATUS_LABELS: Record<string, string> = {
  new: "未読",
  read: "既読",
  handled: "対応済",
};

const STATUS_COLORS: Record<string, string> = {
  new: "#38bdf8",
  read: "#8899aa",
  handled: "#4ade80",
  deleted: "#f87171",
};

function StatusBadge({ status, deleted }: { status: string; deleted: boolean }) {
  const key = deleted ? "deleted" : status;
  const label = deleted ? "非表示" : (STATUS_LABELS[status] ?? status);
  return (
    <span style={{
      fontSize: 11,
      fontWeight: 700,
      borderRadius: 4,
      padding: "2px 7px",
      background: `${STATUS_COLORS[key] ?? "#8899aa"}22`,
      color: STATUS_COLORS[key] ?? "#8899aa",
    }}>
      {label}
    </span>
  );
}

function PayloadTable({ payload, fields }: { payload: Record<string, unknown>; fields: AudienceFieldDef[] }) {
  const fieldMap = Object.fromEntries(fields.map((f) => [f.key, f.label]));
  const entries = Object.entries(payload);
  if (entries.length === 0) return <span style={{ color: "#8899aa", fontSize: 12 }}>（空）</span>;
  return (
    <table style={{ fontSize: 12, borderCollapse: "collapse", width: "100%" }}>
      <tbody>
        {entries.map(([k, v]) => (
          <tr key={k}>
            <td style={{ padding: "2px 8px 2px 0", color: "#8899aa", whiteSpace: "nowrap", verticalAlign: "top", minWidth: 100 }}>
              {fieldMap[k] ?? k}
            </td>
            <td style={{ padding: "2px 0", wordBreak: "break-word" }}>
              {typeof v === "boolean" ? (v ? "✓ はい" : "いいえ") : String(v ?? "")}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function SeriesSubmissionsPage() {
  const { channelSlug = "", seriesId = "", formId = "" } = useParams();

  const [subs, setSubs] = useState<AudienceSubmissionAdminOut[]>([]);
  const [formTitle, setFormTitle] = useState("");
  const [formFields, setFormFields] = useState<AudienceFieldDef[]>([]);
  const [statusFilter, setStatusFilter] = useState<string>("all");
  const [includeDeleted, setIncludeDeleted] = useState(false);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");

  // フォームメタ (title + fields) を forms リストから取得
  useEffect(() => {
    if (!channelSlug || !seriesId) return;
    api
      .GET("/api/v1/admin/scheduling/{slug}/series/{series_id}/forms", {
        params: { path: { slug: channelSlug, series_id: Number(seriesId) } },
      })
      .then(({ data }) => {
        if (!data) return;
        const forms = data as components["schemas"]["AudienceFormAdminOut"][];
        const form = forms.find((f) => String(f.id) === formId);
        if (form) {
          setFormTitle(form.title);
          setFormFields(form.fields as AudienceFieldDef[]);
        }
      })
      .catch(() => {});
  }, [channelSlug, seriesId, formId]);

  const load = useCallback(() => {
    if (!channelSlug || !seriesId || !formId) return;
    setLoading(true);

    const query: Record<string, string> = {};
    if (statusFilter !== "all") query.status = statusFilter;
    if (includeDeleted) query.include_deleted = "true";

    api
      .GET(
        "/api/v1/admin/scheduling/{slug}/series/{series_id}/forms/{form_id}/submissions",
        {
          params: {
            path: { slug: channelSlug, series_id: Number(seriesId), form_id: Number(formId) },
            query,
          },
        }
      )
      .then(({ data, error }) => {
        setLoading(false);
        if (error) setErr("読み込み失敗");
        else setSubs((data as AudienceSubmissionAdminOut[]) ?? []);
      })
      .catch(() => { setLoading(false); setErr("読み込み失敗"); });
  }, [channelSlug, seriesId, formId, statusFilter, includeDeleted]);

  useEffect(load, [load]);

  async function action(subId: number, act: string, label: string, confirm?: string) {
    if (confirm && !window.confirm(confirm)) return;
    const { error } = await api.POST(
      "/api/v1/admin/scheduling/{slug}/series/{series_id}/forms/{form_id}/submissions/{sub_id}",
      {
        params: {
          path: { slug: channelSlug, series_id: Number(seriesId), form_id: Number(formId), sub_id: subId },
          query: { action: act },
        },
      }
    );
    if (error) setErr("操作に失敗しました");
    else { setMsg(`${label}`); load(); }
  }

  function csvUrl() {
    return `/api/v1/admin/scheduling/${channelSlug}/series/${seriesId}/forms/${formId}/submissions.csv`;
  }

  if (loading) return <EmptyState loading>読み込み中…</EmptyState>;

  return (
    <StudioPage
      title={formTitle ? `投稿一覧 — ${formTitle}` : "投稿一覧"}
      actions={
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <Link
            to={`/series/${channelSlug}/forms/${seriesId}`}
            style={{ fontSize: 13, color: "#8899aa", textDecoration: "none" }}
          >
            ‹ フォーム管理
          </Link>
          <a
            href={csvUrl()}
            className="btn"
            style={{ fontSize: 12, padding: "4px 12px", background: "#2a3a5a" }}
            download
          >
            CSV 出力
          </a>
        </div>
      }
    >
      {err && <Notice variant="error">{err}</Notice>}
      {msg && <Notice variant="success">{msg}</Notice>}

      {/* フィルタ */}
      <div style={{ display: "flex", gap: 12, marginBottom: 16, flexWrap: "wrap", alignItems: "center" }}>
        <div style={{ display: "flex", gap: 4 }}>
          {(["all", "new", "read", "handled"] as const).map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => setStatusFilter(s)}
              style={{
                fontSize: 12, padding: "3px 10px", borderRadius: 6, border: "1px solid",
                borderColor: statusFilter === s ? "var(--accent, #38bdf8)" : "#2a2a2a",
                background: statusFilter === s ? "rgba(56,189,248,.14)" : "#0d1530",
                color: statusFilter === s ? "var(--accent, #38bdf8)" : "#8899aa",
                cursor: "pointer",
              }}
            >
              {s === "all" ? "すべて" : STATUS_LABELS[s]}
            </button>
          ))}
        </div>
        <label style={{ fontSize: 12, display: "flex", alignItems: "center", gap: 6, color: "#8899aa" }}>
          <input type="checkbox" checked={includeDeleted} onChange={(e) => setIncludeDeleted(e.target.checked)} />
          非表示も含む
        </label>
        <span style={{ fontSize: 12, color: "#8899aa", marginLeft: "auto" }}>{subs.length} 件</span>
      </div>

      {subs.length === 0 && (
        <div style={{ fontSize: 13, color: "#8899aa", padding: "32px 0", textAlign: "center" }}>
          {statusFilter !== "all" ? `「${STATUS_LABELS[statusFilter]}」の投稿はありません` : "まだ投稿がありません"}
        </div>
      )}

      {subs.map((sub) => (
        <div
          key={sub.id}
          style={{
            border: "1px solid #2a2a2a",
            borderRadius: 10,
            marginBottom: 10,
            background: sub.deleted ? "#0a0a18" : "#0d1530",
            opacity: sub.deleted ? 0.6 : 1,
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "10px 14px", borderBottom: "1px solid #1a2a3a" }}>
            <StatusBadge status={sub.status} deleted={sub.deleted} />
            <span style={{ fontSize: 13, fontWeight: 700, flex: 1 }}>
              {sub.submitter_name || "（匿名）"}
            </span>
            {sub.submitter_email && (
              <span style={{ fontSize: 12, color: "#8899aa" }}>{sub.submitter_email}</span>
            )}
            <span style={{ fontSize: 11, color: "#8899aa", whiteSpace: "nowrap" }}>
              {new Date(sub.created_at).toLocaleString("ja-JP", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })}
            </span>
          </div>

          <div style={{ padding: "10px 14px" }}>
            <PayloadTable payload={sub.payload as Record<string, unknown>} fields={formFields} />
          </div>

          <div style={{ padding: "8px 14px", display: "flex", gap: 6, borderTop: "1px solid #1a2a3a" }}>
            {!sub.deleted && sub.status === "new" && (
              <button type="button" className="btn" style={{ fontSize: 12, padding: "2px 8px" }}
                onClick={() => void action(sub.id, "read", "既読にしました")}>
                既読
              </button>
            )}
            {!sub.deleted && sub.status !== "handled" && (
              <button type="button" className="btn" style={{ fontSize: 12, padding: "2px 8px", background: "rgba(74,222,128,.2)", color: "#4ade80" }}
                onClick={() => void action(sub.id, "handled", "対応済にしました")}>
                対応済
              </button>
            )}
            {!sub.deleted ? (
              <button type="button" className="btn" style={{ fontSize: 12, padding: "2px 8px", background: "#3a1a1a", color: "#f87171" }}
                onClick={() => void action(sub.id, "delete", "非表示にしました", "この投稿を非表示にしますか？")}>
                非表示
              </button>
            ) : (
              <button type="button" className="btn" style={{ fontSize: 12, padding: "2px 8px" }}
                onClick={() => void action(sub.id, "restore", "表示に戻しました")}>
                表示に戻す
              </button>
            )}
          </div>
        </div>
      ))}
    </StudioPage>
  );
}
