// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
/** 時計スタイル上書き — シリーズ/番組フォームで共用するコンパクトな override セクション。
 * value=null → チャンネルデフォルト。value={} or {...} → カスタム設定。 */

import { useEffect, useRef, useState } from "react";

import { CLOCK_FONT_GROUPS } from "./clockFonts";

type StyleDict = Record<string, unknown>;

const DEFAULTS: StyleDict = {
  font_family: "noto-sans-jp",
  time_size: 54,
  font_weight: 700,
  time_color: "#ffffff",
  date_color: "#cfe3ff",
  text_effect: "soft-shadow",
  bg_preset: "dark-gradient",
  bg_opacity: 0.8,
  box_shadow: "md",
  border_radius: "md",
  entrance_anim: "fade",
  position: "top-left",
  show_seconds: false,
  show_date: false,
};

const CORNER_POSITIONS = [
  { value: "top-left",     label: "↖" },
  { value: "top-right",    label: "↗" },
  { value: "bottom-left",  label: "↙" },
  { value: "bottom-right", label: "↘" },
];

interface Preset { id: number; name: string; style: StyleDict }

function PresetLoader({ onLoad }: { onLoad: (style: StyleDict) => void }) {
  const [presets, setPresets] = useState<Preset[]>([]);
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    fetch("/api/v1/admin/clock-presets", { credentials: "same-origin" })
      .then((r) => r.json())
      .then((data) => { if (Array.isArray(data)) setPresets(data as Preset[]); })
      .catch(() => {});
  }, [open]);

  useEffect(() => {
    function onClickOut(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onClickOut);
    return () => document.removeEventListener("mousedown", onClickOut);
  }, []);

  return (
    <div ref={ref} style={{ position: "relative", display: "inline-block" }}>
      <button type="button" className="btn ghost"
        style={{ fontSize: ".75rem", padding: ".2rem .5rem" }}
        onClick={() => setOpen((v) => !v)}>
        プリセットから読込 ▾
      </button>
      {open && (
        <div style={{
          position: "absolute", top: "100%", left: 0, zIndex: 50, marginTop: 2,
          background: "#0d1530", border: "1px solid #2a2a2a", borderRadius: ".4rem",
          minWidth: 180, boxShadow: "0 4px 12px rgba(0,0,0,.4)",
        }}>
          {presets.length === 0
            ? <div style={{ padding: ".5rem .75rem", fontSize: ".8rem", color: "#6b7280" }}>プリセットなし</div>
            : presets.map((p) => (
              <button key={p.id} type="button"
                style={{
                  display: "block", width: "100%", textAlign: "left",
                  padding: ".45rem .75rem", background: "none", border: "none",
                  color: "var(--fg)", cursor: "pointer", fontSize: ".82rem",
                }}
                onMouseEnter={(e) => (e.currentTarget.style.background = "rgba(255,255,255,.05)")}
                onMouseLeave={(e) => (e.currentTarget.style.background = "none")}
                onClick={() => { onLoad(p.style); setOpen(false); }}>
                {p.name}
              </button>
            ))
          }
          <div style={{ borderTop: "1px solid #2a2a2a", padding: ".35rem .75rem" }}>
            <a href="/studio/clock-presets" target="_blank" rel="noreferrer"
              style={{ fontSize: ".75rem", color: "var(--accent)" }}>
              プリセットを管理 ↗
            </a>
          </div>
        </div>
      )}
    </div>
  );
}

