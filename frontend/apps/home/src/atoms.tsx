// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
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

export function LiveBadge({ label = "LIVE" }: { label?: string }) {
  return (
    <span className="badge-live">
      <span className="pulse-dot" />
      {label}
    </span>
  );
}

export function ProgressBar({ pct }: { pct: number }) {
  const w = Math.max(0, Math.min(100, pct));
  return <div className="progress"><i style={{ width: `${w}%` }} /></div>;
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
