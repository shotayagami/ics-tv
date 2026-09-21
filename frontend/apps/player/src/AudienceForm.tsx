// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * 番組ページ フォーム島 — 投書・キャンペーン応募
 *
 * SeriesDetailOut.forms のリストを /api/v1/series/{id} から取得して描画する。
 * @icstv/ui は import しない (公開アプリは public_base.html 共有層スタイルのみ使用)。
 */

import { useEffect, useRef, useState } from "react";

import type { components } from "@icstv/api";

type AudienceFormPublic = components["schemas"]["AudienceFormPublic"];
type AudienceFieldDef = components["schemas"]["AudienceFieldDef"];

const STYLE = `
  .af-wrap { border-top: 1px solid var(--line, #2a2a2a); margin-top: 8px; }
  .af-section { background: var(--card, #101828); border: 1px solid var(--line, #2a2a2a);
    border-radius: 12px; margin: 16px 0; overflow: hidden; }
  .af-head { padding: 16px 20px 12px; border-bottom: 1px solid var(--line, #2a2a2a); }
  .af-head h2 { font-size: 15px; font-weight: 800; margin: 0 0 4px; }
  .af-desc { font-size: 13px; color: var(--dim, #8899aa); margin-top: 4px; }
  .af-kind-badge { display: inline-block; font-size: 11px; font-weight: 700; border-radius: 4px;
    padding: 2px 7px; margin-right: 8px; }
  .af-kind-message { background: rgba(56,189,248,.14); color: var(--accent, #38bdf8); }
  .af-kind-campaign { background: rgba(251,191,36,.14); color: #fbbf24; }
  .af-prize { font-size: 12px; color: #fbbf24; margin-top: 4px; }
  .af-deadline { font-size: 12px; color: var(--dim, #8899aa); margin-top: 2px; }
  .af-body { padding: 16px 20px 20px; }
  .af-field { margin-bottom: 14px; }
  .af-label { display: block; font-size: 13px; font-weight: 700; margin-bottom: 5px; }
  .af-required { color: var(--danger-text, #f87171); font-size: 11px; margin-left: 4px; }
  .af-help { font-size: 11px; color: var(--dim, #8899aa); margin-top: 3px; }
  .af-input, .af-textarea, .af-select { width: 100%; box-sizing: border-box;
    background: var(--bg, #0a0f24); border: 1px solid var(--line, #2a2a2a); border-radius: 6px;
    color: inherit; font-size: 13px; padding: 8px 10px; outline: none; font-family: inherit; }
  .af-input:focus, .af-textarea:focus, .af-select:focus { border-color: var(--accent, #38bdf8); }
  .af-textarea { resize: vertical; min-height: 80px; }
  .af-checkbox-row { display: flex; align-items: center; gap: 8px; }
  .af-submit { display: block; width: 100%; padding: 10px; background: var(--accent, #38bdf8);
    color: var(--accent-on, #04101a); font-size: 14px; font-weight: 800; border: none;
    border-radius: 8px; cursor: pointer; margin-top: 16px; transition: opacity .15s; }
  .af-submit:disabled { opacity: 0.5; cursor: not-allowed; }
  .af-error { color: var(--danger-text, #f87171); font-size: 13px; margin-top: 8px; }
  .af-success { background: rgba(74,222,128,.12); border: 1px solid rgba(74,222,128,.3);
    border-radius: 8px; padding: 16px 20px; font-size: 14px; font-weight: 700; color: #4ade80;
    margin: 16px 0; }
  .af-login-note { font-size: 13px; color: var(--dim, #8899aa); padding: 16px 20px; }
  .af-login-note a { color: var(--accent, #38bdf8); }
`;

