// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useState } from "react";

import type { components } from "@icstv/api";

// 放送コンソールが使う API 型。studio の hooks.ts と同じく @icstv/api のスキーマから写す。
export type OpsStatusOut = components["schemas"]["OpsStatusOut"];
export type YtSlotRow = components["schemas"]["YtSlotRow"];  // 配信枠 (YT 配信状態) 監視
export type LiveSourcesOut = components["schemas"]["LiveSourcesOut"];  // 生入力 ingest URL (読み取り)
export type LiveSourceRow = components["schemas"]["LiveSourceRow"];
export type TimekeeperOut = components["schemas"]["TimekeeperOut"];  // タイムキーパー (Phase 0)

/** 毎秒更新の epoch 秒 (進行バー/カウントダウン算出用)。使う側だけが再レンダする。
 * apps/player/src/hooks.ts と同型 (このリポジトリはアプリごとに軽量コピーを持つ慣習)。 */
export function useNowTick(): number {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => window.clearInterval(id);
  }, []);
  return now;
}

function readCookie(name: string): string {
  const m = document.cookie.match(new RegExp("(?:^|;\\s*)" + name + "=([^;]+)"));
  return m ? decodeURIComponent(m[1]) : "";
}

/** 既存 HTMX 系 Django view (/ops/ch/<slug>/...) を CSRF 付き form-encoded で POST する。
 * studio hooks.ts の postForm と同型。200/204 が成功。送出操作 (P1.2) で使う。 */
export async function postForm(
  url: string,
  fields: Record<string, string> = {},
): Promise<{ status: number; text: string }> {
  const res = await fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded",
      "X-CSRFToken": readCookie("csrftoken"),
    },
    body: new URLSearchParams(fields),
  });
  // 失敗時 (409 押え吸収不能 等) は本文に理由が入る → 呼び出し側がそのまま表示する。
  const text = await res.text().catch(() => "");
  return { status: res.status, text };
}

/** 画像ファイルを含む CG overlay 操作 (op_overlay kind=graphic) 用の multipart POST。
 * postForm と違い Content-Type は fetch が FormData から自動付与する (boundary 込み)。
 * フル CG op パネル (P1.3) で画像アップロード + 位置指定を送る。 */
export async function postMultipart(
  url: string,
  form: FormData,
): Promise<{ status: number; text: string }> {
  const res = await fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: { "X-CSRFToken": readCookie("csrftoken") },
    body: form,
  });
  const text = await res.text().catch(() => "");
  return { status: res.status, text };
}
