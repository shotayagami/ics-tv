// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { type CSSProperties, useEffect, useRef, useState } from "react";

import { api } from "@icstv/api";
import type { components } from "@icstv/api";
import { SegmentedNav } from "./atoms";

type GuideOut = components["schemas"]["GuideOut"];
type GuideBlock = components["schemas"]["GuideBlock"];

// 縦密度 (px/分) のズーム。− / ⟲ / + で調整し localStorage に保持 (日/週で共有キー)。
// 幾何は server の素データ (start_min/dur_min/now_min) から client がこの密度で再計算する。
// 定数は core.epg.{DAY_MIN,MIN_BLOCK_H,BLOCK_GAP} と一致させる。
const DAY_MIN = 1440;
const MIN_BLOCK_H = 30;
const BLOCK_GAP = 4;
const PPM_KEY = "icstv.guide.ppm";
const PPM_MIN = 1.0;
const PPM_MAX = 6.0;
const PPM_STEP = 0.4;

const clampPpm = (v: number) => Math.min(PPM_MAX, Math.max(PPM_MIN, Math.round(v * 100) / 100));

function loadPpm(): number | null {
  try {
    const v = parseFloat(localStorage.getItem(PPM_KEY) ?? "");
    return Number.isFinite(v) ? clampPpm(v) : null;
  } catch {
    return null;
  }
}

function savePpm(v: number) {
  try {
    localStorage.setItem(PPM_KEY, String(v));
  } catch {
    /* localStorage 不可 (プライベートモード等) でもズーム自体は機能させる */
  }
}

// 高さからブロック本文の表示量を決める (短尺は時刻+タイトル優先でジャンルを畳む)。
function blockTier(height: number): string {
  if (height < 38) return "blk tiny";
  if (height < 56) return "blk compact";
  return "blk";
}

function blockStyle(top: number, height: number, b: GuideBlock): CSSProperties {
  return {
    position: "absolute",
    left: 6,
    right: 6,
    top,
    height,
    padding: "8px 11px",
    borderRadius: 9,
    overflow: "hidden",
    background: b.bg,
    border: `1px solid ${b.border}`,
    borderLeft: `4px solid ${b.color}`,
    boxShadow: b.shadow,
    textDecoration: "none",
    color: "inherit",
    display: "block",
  };
}

