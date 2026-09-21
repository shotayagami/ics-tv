// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
// studio UI アトム: presentational のみ — ロジックは呼び出し側が保持する

import {
  Children,
  cloneElement,
  isValidElement,
  useEffect,
  useId,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
} from "react";
import type { ComponentType, ReactElement, ReactNode } from "react";

// ── LinkLike (react-router アダプタ用型) ──────────────────────────────────────

export type LinkLike = ComponentType<{ href: string; className?: string; children: ReactNode }>;

export const DefaultLink: LinkLike = ({ href, className, children }) => (
  <a href={href} className={className}>{children}</a>
);

// ── SidebarNavItem / SidebarGroup (nav.ts が参照する型) ──────────────────────

export interface SidebarNavItem {
  label: string;
  href: string;
  active?: boolean;
}

export interface SidebarGroup {
  heading: string;
  items: SidebarNavItem[];
}

// ── EmptyState ────────────────────────────────────────────────────────────────

export function EmptyState({ children, loading = false }: { children?: ReactNode; loading?: boolean }) {
  if (loading) return <p className="st-loading">{children ?? "読み込み中…"}</p>;
  return <div className="st-empty">{children ?? "データがありません。"}</div>;
}

// ── Notice ────────────────────────────────────────────────────────────────────

export type NoticeVariant = "success" | "error";

export function Notice({ variant = "success", children }: { variant?: NoticeVariant; children: ReactNode }) {
  return <p className={variant === "error" ? "st-notice err" : "st-notice"}>{children}</p>;
}

// ── StatusBadge ───────────────────────────────────────────────────────────────

export type StatusTone = "neutral" | "ok" | "warn" | "danger";

export function StatusBadge({ label, tone = "neutral" }: { label: string; tone?: StatusTone }) {
  return <span className={tone === "neutral" ? "st-badge" : `st-badge ${tone}`}>{label}</span>;
}

// ── OldScreenLink ─────────────────────────────────────────────────────────────

export function OldScreenLink({ href, children }: { href: string; children: ReactNode }) {
  return <a className="st-oldlink" href={href}>旧画面 › {children}</a>;
}

// ── Meter ─────────────────────────────────────────────────────────────────────

export function Meter({
  value,
  max = 100,
  label,
  display,
}: {
  value: number;
  max?: number;
  label?: ReactNode;
  display?: ReactNode;
}) {
  const pct = max > 0 ? Math.max(0, Math.min(100, (value / max) * 100)) : 0;
  return (
    <div className="st-meter">
      {label != null && <span className="label">{label}</span>}
      <div className="track">
        <div className="st-bar" style={{ width: `${pct}%` }} />
      </div>
      {display != null && <span className="val">{display}</span>}
    </div>
  );
}

// ── Breadcrumb ────────────────────────────────────────────────────────────────

export interface Crumb {
  label: string;
  href?: string;
}

export function Breadcrumb({
  items,
  linkComponent: Link = DefaultLink,
}: {
  items: Crumb[];
  linkComponent?: LinkLike;
}) {
  return (
    <nav className="st-breadcrumb" aria-label="breadcrumb">
      {items.map((c, i) =>
        c.href ? (
          <Link key={i} href={c.href} className="">‹ {c.label}</Link>
        ) : (
          <span key={i} aria-current="page">{c.label}</span>
        ),
      )}
    </nav>
  );
}

// ── SubNav ────────────────────────────────────────────────────────────────────

export interface SubNavItem {
  label: string;
  href: string;
  active?: boolean;
}

