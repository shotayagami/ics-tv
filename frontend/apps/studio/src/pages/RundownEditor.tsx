// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useRef, useState } from "react";

import { Notice } from "../atoms";
import type { LiveCueRow } from "../hooks";
import { postJson } from "../hooks";

/** M:SS 表示 (常に非負)。 */
export function fmtDur(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function msFromMinSec(min: FormDataEntryValue | null, sec: FormDataEntryValue | null): number {
  return (Number(min) || 0) * 60000 + (Number(sec) || 0) * 1000;
}

export interface AutoFireFields {
  auto_fire: boolean;
  auto_offset_ms: number;
  auto_anchor: string;
  auto_wall_time: string;
}

/** 自動発火チェックボックス + アンカー選択 (開始相対=分/秒 offset / 壁時計=時刻) (Phase2 §5・
 * Phase3 D1 §11)。CM/VT の pending cue でのみ表示する (呼び出し側でガード済み)。入力は
 * defaultValue の非制御 (add-cue フォームと同じ流儀)、チェック/アンカーは即時 onChange、
 * 分/秒・時刻は onBlur で確定して update を叩く (1文字ごとの過剰な通信を避ける)。 */
function AutoFireCell({
  cue,
  busy,
  onSave,
}: {
  cue: LiveCueRow;
  busy: boolean;
  onSave: (cue: LiveCueRow, fields: AutoFireFields) => void;
}) {
  const minRef = useRef<HTMLInputElement>(null);
  const secRef = useRef<HTMLInputElement>(null);
  const timeRef = useRef<HTMLInputElement>(null);
  const [anchor, setAnchor] = useState(cue.auto_anchor || "start");
  const offsetMs = cue.auto_offset_ms ?? 0;
  const initMin = Math.floor(offsetMs / 60000);
  const initSec = Math.floor((offsetMs % 60000) / 1000);

  // 「現在の DOM 上の値」を読む (クロージャの古い値ではなく)。チェックボックス操作が未確定
  // (onBlur 前) の入力を古い値へ巻き戻して上書きしてしまう事故を防ぐ (2026-07-04 レビュー指摘)。
  function commit(next: Partial<AutoFireFields> = {}) {
    onSave(cue, {
      auto_fire: next.auto_fire ?? cue.auto_fire,
      auto_anchor: next.auto_anchor ?? anchor,
      auto_offset_ms: msFromMinSec(minRef.current?.value ?? null, secRef.current?.value ?? null),
      auto_wall_time: timeRef.current?.value ?? cue.auto_wall_time,
    });
  }

  return (
    <span style={{ display: "inline-flex", gap: ".3rem", alignItems: "center", flexWrap: "wrap" }}>
      <label style={{ display: "inline-flex", alignItems: "center", gap: ".2rem" }}>
        <input
          type="checkbox"
          disabled={busy}
          checked={cue.auto_fire}
          onChange={(e) => commit({ auto_fire: e.currentTarget.checked })}
        />
        自動
      </label>
      <select
        disabled={busy || !cue.auto_fire}
        value={anchor}
        onChange={(e) => {
          const v = e.currentTarget.value;
          setAnchor(v);
          // 壁時計へ切替時に時刻未設定なら POST を保留 (時刻入力の onBlur で確定)。
          // 未設定のまま送ると backend が 422 (auto_wall_time 必須) を返すのを避ける。
          if (v === "start" || timeRef.current?.value || cue.auto_wall_time) {
            commit({ auto_anchor: v });
          }
        }}
        style={{ fontSize: ".78rem" }}
      >
        <option value="start">開始相対</option>
        <option value="wallclock">壁時計</option>
      </select>
      {anchor === "wallclock" ? (
        <input
          ref={timeRef}
          type="time"
          disabled={busy || !cue.auto_fire}
          defaultValue={cue.auto_wall_time || ""}
          onBlur={() => commit()}
          style={{ width: "6.5rem" }}
        />
      ) : (
        <>
          <input ref={minRef} type="number" min={0} disabled={busy || !cue.auto_fire} defaultValue={initMin} onBlur={() => commit()} style={{ width: "3.2rem" }} />
          分
          <input ref={secRef} type="number" min={0} max={59} disabled={busy || !cue.auto_fire} defaultValue={initSec} onBlur={() => commit()} style={{ width: "3.2rem" }} />
          秒
        </>
      )}
    </span>
  );
}

/** 進行表エディタが扱う共通データ (LiveRundownOut / RundownTemplateOut の共通部分)。 */
export interface RundownEditorData {
  cues: LiveCueRow[];
  bundles: { id: number; name: string }[];
  assets: { id: number; name: string }[];
  planned_total_ms: number;
  over_under_ms: number;
}

/** 進行表エディタ (生キューシート / 定番進行表テンプレ 共通・タイムキープ Phase1/Phase3 C)。
 * LiveRundownPage (生番組の LiveRundown) と RundownTemplatePage (SeriesSlot の雛形) の両方が
 * これを使う。両者は書き込み先 URL だけが違うので、addUrl / cueUrl で注入する。読み込み (typed
 * api.GET) と StudioPage シェルは各ページが持つ (data + reload を渡す)。
 * showState: 雛形は state を持たないため 状態 列を出さない。 */
export function RundownEditor({
  data,
  reload,
  addUrl,
  cueUrl,
  showState = true,
}: {
  data: RundownEditorData;
  reload: () => void;
  addUrl: string;
  cueUrl: (cueId: number, action: "update" | "move" | "delete") => string;
  showState?: boolean;
}) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const overUnder = data.over_under_ms;

  async function op(url: string, body: Record<string, unknown>, okMsg: string) {
    if (busy) return;
    setBusy(true);
    setErr("");
    const { status, data: res } = await postJson(url, body);
    setBusy(false);
    if ((status === 200 && res?.ok) || status === 204) {
      setMsg(okMsg);
      reload();
    } else setErr(res?.error || "操作に失敗しました");
  }

  function move(cueId: number, direction: "up" | "down") {
    op(cueUrl(cueId, "move"), { direction }, "並べ替えました");
  }
  function del(cueId: number) {
    if (!window.confirm("この cue を削除しますか？")) return;
    op(cueUrl(cueId, "delete"), {}, "削除しました");
  }

  // 自動発火の設定変更。update はフルリプレース式 (全項目を毎回送る必要がある) なので、
  // cue の既存値をそのまま再送し、auto_fire 系だけを差し替える。
  function updateAutoFire(cue: LiveCueRow, f: AutoFireFields) {
    op(
      cueUrl(cue.id, "update"),
      {
        label: cue.label,
        planned_duration_ms: cue.planned_duration_ms,
        cm_bundle_id: cue.cm_bundle_id,
        grid: cue.grid,
        asset_id: cue.asset_id,
        auto_fire: f.auto_fire,
        auto_offset_ms: f.auto_offset_ms,
        auto_anchor: f.auto_anchor,
        auto_wall_time: f.auto_wall_time,
      },
      "自動発火設定を更新しました",
    );
  }

  function addSection(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    op(
      addUrl,
      { kind: "section", label: String(f.get("label") || ""), planned_duration_ms: msFromMinSec(f.get("min"), f.get("sec")) },
      "本編を追加しました",
    );
    e.currentTarget.reset();
  }
  function addCm(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    op(
      addUrl,
      {
        kind: "cm",
        label: String(f.get("label") || ""),
        planned_duration_ms: msFromMinSec(f.get("min"), f.get("sec")),
        cm_bundle_id: Number(f.get("cm_bundle_id")) || null,
        grid: String(f.get("grid") || "15s"),
      },
      "CM枠を追加しました",
    );
    e.currentTarget.reset();
  }
  function addVt(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    op(
      addUrl,
      {
        kind: "vt",
        label: String(f.get("label") || ""),
        planned_duration_ms: msFromMinSec(f.get("min"), f.get("sec")),
        asset_id: Number(f.get("asset_id")) || null,
      },
      "VTを追加しました",
    );
    e.currentTarget.reset();
  }

  const colSpan = showState ? 8 : 7;

  return (
    <>
      {(msg || err) && <Notice variant={err ? "error" : "success"}>{err || msg}</Notice>}
      <div className="card" style={{ marginBottom: "1rem" }}>
        <table>
          <thead>
            <tr>
              <th>#</th>
              <th>種別</th>
              <th>ラベル</th>
              <th>尺</th>
              <th>割付</th>
              {showState && <th>状態</th>}
              <th>自動発火</th>
              <th>並べ替え/削除</th>
            </tr>
          </thead>
          <tbody>
            {data.cues.map((cue, i) => (
              <tr key={cue.id}>
                <td className="muted">{cue.seq}</td>
                <td>{cue.kind_label}</td>
                <td>{cue.label || <span className="muted">—</span>}</td>
                <td style={{ whiteSpace: "nowrap" }}>{fmtDur(cue.planned_duration_ms)}</td>
                <td>
                  {cue.kind === "cm" ? (
                    cue.cm_bundle_name || <span className="muted">動的({cue.grid})</span>
                  ) : cue.kind === "vt" ? (
                    cue.asset_title
                  ) : (
                    <span className="muted">—</span>
                  )}
                </td>
                {showState && <td>{cue.state_label}</td>}
                <td style={{ whiteSpace: "nowrap" }}>
                  {cue.kind !== "section" && cue.state === "pending" ? (
                    // key に auto_offset_ms を含める: 分/秒は非制御入力 (defaultValue) のため、
                    // 他端末の編集等で外部から値が変わった場合に再マウントして追従させる。
                    <AutoFireCell
                      key={`${cue.id}:${cue.auto_anchor}:${cue.auto_offset_ms ?? 0}:${cue.auto_wall_time}`}
                      cue={cue}
                      busy={busy}
                      onSave={updateAutoFire}
                    />
                  ) : (
                    <span className="muted">—</span>
                  )}
                </td>
                <td style={{ whiteSpace: "nowrap" }}>
                  <button
                    className="btn"
                    type="button"
                    disabled={busy || i === 0 || cue.state !== "pending"}
                    onClick={() => move(cue.id, "up")}
                    style={{ fontSize: ".72rem", padding: "0 .4rem" }}
                  >
                    ↑
                  </button>
                  <button
                    className="btn"
                    type="button"
                    disabled={busy || i === data.cues.length - 1 || cue.state !== "pending"}
                    onClick={() => move(cue.id, "down")}
                    style={{ fontSize: ".72rem", padding: "0 .4rem", marginLeft: ".2rem" }}
                  >
                    ↓
                  </button>
                  <button
                    className="btn"
                    type="button"
                    disabled={busy || cue.state !== "pending"}
                    onClick={() => del(cue.id)}
                    style={{ fontSize: ".72rem", padding: "0 .4rem", marginLeft: ".3rem", background: "var(--warn)" }}
                  >
                    ×
                  </button>
                </td>
              </tr>
            ))}
            {data.cues.length === 0 && (
              <tr>
                <td colSpan={colSpan} className="muted">
                  行がありません。下で本編/CM/VTを追加します。
                </td>
              </tr>
            )}
          </tbody>
        </table>
        <p className="muted" style={{ fontSize: ".8rem", margin: ".4rem 0 0" }}>
          予定尺合計 {fmtDur(data.planned_total_ms)} · 枠 {fmtDur(data.planned_total_ms - overUnder)}
          {overUnder !== 0 && (
            <>
              {" · "}
              <span style={{ color: overUnder > 0 ? "var(--warn)" : "var(--accent)" }}>
                {overUnder > 0 ? "押し" : "巻き"} {fmtDur(Math.abs(overUnder))}
              </span>
            </>
          )}
        </p>
        <p className="muted" style={{ fontSize: ".8rem", margin: ".2rem 0 0" }}>
          自動発火: 開始が遅れるとズレるため、固定時刻の cue のみ推奨。
        </p>
      </div>

      <div className="st-grid">
        <form onSubmit={addSection} className="card">
          <h3 style={{ margin: "0 0 .4rem" }}>本編を追加</h3>
          <span style={{ display: "inline-flex", gap: ".3rem", alignItems: "center", flexWrap: "wrap" }}>
            <input type="text" name="label" placeholder="ラベル (任意)" style={{ width: "9rem" }} />
            <input type="number" name="min" min={0} placeholder="分" style={{ width: "4rem" }} /> 分
            <input type="number" name="sec" min={0} max={59} placeholder="秒" style={{ width: "4rem" }} /> 秒
            <button className="btn" type="submit" disabled={busy}>＋本編</button>
          </span>
        </form>
        <form onSubmit={addCm} className="card">
          <h3 style={{ margin: "0 0 .4rem" }}>CM枠を追加</h3>
          <span style={{ display: "inline-flex", gap: ".3rem", alignItems: "center", flexWrap: "wrap" }}>
            <input type="text" name="label" placeholder="ラベル (任意)" style={{ width: "9rem" }} />
            <select name="cm_bundle_id" defaultValue="">
              <option value="">— 未割付 (grid で動的充填) —</option>
              {data.bundles.map((b) => (
                <option key={b.id} value={b.id}>{b.name}</option>
              ))}
            </select>
            <select name="grid" defaultValue="15s">
              <option value="15s">15秒グリッド</option>
              <option value="20s">20秒グリッド</option>
            </select>
            <input type="number" name="min" min={0} placeholder="分" style={{ width: "4rem" }} /> 分
            <input type="number" name="sec" min={0} max={59} placeholder="秒" style={{ width: "4rem" }} /> 秒
            <button className="btn" type="submit" disabled={busy}>＋CM枠</button>
          </span>
        </form>
        <form onSubmit={addVt} className="card">
          <h3 style={{ margin: "0 0 .4rem" }}>VTを追加</h3>
          <span style={{ display: "inline-flex", gap: ".3rem", alignItems: "center", flexWrap: "wrap" }}>
            <input type="text" name="label" placeholder="ラベル (任意)" style={{ width: "9rem" }} />
            <select name="asset_id" defaultValue="">
              <option value="">— 素材を選択 —</option>
              {data.assets.map((a) => (
                <option key={a.id} value={a.id}>{a.name}</option>
              ))}
            </select>
            <input type="number" name="min" min={0} placeholder="分" style={{ width: "4rem" }} /> 分
            <input type="number" name="sec" min={0} max={59} placeholder="秒" style={{ width: "4rem" }} /> 秒
            <button className="btn" type="submit" disabled={busy}>＋VT</button>
          </span>
        </form>
      </div>
    </>
  );
}
