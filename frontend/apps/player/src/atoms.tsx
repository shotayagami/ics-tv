// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useId, useRef } from "react";
import type { CSSProperties, MouseEvent, ReactNode } from "react";

// ── Button ────────────────────────────────────────────────────────────────────

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

// ── Chip ──────────────────────────────────────────────────────────────────────

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

// ── LiveBadge ─────────────────────────────────────────────────────────────────

export function LiveBadge({ label = "LIVE" }: { label?: string }) {
  return (
    <span className="badge-live">
      <span className="pulse-dot" />
      {label}
    </span>
  );
}

// ── ProgressBar ───────────────────────────────────────────────────────────────

export function ProgressBar({ pct }: { pct: number }) {
  const w = Math.max(0, Math.min(100, pct));
  return <div className="progress"><i style={{ width: `${w}%` }} /></div>;
}

// ── StatusPill ────────────────────────────────────────────────────────────────

export type StatusTone = "neutral" | "ok" | "warn" | "danger" | "live" | "restricted";

export function StatusPill({
  tone = "neutral",
  outline = false,
  dot,
  children,
}: {
  tone?: StatusTone;
  outline?: boolean;
  dot?: boolean;
  children: ReactNode;
}) {
  const showDot = dot ?? tone === "live";
  return (
    <span className={`statuspill tone-${tone}${outline ? " is-outline" : ""}`} role="status">
      {showDot ? <span className="sp-dot" aria-hidden="true" /> : null}
      {children}
    </span>
  );
}

// ── ChannelTabs ───────────────────────────────────────────────────────────────

export interface TabItem {
  slug: string;
  name: string;
  tint: string;
  href: string;
  active: boolean;
  pinned: boolean;
}

export function ChannelTabs({ items, homeHref }: { items: TabItem[]; homeHref: string }) {
  return (
    <div className="tabs">
      <a className="home" href={homeHref}>‹ ホーム</a>
      <span className="slash">/</span>
      {items.map((c) => (
        <a key={c.slug} className={"tab" + (c.active ? " sel" : "")} href={c.href}>
          <span className="dot" style={{ background: c.tint }} />
          {c.name}
          {c.pinned && <span className="pin-mark" aria-label="お気に入り">★</span>}
        </a>
      ))}
    </div>
  );
}

// ── ScheduleList ──────────────────────────────────────────────────────────────

export interface ScheduleRow {
  id: number;
  time: string;
  title: string;
  genre: string;
  color: string;
  isNow: boolean;
  rowBg: string;
  timeColor: string;
  titleColor: string;
  href?: string;
  isRerun?: boolean;
}

export function ScheduleList({
  items,
  emptyText = "本日の番組情報はまだありません。",
}: {
  items: ScheduleRow[];
  emptyText?: string;
}) {
  if (items.length === 0) return <div className="empty">{emptyText}</div>;
  return (
    <>
      {items.map((p) => {
        const inner = (
          <>
            <div className="when">
              <div className="tm" style={{ color: p.timeColor }}>{p.time}</div>
              {p.isNow && <div className="on">ON AIR</div>}
              {p.isRerun && <div className="rerun">再放送</div>}
            </div>
            <div className="b" style={{ borderLeft: `3px solid ${p.color}` }}>
              <div className="t" style={{ color: p.titleColor }}>{p.title}</div>
              {p.genre && <div className="g" style={{ color: p.color }}>{p.genre}</div>}
            </div>
          </>
        );
        const key = `${p.isRerun ? "r" : "p"}${p.id}-${p.time}`;
        const style = { background: p.rowBg, textDecoration: "none", color: "inherit" };
        return p.href ? (
          <a key={key} className="item" href={p.href} style={style}>{inner}</a>
        ) : (
          <div key={key} className="item" style={style}>{inner}</div>
        );
      })}
    </>
  );
}

// ── TextField ─────────────────────────────────────────────────────────────────

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

// ── Modal + ConfirmDialog ─────────────────────────────────────────────────────

const FOCUSABLE =
  'a[href],button:not([disabled]),textarea,input,select,[tabindex]:not([tabindex="-1"])';

function Modal({
  open,
  onClose,
  title,
  children,
  actions,
}: {
  open: boolean;
  onClose?: () => void;
  title?: string;
  children?: ReactNode;
  actions?: ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const el = ref.current;
    const prev = document.activeElement as HTMLElement | null;
    const list = (): HTMLElement[] =>
      el ? Array.from(el.querySelectorAll<HTMLElement>(FOCUSABLE)) : [];
    list()[0]?.focus();

    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose?.();
      } else if (e.key === "Tab") {
        const l = list();
        if (!l.length) return;
        const first = l[0];
        const last = l[l.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      prev?.focus?.();
    };
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div className="scrim" onMouseDown={(e) => e.target === e.currentTarget && onClose?.()}>
      <div className="dialog" role="dialog" aria-modal="true" aria-label={title} ref={ref}>
        {title ? <h2 className="dialog-title">{title}</h2> : null}
        {children ? <div className="dialog-body">{children}</div> : null}
        {actions ? <div className="dialog-actions">{actions}</div> : null}
      </div>
    </div>
  );
}

export function ConfirmDialog({
  open,
  title,
  message,
  destructive = false,
  confirmLabel = "確認",
  cancelLabel = "キャンセル",
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title?: string;
  message?: ReactNode;
  destructive?: boolean;
  confirmLabel?: string;
  cancelLabel?: string;
  onConfirm?: () => void;
  onCancel?: () => void;
}) {
  return (
    <Modal
      open={open}
      title={title}
      onClose={onCancel}
      actions={
        <>
          <button type="button" className="dlg-btn btn-cancel" onClick={onCancel}>{cancelLabel}</button>
          <button
            type="button"
            className={`dlg-btn ${destructive ? "btn-danger" : "btn-confirm"}`}
            onClick={onConfirm}
          >
            {confirmLabel}
          </button>
        </>
      }
    >
      {message}
    </Modal>
  );
}
