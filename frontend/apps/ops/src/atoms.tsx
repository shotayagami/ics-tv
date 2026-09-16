// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
// ops UI アトム: presentational のみ — ロジックは呼び出し側が保持する

import type { ComponentType, ReactNode } from "react";

import { useNowTick } from "./hooks";

// ── LinkLike ──────────────────────────────────────────────────────────────────

export type LinkLike = ComponentType<{ href: string; className?: string; children: ReactNode }>;

export const DefaultLink: LinkLike = ({ href, className, children }) => (
  <a href={href} className={className}>{children}</a>
);

// ── EmptyState ────────────────────────────────────────────────────────────────

export function EmptyState({ children, loading = false }: { children?: ReactNode; loading?: boolean }) {
  if (loading) return <p className="st-loading">{children ?? "読み込み中…"}</p>;
  return <div className="st-empty">{children ?? "データがありません。"}</div>;
}

// ── StatusBadge ───────────────────────────────────────────────────────────────

export type StatusTone = "neutral" | "ok" | "warn" | "danger";

export function StatusBadge({ label, tone = "neutral" }: { label: string; tone?: StatusTone }) {
  return <span className={tone === "neutral" ? "st-badge" : `st-badge ${tone}`}>{label}</span>;
}

// ── ChannelPicker ─────────────────────────────────────────────────────────────

export interface ChannelPickerItem {
  slug: string;
  name: string;
  href: string;
}

export function ChannelPicker({
  items,
  activeSlug,
  linkComponent: Link = DefaultLink,
}: {
  items: ChannelPickerItem[];
  activeSlug?: string;
  linkComponent?: LinkLike;
}) {
  return (
    <nav className="st-subnav">
      {items.map((it) => (
        <Link key={it.slug} href={it.href} className={it.slug === activeSlug ? "active" : ""}>{it.name}</Link>
      ))}
    </nav>
  );
}

// ── BigCountdown (タイムキーパー Phase 0) ────────────────────────────────────

export type TimerTone = "ok" | "warn" | "danger";

/** 残り秒数から色トーンを決める (5分未満=warn/1分未満=danger)。呼び出し側 (ページ) が使う。 */
export function remainingTone(seconds: number): TimerTone {
  if (seconds < 60) return "danger";
  if (seconds < 300) return "warn";
  return "ok";
}

/** 大きな残り時間表示。1秒毎に自己更新する leaf。ロジック (何色にするか) はページ側が
 * remainingTone() で決めて tone として渡す — このコンポーネントは表示専用。 */
export function BigCountdown({ targetSec, tone }: { targetSec: number | null; tone: TimerTone }) {
  const now = useNowTick();
  if (targetSec == null) return <div className="ops-tk-big ops-tk-big--neutral">--:--</div>;
  const rem = Math.floor(targetSec - now);
  const over = rem < 0;
  const s = Math.abs(rem);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const pad = (n: number) => (n < 10 ? "0" + n : String(n));
  const label = h > 0 ? `${h}:${pad(m)}:${pad(sec)}` : `${pad(m)}:${pad(sec)}`;
  return (
    <div className={`ops-tk-big ops-tk-big--${tone}${over ? " ops-tk-big--over" : ""}`}>
      {over ? "+" : ""}
      {label}
    </div>
  );
}