export function GuidePage({ homeBase }: { homeBase: string }) {
  // 表示日は URL の ?date= を真実とする。CDN/bfcache で焼かれた data-base-date が古くても、
  // ?date= が無ければ空文字で取得してサーバの当日 (today) を採用するため、常に当日で開く (#2)。
  const [date, setDate] = useState(() => new URLSearchParams(location.search).get("date") ?? "");
  const [data, setData] = useState<GuideOut | null>(null);
  const [ppm, setPpmState] = useState<number | null>(loadPpm);
  const scrollRef = useRef<HTMLDivElement>(null);
  const scrolledRef = useRef(false);

  useEffect(() => {
    let alive = true;
    api.GET("/api/v1/guide", { params: { query: { date } } }).then(({ data }) => {
      if (alive && data) setData(data);
    });
    return () => {
      alive = false;
    };
  }, [date]);

  // 戻る/進むで ?date= が変わったら追従
  useEffect(() => {
    const onPop = () => setDate(new URLSearchParams(location.search).get("date") ?? "");
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  // 保存済みのズーム設定が無ければ server の既定密度を採用する
  useEffect(() => {
    if (data && ppm == null) setPpmState(data.px_per_min);
  }, [data, ppm]);

  const scale = ppm ?? data?.px_per_min ?? 2.2;

  // 初回ロードで現在時刻付近へ自動スクロール (デザイン: nowTop - 150)
  useEffect(() => {
    if (!data || scrolledRef.current || !data.show_now || data.now_min == null) return;
    const el = scrollRef.current;
    if (!el) return;
    scrolledRef.current = true;
    const top = data.now_min * scale;
    requestAnimationFrame(() => {
      el.scrollTop = Math.max(0, top - 150);
    });
  }, [data, scale]);

  function nav(to: string) {
    setData(null);
    setDate(to);
    history.pushState(null, "", to ? `${location.pathname}?date=${to}` : location.pathname);
  }

  function setPpm(v: number) {
    const c = clampPpm(v);
    setPpmState(c);
    savePpm(c);
  }

  if (!data) {
    return (
      <section className="guide-page">
        <div className="guide-head">
          <h1>番組表</h1>
        </div>
        <div className="sk sk-guide" aria-hidden="true" />
      </section>
    );
  }

  return (
    <section className="guide-page">
      <div className="guide-head">
        <h1>番組表</h1>
        <SegmentedNav
          ariaLabel="表示"
          items={[
            { key: "day", label: "日", selected: true, href: `${homeBase}/guide/` },
            { key: "week", label: "週", href: `${homeBase}/guide/week/` },
          ]}
        />
        <SegmentedNav
          ariaLabel="日付"
          items={[
            {
              key: "prev",
              label: "‹ 前日",
              href: `?date=${data.prev_date}`,
              onClick: (e) => (e.preventDefault(), nav(data.prev_date)),
            },
            {
              key: "today",
              label: "今日",
              selected: true,
              href: `?date=${data.today}`,
              onClick: (e) => (e.preventDefault(), nav(data.today)),
            },
            {
              key: "next",
              label: "翌日 ›",
              href: `?date=${data.next_date}`,
              onClick: (e) => (e.preventDefault(), nav(data.next_date)),
            },
          ]}
        />
        <span className="dlabel">
          {data.label}
          {data.is_today ? " ・本日" : ""}
        </span>
        <div className="guide-tools">
          <div className="zoom" role="group" aria-label="番組表の高さ">
            <button
              type="button"
              className="zbtn"
              aria-label="縮小"
              disabled={scale <= PPM_MIN + 1e-9}
              onClick={() => setPpm(scale - PPM_STEP)}
            >
              −
            </button>
            <button
              type="button"
              className="zbtn zreset"
              aria-label="標準の高さに戻す"
              title="標準の高さ"
              onClick={() => setPpm(data.px_per_min)}
            >
              ⟲
            </button>
            <button
              type="button"
              className="zbtn"
              aria-label="拡大"
              disabled={scale >= PPM_MAX - 1e-9}
              onClick={() => setPpm(scale + PPM_STEP)}
            >
              ＋
            </button>
          </div>
          {data.show_now && (
            <span className="nowclock">
              <i />
              現在時刻 {data.now_label}
            </span>
          )}
        </div>
      </div>

      <div className="grid">
        <div className="grid-cols">
          <div className="axis-sp" />
          {data.cols.map((col, i) => (
            <div className="col" key={i}>
              <span className="dot" style={{ background: col.tint }} />
              <span className="nm">{col.name}</span>
              <span className="lv">
                <span className="pulse-dot" />
                LIVE
              </span>
            </div>
          ))}
        </div>
        <div className="grid-scroll" ref={scrollRef}>
          <div className="grid-inner" style={{ height: DAY_MIN * scale }}>
            <div className="axis">
              {data.hours.map((h, i) => (
                <div className="h" style={{ top: i * 60 * scale }} key={h.label}>
                  {h.label}
                </div>
              ))}
            </div>
            {data.cols.map((col, i) => (
              <div className="gcol" key={i}>
                {col.blocks.map((b) => {
                  const top = b.start_min * scale;
                  const height = Math.max(b.dur_min * scale - BLOCK_GAP, MIN_BLOCK_H);
                  const tip = `${b.time} ${b.title}${b.genre ? ` / ${b.genre}` : ""}`;
                  const inner = (
                    <>
                      <div className="bt">
                        <span className="tm" style={{ color: b.time_color }}>
                          {b.time}
                        </span>
                        {b.is_live && <span className="on">ON AIR</span>}
                        {b.is_rerun && <span className="rerun-tag">再放送</span>}
                      </div>
                      <div className="btl" style={{ color: b.title_color }}>
                        {b.title}
                      </div>
                      {b.genre && (
                        <div className="bg" style={{ color: b.color }}>
                          {b.genre}
                        </div>
                      )}
                    </>
                  );
                  // 再放送ブロックは編成 Program ではない合成行 → 番組詳細リンクを張らず div で出す。
                  const key = `${b.is_rerun ? "r" : "p"}${b.id}-${Math.round(b.start_min)}`;
                  return b.is_rerun ? (
                    <div className={blockTier(height)} key={key} title={tip} style={blockStyle(top, height, b)}>
                      {inner}
                    </div>
                  ) : (
                    <a
                      className={blockTier(height)}
                      key={key}
                      title={tip}
                      href={`${homeBase}/program/${b.id}/`}
                      style={blockStyle(top, height, b)}
                    >
                      {inner}
                    </a>
                  );
                })}
              </div>
            ))}
            {data.show_now && data.now_min != null && (
              <div className="nowline" style={{ top: data.now_min * scale }}>
                <span className="pill">{data.now_label}</span>
              </div>
            )}
          </div>
        </div>
      </div>
    </section>
  );
}
