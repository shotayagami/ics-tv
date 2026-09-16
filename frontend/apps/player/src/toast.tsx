// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { createContext, useCallback, useContext, useRef, useState } from "react";
import type { ReactNode } from "react";

export type ToastTone = "success" | "error" | "info";

export interface ToastApi {
  success: (message: ReactNode, opts?: { duration?: number }) => number;
  error: (message: ReactNode, opts?: { duration?: number }) => number;
  info: (message: ReactNode, opts?: { duration?: number }) => number;
  dismiss: (id: number) => void;
}

interface ToastItem {
  id: number;
  tone: ToastTone;
  message: ReactNode;
}

const ToastCtx = createContext<ToastApi | null>(null);

export function ToastProvider({
  children,
  duration = 4000,
}: {
  children: ReactNode;
  duration?: number;
}) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const seq = useRef(0);

  const dismiss = useCallback((id: number) => {
    setToasts((t) => t.filter((x) => x.id !== id));
  }, []);

  const push = useCallback(
    (tone: ToastTone, message: ReactNode, opts: { duration?: number } = {}) => {
      const id = ++seq.current;
      setToasts((t) => t.concat([{ id, tone, message }]));
      const ms = opts.duration ?? duration;
      if (ms > 0) setTimeout(() => dismiss(id), ms);
      return id;
    },
    [duration, dismiss],
  );

  const api: ToastApi = {
    success: (m, o) => push("success", m, o),
    error: (m, o) => push("error", m, o),
    info: (m, o) => push("info", m, o),
    dismiss,
  };

  return (
    <ToastCtx.Provider value={api}>
      {children}
      <div className="toast-region" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className={`toast ${t.tone}`} role="status">
            <span className="toast-dot" aria-hidden="true" />
            <span className="toast-msg">{t.message}</span>
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

export function useToast(): ToastApi {
  const ctx = useContext(ToastCtx);
  if (!ctx) throw new Error("useToast must be used within <ToastProvider>");
  return ctx;
}
