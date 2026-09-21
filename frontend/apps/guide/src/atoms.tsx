// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import type { MouseEvent, ReactNode } from "react";

export interface SegmentedItem {
  key?: string | number;
  label: ReactNode;
  href?: string;
  selected?: boolean;
  onClick?: (e: MouseEvent, item: SegmentedItem) => void;
}

export function SegmentedNav({
  items = [],
  ariaLabel,
  layout,
  variant = "segmented",
  onSelect,
}: {
  items: SegmentedItem[];
  ariaLabel?: string;
  layout?: "auto" | "fill";
  variant?: "segmented" | "chips";
  onSelect?: (key: string | number, item: SegmentedItem) => void;
}) {
  const navCls = ["segnav", variant === "chips" ? "variant-chips" : "", layout ? `layout-${layout}` : ""]
    .filter(Boolean)
    .join(" ");
  return (
    <nav className={navCls} aria-label={ariaLabel}>
      {items.map((it, i) => {
        const key = it.key ?? i;
        const selected = !!it.selected;
        const cls = `segnav-item${selected ? " is-selected" : ""}`;
        const ariaCurrent = selected ? ("true" as const) : undefined;
        const handle = (e: MouseEvent) => {
          it.onClick?.(e, it);
          onSelect?.(key, it);
        };
        return it.href ? (
          <a key={key} className={cls} href={it.href} aria-current={ariaCurrent} onClick={handle}>
            {it.label}
          </a>
        ) : (
          <button key={key} type="button" className={cls} aria-current={ariaCurrent} onClick={handle}>
            {it.label}
          </button>
        );
      })}
    </nav>
  );
}
