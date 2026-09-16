// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useId } from "react";
import type { CSSProperties, MouseEvent, ReactNode } from "react";

export type ButtonVariant = "play" | "ghost" | "pin";

export function Button({
  variant = "play",
  active = false,
  href,
  onClick,
  target,
  rel,
  as,
  disabled = false,
  children,
}: {
  variant?: ButtonVariant;
  active?: boolean;
  href?: string;
  onClick?: (e: MouseEvent<HTMLElement>) => void;
  target?: string;
  rel?: string;
  as?: "span";
  disabled?: boolean;
  children: ReactNode;
}) {
  const cls =
    variant === "play"
      ? "btn-play"
      : variant === "ghost"
        ? "btn-ghost"
        : "btn-pin" + (active ? " on" : "");
  if (as === "span") return <span className={cls} onClick={onClick}>{children}</span>;
  if (href !== undefined)
    return <a className={cls} href={href} target={target} rel={rel} onClick={onClick}>{children}</a>;
  return <button className={cls} type="button" onClick={onClick} disabled={disabled}>{children}</button>;
}

export function Chip({
  label,
  genre = false,
  color,
  className,
}: {
  label: string;
  genre?: boolean;
  color?: string;
  className?: string;
}) {
  const style: CSSProperties | undefined = !genre && color ? { background: color } : undefined;
  const cls = [genre ? "chip genre" : "chip", className].filter(Boolean).join(" ");
  return <span className={cls} style={style}>{label}</span>;
}

export function Hero({
  href,
  media,
  live = false,
  bug,
  chips,
  title,
  meta,
  progress,
  actions,
}: {
  href?: string;
  media?: ReactNode;
  live?: boolean;
  bug?: ReactNode;
  chips?: ReactNode;
  title: ReactNode;
  meta?: ReactNode;
  progress?: ReactNode;
  actions?: ReactNode;
}) {
  const inner = (
    <>
      {media ?? (
        <div className="hero-media" aria-hidden="true">
          <span className="hero-bg-label" />
        </div>
      )}
      {live ? (
        <span className="badge-live">
          <span className="pulse-dot" />
          LIVE
          <span className="sr-only">オンエア中</span>
        </span>
      ) : null}
      {bug ? <div className="bug">{bug}</div> : null}
      <div className="hero-scrim">
        {chips ? <div className="chips">{chips}</div> : null}
        <h1>{title}</h1>
        {meta ? <div className="meta">{meta}</div> : null}
        {progress ? <div className="prog">{progress}</div> : null}
        {actions ? <div className="actions">{actions}</div> : null}
      </div>
    </>
  );
  return href ? <a className="hero" href={href}>{inner}</a> : <div className="hero">{inner}</div>;
}

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

export interface SearchResultItem {
  id?: string | number;
  time: string;
  title: string;
  channel?: string;
  genre?: string;
  href?: string;
}

export function SearchResultList({
  items,
  emptyText = "該当する番組がありません。",
}: {
  items: SearchResultItem[];
  emptyText?: string;
}) {
  if (items.length === 0) return <div className="sr-empty">{emptyText}</div>;
  return (
    <ul className="sr-list">
      {items.map((it, i) => (
        <li key={it.id ?? i}>
          <a href={it.href}>
            <span className="st tabnum">{it.time}</span>
            <span className="stt">
              {it.title}
              {it.channel && (
                <span className="muted" style={{ fontWeight: 600 }}>
                  {" "}・ {it.channel}
                </span>
              )}
            </span>
            {it.genre && <span className="sg">{it.genre}</span>}
          </a>
        </li>
      ))}
    </ul>
  );
}

export function TextField({
  value,
  onChange,
  placeholder,
  type = "text",
  autoFocus,
  label,
  ariaLabel,
  hint,
  error,
  required = false,
  multiline = false,
  rows = 3,
  maxLength,
}: {
  value?: string;
  onChange?: (v: string) => void;
  placeholder?: string;
  type?: string;
  autoFocus?: boolean;
  label?: string;
  ariaLabel?: string;
  hint?: string;
  error?: string;
  required?: boolean;
  multiline?: boolean;
  rows?: number;
  maxLength?: number;
}) {
  const id = useId();
  const invalid = !!error;
  const decorated = !!(label || hint || error || required || multiline);

  if (!decorated) {
    return (
      <input
        className="sr-input"
        type={type}
        value={value}
        placeholder={placeholder}
        autoFocus={autoFocus}
        aria-label={ariaLabel}
        maxLength={maxLength}
        onChange={onChange ? (e) => onChange(e.target.value) : undefined}
      />
    );
  }

  const hintId = `${id}-hint`;
  const errId = `${id}-err`;
  const describedBy =
    [hint && !invalid ? hintId : null, invalid ? errId : null].filter(Boolean).join(" ") || undefined;
  const common = {
    id,
    placeholder,
    value,
    autoFocus,
    maxLength,
    required: required || undefined,
    "aria-invalid": invalid || undefined,
    "aria-describedby": describedBy,
    "aria-required": required || undefined,
    "aria-label": label ? undefined : ariaLabel,
  };

  return (
    <div className="field">
      {label ? (
        <label className="field-label" htmlFor={id}>
          {label}
          {required ? <span className="req" aria-hidden="true">{" *"}</span> : null}
        </label>
      ) : null}
      {multiline ? (
        <textarea
          {...common}
          rows={rows}
          className={`field-textarea${invalid ? " is-invalid" : ""}`}
          onChange={onChange ? (e) => onChange(e.target.value) : undefined}
        />
      ) : (
        <input
          {...common}
          type={type}
          className={`sr-input${invalid ? " is-invalid" : ""}`}
          onChange={onChange ? (e) => onChange(e.target.value) : undefined}
        />
      )}
      {hint && !invalid ? <div className="field-hint" id={hintId}>{hint}</div> : null}
      {invalid ? <div className="field-error" id={errId} role="alert">{error}</div> : null}
    </div>
  );
}
