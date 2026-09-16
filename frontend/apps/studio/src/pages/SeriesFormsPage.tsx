// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * 番組フォームビルダー — AudienceForm の作成/編集 (#番組ページ一人前化)
 *
 * URL: /series/:slug/forms/:seriesId
 */

import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { api } from "@icstv/api";
import { EmptyState, Notice, StudioPage } from "../atoms";

import type { components } from "@icstv/api";

type AudienceFormAdminOut = components["schemas"]["AudienceFormAdminOut"];
type AudienceFieldDef = components["schemas"]["AudienceFieldDef"];

const FIELD_TYPES = [
  { value: "text", label: "テキスト（1行）" },
  { value: "textarea", label: "テキスト（複数行）" },
  { value: "email", label: "メールアドレス" },
  { value: "tel", label: "電話番号" },
  { value: "select", label: "選択（プルダウン）" },
  { value: "date", label: "日付" },
  { value: "checkbox", label: "チェックボックス" },
];

function newField(): AudienceFieldDef {
  return { key: `field_${Date.now()}`, label: "", type: "text", required: false, options: [], help: "" };
}

function FieldEditor({
  field,
  index,
  total,
  onChange,
  onRemove,
  onMove,
}: {
  field: AudienceFieldDef;
  index: number;
  total: number;
  onChange: (f: AudienceFieldDef) => void;
  onRemove: () => void;
  onMove: (dir: -1 | 1) => void;
}) {
  const set = (patch: Partial<AudienceFieldDef>) => onChange({ ...field, ...patch });

  return (
    <div style={{ border: "1px solid #2a2a2a", borderRadius: 8, padding: "12px 14px", marginBottom: 10, background: "#0d1530" }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 8 }}>
        <span style={{ fontSize: 12, color: "#8899aa", minWidth: 20 }}>{index + 1}</span>
        <input
          style={{ flex: 1, background: "#1c2a4a", border: "1px solid #2a3a5a", borderRadius: 4, color: "inherit", padding: "4px 8px", fontSize: 13 }}
          placeholder="ラベル（表示名）"
          value={field.label}
          onChange={(e) => {
            const label = e.target.value;
            const key = field.key.startsWith("field_") ? `field_${Date.now()}` : field.key;
            set({ label, key });
          }}
        />
        <select
          style={{ background: "#1c2a4a", border: "1px solid #2a3a5a", borderRadius: 4, color: "inherit", padding: "4px 8px", fontSize: 12 }}
          value={field.type}
          onChange={(e) => set({ type: e.target.value as AudienceFieldDef["type"] })}
        >
          {FIELD_TYPES.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
        </select>
        <label style={{ fontSize: 12, display: "flex", alignItems: "center", gap: 4 }}>
          <input type="checkbox" checked={field.required} onChange={(e) => set({ required: e.target.checked })} />
          必須
        </label>
        <button type="button" onClick={() => onMove(-1)} disabled={index === 0} title="上へ" style={{ background: "none", border: "none", cursor: "pointer", color: "#8899aa", fontSize: 16 }}>↑</button>
        <button type="button" onClick={() => onMove(1)} disabled={index === total - 1} title="下へ" style={{ background: "none", border: "none", cursor: "pointer", color: "#8899aa", fontSize: 16 }}>↓</button>
        <button type="button" onClick={onRemove} title="削除" style={{ background: "none", border: "none", cursor: "pointer", color: "#f87171", fontSize: 14 }}>✕</button>
      </div>
      {field.type === "select" && (
        <div style={{ marginTop: 6 }}>
          <label style={{ fontSize: 11, color: "#8899aa" }}>選択肢（カンマ区切り）</label>
          <input
            style={{ display: "block", width: "100%", boxSizing: "border-box", marginTop: 4, background: "#1c2a4a", border: "1px solid #2a3a5a", borderRadius: 4, color: "inherit", padding: "4px 8px", fontSize: 12 }}
            value={field.options.join(",")}
            onChange={(e) => set({ options: e.target.value.split(",").map((s) => s.trim()).filter(Boolean) })}
            placeholder="例: 関東,関西,その他"
          />
        </div>
      )}
      <div style={{ marginTop: 6 }}>
        <input
          style={{ width: "100%", boxSizing: "border-box", background: "#1c2a4a", border: "1px solid #2a3a5a", borderRadius: 4, color: "inherit", padding: "4px 8px", fontSize: 12 }}
          placeholder="ヘルプテキスト（任意）"
          value={field.help ?? ""}
          onChange={(e) => set({ help: e.target.value })}
        />
      </div>
    </div>
  );
}

