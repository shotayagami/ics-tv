// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import type { MouseEvent, ReactNode } from "react";

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

export function DateTimePill({ children }: { children: ReactNode }) {
  return <span className="datetime-pill">{children}</span>;
}
