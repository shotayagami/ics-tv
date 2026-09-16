// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import type { components } from "@icstv/api";

export type MemberStatsOut = components["schemas"]["MemberStatsOut"];
export type AccessStatsOut = components["schemas"]["AccessStatsOut"];
export type RightsDashboardOut = components["schemas"]["RightsDashboardOut"];
export type StatRow = components["schemas"]["StatRow"];
export type BillingOut = components["schemas"]["BillingOut"];
export type TimelineOut = components["schemas"]["TimelineOut"];
export type TimelineProgram = components["schemas"]["TimelineProgram"];
export type WeekOut = components["schemas"]["WeekOut"];
export type WeekProgram = components["schemas"]["WeekProgram"];
export type WeekSlotOccurrence = components["schemas"]["WeekSlotOccurrence"];
export type AdminChannel = components["schemas"]["AdminChannel"];
export type ChannelSettingsOut = components["schemas"]["ChannelSettingsOut"];
export type MedialibOut = components["schemas"]["MedialibOut"];
export type MlAsset = components["schemas"]["MlAsset"];
export type CueSheetOut = components["schemas"]["CueSheetOut"];
export type SeriesOut = components["schemas"]["SeriesOut"];
export type SeriesFormOut = components["schemas"]["SeriesFormOut"];
export type SeriesInitial = components["schemas"]["SeriesInitial"];
export type SlotFormOut = components["schemas"]["SlotFormOut"];
export type SlotInitial = components["schemas"]["SlotInitial"];
export type EpisodeOut = components["schemas"]["EpisodeOut"];
export type OpsStatusOut = components["schemas"]["OpsStatusOut"];
export type AllocationOut = components["schemas"]["AllocationOut"];
export type SlotListOut = components["schemas"]["SlotListOut"];
export type YtSlotRow = components["schemas"]["YtSlotRow"];
export type YtRollingConfigOut = components["schemas"]["YtRollingConfigOut"];
export type YtRollingConfigIn = components["schemas"]["YtRollingConfigIn"];
export type GraphicCuesOut = components["schemas"]["GraphicCuesOut"];
export type ProgramFormOut = components["schemas"]["ProgramFormOut"];
export type LiveSourcesOut = components["schemas"]["LiveSourcesOut"];
export type LiveSourceRow = components["schemas"]["LiveSourceRow"];
export type LiveRundownOut = components["schemas"]["LiveRundownOut"];
export type LiveCueRow = components["schemas"]["LiveCueRow"];
export type RundownTemplateOut = components["schemas"]["RundownTemplateOut"];

/** 円フォーマット (整数円 → ¥1,234)。 */
export function yen(n: number): string {
  return "¥" + n.toLocaleString("ja-JP");
}

// 通貨ごとの表示規則。サーバ側の `money` テンプレートフィルタ
// (server/core/templatetags/icstv_public.py) と同じ規則にする。
// Stripe の zero-decimal 通貨は minor unit がそのまま主単位になる (JPY は 1000 → ¥1,000)。
// 2-decimal 通貨は 100 で割る (USD は 1000 → $10.00)。未知の通貨は記号を付けず ISO コードを
// 併記して、**誤った記号で表示するより「読めない」ほうを選ぶ**。
const ZERO_DECIMAL = new Set(["jpy", "krw", "vnd", "clp", "isk"]);
const CURRENCY_SYMBOL: Record<string, string> = { jpy: "¥", usd: "$", eur: "€", gbp: "£" };

/** minor unit の金額を通貨に応じて整形する (ファンクラブ台帳の *_minor + currency 用)。 */
export function money(amountMinor: number | null | undefined, currency = "jpy"): string {
  const cur = (currency || "jpy").toLowerCase();
  const n = amountMinor ?? 0;
  const body = ZERO_DECIMAL.has(cur)
    ? n.toLocaleString("ja-JP")
    : (n / 100).toLocaleString("ja-JP", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const sym = CURRENCY_SYMBOL[cur];
  return sym ? sym + body : `${body} ${cur.toUpperCase()}`;
}

export function readCookie(name: string): string {
  const m = document.cookie.match(new RegExp("(?:^|;\\s*)" + name + "=([^;]+)"));
  return m ? decodeURIComponent(m[1]) : "";
}

/** 既存 Django JSON エンドポイント (ninja 外: 編成 move/resize 等) を CSRF 付きで叩く。
 * 200 → {ok:true,...}、422 → {ok:false,error/earliest} を共に JSON で返す。 */
export async function postJson(url: string, body: unknown): Promise<{ status: number; data: any }> {
  const res = await fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRFToken": readCookie("csrftoken") },
    body: JSON.stringify(body),
  });
  let data: any = null;
  try {
    data = await res.json();
  } catch {
    /* 非 JSON (302 等) */
  }
  return { status: res.status, data };
}

/** multipart/form-data で POST (ファイルアップロード用: 速報チャイム音源等)。
 * Content-Type は FormData が boundary 付きで自動設定するため明示しない。 */
export async function postFile(
  url: string,
  fields: Record<string, string | Blob>,
): Promise<{ status: number; data: any }> {
  const fd = new FormData();
  for (const [k, v] of Object.entries(fields)) fd.append(k, v);
  const res = await fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: { "X-CSRFToken": readCookie("csrftoken") },
    body: fd,
  });
  let data: any = null;
  try {
    data = await res.text();
  } catch {
    /* ignore */
  }
  return { status: res.status, data };
}

/** 既存の HTMX 系 Django view (request.POST を読む: medialib screening 等) 用に form-encoded で POST。 */
export async function postForm(
  url: string,
  fields: Record<string, string> = {},
): Promise<{ status: number; data: any }> {
  const res = await fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/x-www-form-urlencoded", "X-CSRFToken": readCookie("csrftoken") },
    body: new URLSearchParams(fields),
  });
  let data: any = null;
  try {
    data = await res.json();
  } catch {
    /* _ok は text/HTMX なので JSON でない */
  }
  return { status: res.status, data };
}