function FormEditor({
  slug,
  seriesId,
  form,
  onSave,
  onCancel,
}: {
  slug: string;
  seriesId: string;
  form: Partial<AudienceFormAdminOut> | null;
  onSave: () => void;
  onCancel: () => void;
}) {
  const isNew = !form?.id;
  const [kind, setKind] = useState(form?.kind ?? "message");
  const [title, setTitle] = useState(form?.title ?? "");
  const [description, setDescription] = useState(form?.description ?? "");
  const [enabled, setEnabled] = useState(form?.enabled ?? false);
  const [requiresLogin, setRequiresLogin] = useState(form?.requires_login ?? false);
  const [fields, setFields] = useState<AudienceFieldDef[]>((form?.fields as AudienceFieldDef[]) ?? []);
  const [successMessage, setSuccessMessage] = useState(form?.success_message ?? "送信しました。ありがとうございました。");
  const [notifyEmail, setNotifyEmail] = useState(form?.notify_email ?? "");
  const [startsAt, setStartsAt] = useState(form?.starts_at ?? "");
  const [endsAt, setEndsAt] = useState(form?.ends_at ?? "");
  const [prize, setPrize] = useState(form?.prize ?? "");
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState("");

  async function save() {
    setSaving(true);
    setErr("");
    const body = {
      kind,
      title,
      description,
      enabled,
      requires_login: requiresLogin,
      fields,
      success_message: successMessage,
      notify_email: notifyEmail,
      starts_at: startsAt,
      ends_at: endsAt,
      prize,
    };
    let res: { error?: unknown };
    if (isNew) {
      res = await api.POST("/api/v1/admin/scheduling/{slug}/series/{series_id}/forms", {
        params: { path: { slug, series_id: Number(seriesId) } },
        body,
      });
    } else {
      res = await api.PUT("/api/v1/admin/scheduling/{slug}/series/{series_id}/forms/{form_id}", {
        params: { path: { slug, series_id: Number(seriesId), form_id: form!.id! } },
        body,
      });
    }
    setSaving(false);
    if (res.error) {
      setErr("保存に失敗しました。");
    } else {
      onSave();
    }
  }

  const moveField = (i: number, dir: -1 | 1) => {
    setFields((prev) => {
      const next = [...prev];
      const j = i + dir;
      if (j < 0 || j >= next.length) return prev;
      [next[i], next[j]] = [next[j], next[i]];
      return next;
    });
  };

  const row = (label: string, el: React.ReactNode) => (
    <div style={{ marginBottom: 14 }}>
      <label style={{ display: "block", fontSize: 13, fontWeight: 700, marginBottom: 4 }}>{label}</label>
      {el}
    </div>
  );

  const inp = (value: string, onChange: (v: string) => void, placeholder?: string) => (
    <input
      style={{ width: "100%", boxSizing: "border-box", background: "#0a0f24", border: "1px solid #2a2a2a", borderRadius: 6, color: "inherit", padding: "6px 10px", fontSize: 13 }}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
    />
  );

  return (
    <div style={{ background: "#0d1530", border: "1px solid #2a3a5a", borderRadius: 12, padding: 20, marginBottom: 20 }}>
      <h3 style={{ fontSize: 14, fontWeight: 800, marginBottom: 16 }}>{isNew ? "フォームを新規作成" : "フォームを編集"}</h3>

      {row("種別",
        <select style={{ background: "#0a0f24", border: "1px solid #2a2a2a", borderRadius: 6, color: "inherit", padding: "6px 10px", fontSize: 13 }} value={kind} onChange={(e) => { setKind(e.target.value); setRequiresLogin(e.target.value === "campaign"); }}>
          <option value="message">投書・メッセージ</option>
          <option value="campaign">キャンペーン応募</option>
        </select>
      )}
      {row("タイトル", inp(title, setTitle, "例: 番組へメッセージを送る"))}
      {row("説明文（任意）",
        <textarea style={{ width: "100%", boxSizing: "border-box", background: "#0a0f24", border: "1px solid #2a2a2a", borderRadius: 6, color: "inherit", padding: "6px 10px", fontSize: 13, resize: "vertical", minHeight: 60 }}
          value={description} onChange={(e) => setDescription(e.target.value)} />
      )}

      <div style={{ display: "flex", gap: 20, marginBottom: 14 }}>
        <label style={{ fontSize: 13, display: "flex", alignItems: "center", gap: 6 }}>
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
          公開ページに表示（有効）
        </label>
        <label style={{ fontSize: 13, display: "flex", alignItems: "center", gap: 6 }}>
          <input type="checkbox" checked={requiresLogin} onChange={(e) => setRequiresLogin(e.target.checked)} />
          ログイン必須
        </label>
      </div>

      {kind === "campaign" && (
        <>
          {row("賞品・特典（任意）",
            <textarea style={{ width: "100%", boxSizing: "border-box", background: "#0a0f24", border: "1px solid #2a2a2a", borderRadius: 6, color: "inherit", padding: "6px 10px", fontSize: 13, resize: "vertical", minHeight: 50 }}
              value={prize} onChange={(e) => setPrize(e.target.value)} />
          )}
          <div style={{ display: "flex", gap: 12, marginBottom: 14 }}>
            <div style={{ flex: 1 }}>
              <label style={{ display: "block", fontSize: 13, fontWeight: 700, marginBottom: 4 }}>受付開始</label>
              <input type="datetime-local" style={{ width: "100%", boxSizing: "border-box", background: "#0a0f24", border: "1px solid #2a2a2a", borderRadius: 6, color: "inherit", padding: "6px 10px", fontSize: 13 }}
                value={startsAt} onChange={(e) => setStartsAt(e.target.value)} />
            </div>
            <div style={{ flex: 1 }}>
              <label style={{ display: "block", fontSize: 13, fontWeight: 700, marginBottom: 4 }}>受付終了</label>
              <input type="datetime-local" style={{ width: "100%", boxSizing: "border-box", background: "#0a0f24", border: "1px solid #2a2a2a", borderRadius: 6, color: "inherit", padding: "6px 10px", fontSize: 13 }}
                value={endsAt} onChange={(e) => setEndsAt(e.target.value)} />
            </div>
          </div>
        </>
      )}

      <div style={{ marginBottom: 14 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
          <label style={{ fontSize: 13, fontWeight: 700 }}>フォーム項目</label>
          <button type="button" className="btn" style={{ fontSize: 12, padding: "3px 10px" }}
            onClick={() => setFields((prev) => [...prev, newField()])}>
            ＋ 項目を追加
          </button>
        </div>
        {fields.length === 0 && (
          <div style={{ fontSize: 13, color: "#8899aa", padding: "12px 0" }}>項目がありません。「＋ 項目を追加」で追加してください。</div>
        )}
        {fields.map((f, i) => (
          <FieldEditor
            key={f.key}
            field={f}
            index={i}
            total={fields.length}
            onChange={(nf) => setFields((prev) => prev.map((x, j) => (j === i ? nf : x)))}
            onRemove={() => setFields((prev) => prev.filter((_, j) => j !== i))}
            onMove={(dir) => moveField(i, dir)}
          />
        ))}
      </div>

      {row("送信完了メッセージ", inp(successMessage, setSuccessMessage))}
      {row("新着通知メール（空=通知なし。カンマで複数）", inp(notifyEmail, setNotifyEmail, "例: staff@example.com"))}

      {err && <div style={{ color: "#f87171", fontSize: 13, marginBottom: 10 }}>{err}</div>}
      <div style={{ display: "flex", gap: 10 }}>
        <button type="button" className="btn" onClick={save} disabled={saving}>
          {saving ? "保存中…" : "保存"}
        </button>
        <button type="button" className="btn secondary" onClick={onCancel}>
          キャンセル
        </button>
      </div>
    </div>
  );
}

/** フォーム管理の本体 (slug/seriesId を props で受ける)。シリーズ編集ハブのタブからも、
 * 旧スタンドアロンルート (SeriesFormsPage) からも再利用する。 */
export function SeriesFormsPanel({ slug, seriesId }: { slug: string; seriesId: string }) {
  const [forms, setForms] = useState<AudienceFormAdminOut[]>([]);
  const [editing, setEditing] = useState<Partial<AudienceFormAdminOut> | null | false>(false);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [loading, setLoading] = useState(true);

  const load = useCallback(() => {
    if (!slug || !seriesId) return;
    setLoading(true);
    api
      .GET("/api/v1/admin/scheduling/{slug}/series/{series_id}/forms", {
        params: { path: { slug, series_id: Number(seriesId) } },
      })
      .then(({ data, error }) => {
        setLoading(false);
        if (error) setErr("読み込み失敗");
        else setForms((data as AudienceFormAdminOut[]) ?? []);
      })
      .catch(() => { setLoading(false); setErr("読み込み失敗"); });
  }, [slug, seriesId]);

  useEffect(load, [load]);

  async function deleteForm(id: number) {
    if (!window.confirm("このフォームを削除しますか？投稿も全件削除されます。")) return;
    const { error } = await api.DELETE(
      "/api/v1/admin/scheduling/{slug}/series/{series_id}/forms/{form_id}",
      { params: { path: { slug, series_id: Number(seriesId), form_id: id } } }
    );
    if (error) setErr("削除に失敗しました");
    else { setMsg("削除しました"); load(); }
  }

  if (loading) return <EmptyState loading>読み込み中…</EmptyState>;

  return (
    <>
      {err && <Notice variant="error">{err}</Notice>}
      {msg && <Notice variant="success">{msg}</Notice>}

      {editing !== false && (
        <FormEditor
          slug={slug}
          seriesId={seriesId}
          form={editing}
          onSave={() => { setEditing(false); setMsg("保存しました"); load(); }}
          onCancel={() => setEditing(false)}
        />
      )}

      {!editing && (
        <button type="button" className="btn" style={{ marginBottom: 16 }}
          onClick={() => setEditing(null)}>
          ＋ フォームを追加
        </button>
      )}

      {forms.length === 0 && !editing && (
        <div style={{ fontSize: 13, color: "#8899aa", padding: "24px 0" }}>
          フォームがまだありません。「＋ フォームを追加」から作成してください。
        </div>
      )}

      {forms.map((f) => (
        <div key={f.id} style={{ border: "1px solid #2a2a2a", borderRadius: 10, padding: "14px 16px", marginBottom: 12, background: "#0d1530" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8 }}>
            <span style={{
              fontSize: 11, fontWeight: 700, borderRadius: 4, padding: "2px 7px",
              background: f.kind === "campaign" ? "rgba(251,191,36,.14)" : "rgba(56,189,248,.14)",
              color: f.kind === "campaign" ? "#fbbf24" : "var(--accent, #38bdf8)",
            }}>
              {f.kind === "message" ? "投書" : "キャンペーン"}
            </span>
            <strong style={{ fontSize: 14, flex: 1 }}>{f.title}</strong>
            <span style={{ fontSize: 12, color: f.enabled ? "#4ade80" : "#8899aa" }}>
              {f.enabled ? "● 有効" : "○ 無効"}
            </span>
            <span style={{ fontSize: 12, color: "#8899aa" }}>
              投稿 {f.submission_count}件
              {f.new_count > 0 && <span style={{ color: "#38bdf8", marginLeft: 4 }}>（未読 {f.new_count}）</span>}
            </span>
          </div>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <button type="button" className="btn" style={{ fontSize: 12, padding: "3px 10px" }}
              onClick={() => setEditing(f)}>
              編集
            </button>
            <Link
              to={`/series/${slug}/submissions/${seriesId}/${f.id}`}
              className="btn secondary"
              style={{ fontSize: 12, padding: "3px 10px", textDecoration: "none" }}
            >
              投稿一覧
            </Link>
            <button type="button" className="btn" style={{ fontSize: 12, padding: "3px 10px", background: "#3a1a1a", color: "#f87171" }}
              onClick={() => void deleteForm(f.id)}>
              削除
            </button>
          </div>
        </div>
      ))}
    </>
  );
}

/** 旧スタンドアロンルート /series/:slug/forms/:seriesId (ハブからのリンク先互換)。 */
export function SeriesFormsPage() {
  const { slug = "", seriesId = "" } = useParams();
  return (
    <StudioPage
      title="フォーム管理"
      actions={
        <Link
          to={`/series/${slug}/edit/${seriesId}?tab=forms`}
          style={{ fontSize: 13, color: "#8899aa", textDecoration: "none" }}
        >
          ‹ シリーズ編集
        </Link>
      }
    >
      <SeriesFormsPanel slug={slug} seriesId={seriesId} />
    </StudioPage>
  );
}