function FieldInput({
  field,
  value,
  onChange,
}: {
  field: AudienceFieldDef;
  value: string | boolean;
  onChange: (v: string | boolean) => void;
}) {
  if (field.type === "textarea") {
    return (
      <textarea
        className="af-textarea"
        value={value as string}
        onChange={(e) => onChange(e.target.value)}
        required={field.required}
      />
    );
  }
  if (field.type === "select") {
    return (
      <select
        className="af-select"
        value={value as string}
        onChange={(e) => onChange(e.target.value)}
        required={field.required}
      >
        <option value="">選択してください</option>
        {field.options.map((o: string) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
    );
  }
  if (field.type === "checkbox") {
    return (
      <div className="af-checkbox-row">
        <input
          type="checkbox"
          checked={value as boolean}
          onChange={(e) => onChange(e.target.checked)}
          required={field.required}
          id={`af-${field.key}`}
        />
        <label htmlFor={`af-${field.key}`} style={{ fontSize: 13, fontWeight: 600 }}>
          {field.label}
        </label>
      </div>
    );
  }
  return (
    <input
      className="af-input"
      type={field.type === "email" ? "email" : field.type === "tel" ? "tel" : field.type === "date" ? "date" : "text"}
      value={value as string}
      onChange={(e) => onChange(e.target.value)}
      required={field.required}
    />
  );
}

function FormSection({
  form,
  csrf,
}: {
  form: AudienceFormPublic;
  csrf: string;
}) {
  const initValues = () =>
    Object.fromEntries(
      form.fields.map((f: AudienceFieldDef) => [f.key, f.type === "checkbox" ? false : ""])
    ) as Record<string, string | boolean>;

  const [values, setValues] = useState<Record<string, string | boolean>>(initValues);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState(false);

  // requires_login なら member 状態を確認 (gate判定はサーバ委任、ここは guest hint のみ)
  const [gate, setGate] = useState<"" | "login">("");

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setSubmitting(true);
    try {
      const res = await fetch(`/api/v1/forms/${form.id}/submit`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": csrf,
        },
        body: JSON.stringify({ payload: values }),
        credentials: "include",
      });
      const json = await res.json();
      if (!res.ok) {
        setError(json.detail ?? "送信に失敗しました。");
        return;
      }
      if (json.gate === "login") {
        setGate("login");
        return;
      }
      setSuccess(true);
    } catch {
      setError("通信エラーが発生しました。");
    } finally {
      setSubmitting(false);
    }
  }

  if (success) {
    return (
      <div className="af-section">
        <div className="af-success">✓ {form.success_message}</div>
      </div>
    );
  }

  return (
    <div className="af-section">
      <div className="af-head">
        <span className={`af-kind-badge af-kind-${form.kind}`}>
          {form.kind === "message" ? "投書・メッセージ" : "キャンペーン応募"}
        </span>
        <h2>{form.title}</h2>
        {form.description && <div className="af-desc">{form.description}</div>}
        {form.prize && <div className="af-prize">🎁 {form.prize}</div>}
        {form.ends_display && (
          <div className="af-deadline">応募締切: {form.ends_display}</div>
        )}
      </div>

      {gate === "login" ? (
        <div className="af-login-note">
          このフォームへの送信には<a href="/members/login/?next=">ログイン</a>が必要です。
        </div>
      ) : (
        <div className="af-body">
          <form onSubmit={handleSubmit} noValidate>
            {form.fields.map((field: AudienceFieldDef) => (
              <div key={field.key} className="af-field">
                {field.type !== "checkbox" && (
                  <label className="af-label">
                    {field.label}
                    {field.required && <span className="af-required">必須</span>}
                  </label>
                )}
                <FieldInput
                  field={field}
                  value={values[field.key] ?? ""}
                  onChange={(v) => setValues((prev) => ({ ...prev, [field.key]: v }))}
                />
                {field.help && <div className="af-help">{field.help}</div>}
              </div>
            ))}
            {error && <div className="af-error">{error}</div>}
            <button type="submit" className="af-submit" disabled={submitting}>
              {submitting ? "送信中…" : "送信する"}
            </button>
          </form>
        </div>
      )}
    </div>
  );
}

export function AudienceFormIsland({
  seriesId,
  csrf,
}: {
  seriesId: number;
  csrf: string;
}) {
  const [forms, setForms] = useState<AudienceFormPublic[]>([]);
  const styleRef = useRef<HTMLStyleElement | null>(null);

  useEffect(() => {
    // インラインスタイル注入
    if (!styleRef.current) {
      const s = document.createElement("style");
      s.textContent = STYLE;
      document.head.appendChild(s);
      styleRef.current = s;
    }
    // SeriesDetailOut から forms を取得
    fetch(`/api/v1/series/${seriesId}`)
      .then((r) => r.json())
      .then((data) => {
        if (Array.isArray(data.forms)) setForms(data.forms);
      })
      .catch(() => {});
  }, [seriesId]);

  if (forms.length === 0) return null;

  return (
    <div className="af-wrap">
      {forms.map((form) => (
        <FormSection key={form.id} form={form} csrf={csrf} />
      ))}
    </div>
  );
}
