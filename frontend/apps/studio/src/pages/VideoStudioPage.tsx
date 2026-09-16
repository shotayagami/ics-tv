// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useState } from "react";
import { Player } from "@remotion/player";
import type { ComponentType } from "react";

import { StudioPage } from "../atoms";
import { LowerThird, LOWER_THIRD_DEFAULT_PROPS } from "../remotion/compositions/LowerThird";
import { TitleCard, TITLE_CARD_DEFAULT_PROPS } from "../remotion/compositions/TitleCard";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyProps = Record<string, any>;

interface CompositionDef {
  id: string;
  label: string;
  component: ComponentType<AnyProps>;
  durationInFrames: number;
  fps: number;
  width: number;
  height: number;
  defaultProps: AnyProps;
}

const FPS = 30;

const COMPOSITIONS: CompositionDef[] = [
  {
    id: "title-card",
    label: "タイトルカード",
    component: TitleCard as ComponentType<AnyProps>,
    durationInFrames: FPS * 5,
    fps: FPS,
    width: 1280,
    height: 720,
    defaultProps: TITLE_CARD_DEFAULT_PROPS,
  },
  {
    id: "lower-third",
    label: "Lサード（名前スーパー）",
    component: LowerThird as ComponentType<AnyProps>,
    durationInFrames: FPS * 5,
    fps: FPS,
    width: 1280,
    height: 720,
    defaultProps: LOWER_THIRD_DEFAULT_PROPS,
  },
];

export function VideoStudioPage() {
  const [selectedId, setSelectedId] = useState(COMPOSITIONS[0].id);
  const [propsJson, setPropsJson] = useState(() => JSON.stringify(COMPOSITIONS[0].defaultProps, null, 2));
  const [jsonError, setJsonError] = useState<string | null>(null);

  const comp = COMPOSITIONS.find((c) => c.id === selectedId) ?? COMPOSITIONS[0];

  let inputProps: AnyProps = comp.defaultProps;
  try {
    const parsed = JSON.parse(propsJson);
    inputProps = parsed;
  } catch {
    // keep last valid
  }

  function handleCompositionChange(id: string) {
    const next = COMPOSITIONS.find((c) => c.id === id);
    if (!next) return;
    setSelectedId(id);
    setPropsJson(JSON.stringify(next.defaultProps, null, 2));
    setJsonError(null);
  }

  function handlePropsChange(val: string) {
    setPropsJson(val);
    try {
      JSON.parse(val);
      setJsonError(null);
    } catch (e) {
      setJsonError((e as Error).message);
    }
  }

  return (
    <StudioPage title="動画スタジオ">
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "280px 1fr",
          gap: "1.4rem",
          alignItems: "start",
          marginTop: "1rem",
        }}
      >
        {/* ── 左: コンポジション選択 + props editor ── */}
        <div style={{ display: "flex", flexDirection: "column", gap: ".9rem" }}>
          <div>
            <label style={{ display: "block", fontSize: ".78rem", color: "var(--fg-muted)", marginBottom: ".3rem" }}>
              コンポジション
            </label>
            <select
              value={selectedId}
              onChange={(e) => handleCompositionChange(e.target.value)}
              style={{
                width: "100%",
                padding: ".4rem .6rem",
                background: "var(--surface-2, #1a2332)",
                border: "1px solid var(--line, #2a3a4a)",
                borderRadius: 4,
                color: "var(--fg)",
                fontSize: ".88rem",
              }}
            >
              {COMPOSITIONS.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.label}
                </option>
              ))}
            </select>
          </div>

          <div>
            <label style={{ display: "block", fontSize: ".78rem", color: "var(--fg-muted)", marginBottom: ".3rem" }}>
              Props（JSON）
            </label>
            <textarea
              value={propsJson}
              onChange={(e) => handlePropsChange(e.target.value)}
              rows={14}
              spellCheck={false}
              style={{
                width: "100%",
                fontFamily: "monospace",
                fontSize: ".75rem",
                lineHeight: 1.55,
                padding: ".5rem .6rem",
                background: "var(--surface-2, #1a2332)",
                border: `1px solid ${jsonError ? "var(--danger, #c0392b)" : "var(--line, #2a3a4a)"}`,
                borderRadius: 4,
                color: "var(--fg)",
                resize: "vertical",
                boxSizing: "border-box",
              }}
            />
            {jsonError && (
              <p style={{ color: "var(--danger, #c0392b)", fontSize: ".72rem", marginTop: ".2rem", fontFamily: "monospace" }}>
                {jsonError}
              </p>
            )}
          </div>

          <p style={{ fontSize: ".72rem", color: "var(--fg-muted)", lineHeight: 1.6, margin: 0 }}>
            {comp.width}×{comp.height} / {comp.fps}fps / {(comp.durationInFrames / comp.fps).toFixed(1)}秒
            <br />
            <span style={{ opacity: .7 }}>
              ローカル開発時のみ: <code>npm run studio</code> → localhost:3000
            </span>
          </p>
        </div>

        {/* ── 右: プレイヤー ── */}
        <div>
          <Player
            component={comp.component}
            durationInFrames={comp.durationInFrames}
            fps={comp.fps}
            compositionWidth={comp.width}
            compositionHeight={comp.height}
            style={{ width: "100%", borderRadius: 6, overflow: "hidden" }}
            controls
            inputProps={inputProps}
          />
        </div>
      </div>
    </StudioPage>
  );
}