export function SubNav({
  items,
  linkComponent: Link = DefaultLink,
}: {
  items: SubNavItem[];
  linkComponent?: LinkLike;
}) {
  return (
    <nav className="st-subnav">
      {items.map((it, i) => (
        <Link key={i} href={it.href} className={it.active ? "active" : ""}>{it.label}</Link>
      ))}
    </nav>
  );
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

// ── StudioPage ────────────────────────────────────────────────────────────────

export function StudioPage({
  title,
  breadcrumb,
  channel,
  actions,
  children,
}: {
  title: ReactNode;
  breadcrumb?: ReactNode;
  channel?: ReactNode;
  actions?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <>
      {breadcrumb}
      <div className="st-page-head">
        <h1 className="st-page-title">{title}</h1>
        {channel}
        {actions && <div className="st-page-actions">{actions}</div>}
      </div>
      {children}
    </>
  );
}

// ── Field ─────────────────────────────────────────────────────────────────────

export function Field({
  label,
  children,
  htmlFor,
  error,
  hint,
  required = false,
}: {
  label: string;
  children: ReactNode;
  htmlFor?: string;
  error?: ReactNode;
  hint?: ReactNode;
  required?: boolean;
}) {
  const autoId = useId();
  const id = htmlFor ?? autoId;
  const invalid = !!error;
  const hintId = `${id}-hint`;
  const errId = `${id}-err`;
  const describedBy =
    [hint && !invalid ? hintId : null, invalid ? errId : null].filter(Boolean).join(" ") || undefined;

  let injected = false;
  const kids = Children.map(children, (child) => {
    if (!injected && isValidElement(child)) {
      injected = true;
      const el = child as ReactElement<Record<string, unknown>>;
      return cloneElement(el, {
        id: (el.props.id as string | undefined) ?? id,
        "aria-invalid": invalid || undefined,
        "aria-describedby": describedBy,
        "aria-required": required || undefined,
      });
    }
    return child;
  });

  return (
    <div className={`field${invalid ? " invalid" : ""}`}>
      <label htmlFor={id}>
        {label}
        {required ? <span className="req" aria-hidden="true">{" *"}</span> : null}
      </label>
      {kids}
      {hint && !invalid ? <span className="hint" id={hintId}>{hint}</span> : null}
      {invalid ? <span className="err" id={errId} role="alert">{error}</span> : null}
    </div>
  );
}

// ── StudioSidebar ─────────────────────────────────────────────────────────────

export function StudioSidebar({
  logo = "ICS-TV studio",
  home,
  groups,
  footer,
  linkComponent: Link = DefaultLink,
}: {
  logo?: string;
  home?: SidebarNavItem;
  groups: SidebarGroup[];
  footer?: ReactNode;
  linkComponent?: LinkLike;
}) {
  return (
    <aside className="st-sidebar">
      <span className="logo">{logo}</span>
      <nav>
        {home && (
          <Link href={home.href} className={home.active ? "active" : ""}>{home.label}</Link>
        )}
        {groups.map((g, gi) => (
          <div className="st-nav-group" key={gi}>
            <div className="st-nav-group-h">{g.heading}</div>
            {g.items.map((it, i) => (
              <Link key={i} href={it.href} className={it.active ? "active" : ""}>{it.label}</Link>
            ))}
          </div>
        ))}
      </nav>
      {footer && <div className="st-sidebar-foot">{footer}</div>}
    </aside>
  );
}

// ── StudioLayout ──────────────────────────────────────────────────────────────

export function StudioLayout({
  sidebar,
  children,
  brand = "ICS-TV studio",
}: {
  sidebar: ReactNode;
  children: ReactNode;
  brand?: string;
}) {
  const [open, setOpen] = useState(false);
  const hamburgerRef = useRef<HTMLButtonElement>(null);

  const closeOnNav = (e: ReactMouseEvent) => {
    if ((e.target as Element).closest?.(".st-sidebar a")) setOpen(false);
  };

  useEffect(() => {
    if (!open) return;
    document.querySelector<HTMLElement>(".st-sidebar a")?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      hamburgerRef.current?.focus();
    };
  }, [open]);

  return (
    <div className={open ? "st-layout is-open" : "st-layout"} onClick={closeOnNav}>
      {sidebar}
      <div className="st-sidebar-scrim" onClick={() => setOpen(false)} />
      <main className="st-main">
        <div className="st-topbar">
          <button
            ref={hamburgerRef}
            type="button"
            className="st-hamburger"
            aria-label="メニュー"
            aria-expanded={open}
            onClick={() => setOpen((v) => !v)}
          >
            ☰
          </button>
          <span className="st-topbar-brand">{brand}</span>
        </div>
        {children}
      </main>
    </div>
  );
}