export function ClockStyleOverride({
  value,
  onChange,
}: {
  value: StyleDict | null;
  onChange: (v: StyleDict | null) => void;
}) {
  const isCustom = value !== null;
  const s: StyleDict = isCustom ? { ...DEFAULTS, ...value } : DEFAULTS;

  function enable(on: boolean) {
    onChange(on ? { ...DEFAULTS } : null);
  }

  function set(key: string, v: unknown) {
    if (!isCustom) return;
    onChange({ ...s, [key]: v });
  }

  return (
    <details style={{ marginTop: ".8rem" }}>
      <summary style={{ cursor: "pointer", fontSize: ".82rem", fontWeight: 600, color: "var(--fg-muted, #9ba)", userSelect: "none" }}>
        時計スタイル上書き {isCustom ? <span style={{ color: "var(--accent)", marginLeft: ".3rem" }}>カスタム設定中</span> : ""}
      </summary>
      <div style={{ marginTop: ".6rem", padding: ".8rem", background: "#0d1530", borderRadius: ".5rem", border: "1px solid #2a2a2a" }}>
        <div style={{ display: "flex", gap: "1.2rem", marginBottom: ".7rem" }}>
          <label style={{ display: "inline-flex", gap: ".3rem", alignItems: "center", fontSize: ".82rem" }}>
            <input type="radio" checked={!isCustom} onChange={() => enable(false)} />
            チャンネルデフォルト
          </label>
          <label style={{ display: "inline-flex", gap: ".3rem", alignItems: "center", fontSize: ".82rem" }}>
            <input type="radio" checked={isCustom} onChange={() => enable(true)} />
            カスタム設定
          </label>
        </div>

        {isCustom && (
          <>
          <div style={{ marginBottom: ".5rem" }}>
            <PresetLoader onLoad={(style) => onChange({ ...DEFAULTS, ...style })} />
          </div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: ".7rem" }}>
            {/* フォント */}
            <label className="field">
              <span>フォント</span>
              <select value={String(s.font_family)} onChange={(e) => set("font_family", e.target.value)}>
                {CLOCK_FONT_GROUPS.map((g) => (
                  <optgroup key={g.group} label={g.group}>
                    {g.options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                  </optgroup>
                ))}
              </select>
            </label>

            {/* テキストサイズ */}
            <label className="field">
              <span>サイズ (px)</span>
              <input type="number" min={36} max={80} value={Number(s.time_size)} onChange={(e) => set("time_size", Number(e.target.value))} style={{ width: "5rem" }} />
            </label>

            {/* テキストエフェクト */}
            <label className="field">
              <span>テキストエフェクト</span>
              <select value={String(s.text_effect)} onChange={(e) => set("text_effect", e.target.value)}>
                <option value="none">なし</option>
                <option value="soft-shadow">ソフトシャドウ</option>
                <option value="hard-outline">ハードアウトライン</option>
                <option value="glow">グロー</option>
              </select>
            </label>

            {/* 背景 */}
            <label className="field">
              <span>背景</span>
              <select value={String(s.bg_preset)} onChange={(e) => set("bg_preset", e.target.value)}>
                <option value="dark-gradient">グラデーション（既定）</option>
                <option value="dark-solid">ソリッド</option>
                <option value="dark-pill">ピル型ソリッド</option>
                <option value="frosted">すりガラス</option>
                <option value="none">なし（透明）</option>
              </select>
            </label>

            {/* 背景不透明度 */}
            <label className="field">
              <span>背景不透明度 ({Math.round(Number(s.bg_opacity) * 100)}%)</span>
              <input type="range" min={0} max={1} step={0.05}
                value={Number(s.bg_opacity)}
                onChange={(e) => set("bg_opacity", parseFloat(e.target.value))}
                style={{ width: "8rem" }} />
            </label>

            {/* アニメーション */}
            <label className="field">
              <span>アニメーション</span>
              <select value={String(s.entrance_anim)} onChange={(e) => set("entrance_anim", e.target.value)}>
                <option value="none">なし</option>
                <option value="fade">フェード</option>
                <option value="slide-down">スライドダウン</option>
                <option value="zoom">ズーム</option>
              </select>
            </label>

            {/* 位置 */}
            <div className="field">
              <span>位置</span>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: ".2rem", width: "5rem" }}>
                {CORNER_POSITIONS.map((p) => (
                  <button key={p.value} type="button"
                    className={`btn${String(s.position) === p.value ? "" : " ghost"}`}
                    style={{ padding: ".15rem .2rem", fontSize: "1rem", lineHeight: 1 }}
                    onClick={() => set("position", p.value)}>
                    {p.label}
                  </button>
                ))}
              </div>
            </div>

            {/* 色 */}
            <label className="field">
              <span>時刻の色</span>
              <input type="color" value={String(s.time_color)} onChange={(e) => set("time_color", e.target.value)} style={{ width: "3rem", height: "2rem", padding: ".1rem" }} />
            </label>
            <label className="field">
              <span>日付の色</span>
              <input type="color" value={String(s.date_color)} onChange={(e) => set("date_color", e.target.value)} style={{ width: "3rem", height: "2rem", padding: ".1rem" }} />
            </label>

            {/* 表示オプション */}
            <div className="field">
              <span>表示</span>
              <div style={{ display: "flex", flexDirection: "column", gap: ".2rem" }}>
                <label style={{ display: "inline-flex", gap: ".3rem", alignItems: "center", fontSize: ".82rem" }}>
                  <input type="checkbox" checked={!!s.show_seconds} onChange={(e) => set("show_seconds", e.target.checked)} /> 秒を表示
                </label>
                <label style={{ display: "inline-flex", gap: ".3rem", alignItems: "center", fontSize: ".82rem" }}>
                  <input type="checkbox" checked={!!s.show_date} onChange={(e) => set("show_date", e.target.checked)} /> 日付を表示
                </label>
              </div>
            </div>
          </div>
          </>
        )}
        <p className="muted" style={{ fontSize: ".72rem", margin: ".5rem 0 0" }}>
          未指定のキーはチャンネルデフォルト → 時間帯設定の順で補完されます。
        </p>
      </div>
    </details>
  );
}
