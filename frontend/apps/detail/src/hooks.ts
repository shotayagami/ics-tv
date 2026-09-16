// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import type { components } from "@icstv/api";

export type ProgramDetailOut = components["schemas"]["ProgramDetailOut"];
export type ProgramCta = components["schemas"]["ProgramCta"];

/** 非 GET の Django ビュー (member AJAX トグル) 用に csrftoken cookie を読む。 */
export function readCookie(name: string): string {
  const m = document.cookie.match(new RegExp("(?:^|;\\s*)" + name + "=([^;]+)"));
  return m ? decodeURIComponent(m[1]) : "";
}

/** お気に入り/リマインドのトグル。既存 member エンドポイントが X-Requested-With で JSON を返す。 */
export async function postToggle(url: string): Promise<Record<string, boolean>> {
  const res = await fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: { "X-Requested-With": "XMLHttpRequest", "X-CSRFToken": readCookie("csrftoken") },
  });
  if (!res.ok) throw new Error(String(res.status));
  return res.json();
}
