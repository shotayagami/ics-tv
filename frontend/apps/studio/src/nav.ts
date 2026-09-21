// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useLocation } from "react-router-dom";

import type { SidebarGroup, SidebarNavItem } from "./atoms";

/** ナビ項目 1 件の IA 定義。href はサイドバーの遷移先。match はアクティブ判定を広げる
 * 追加 prefix(href の prefix で拾えない orphan サブ画面用)。 */
interface NavDef {
  label: string;
  href: string;
  /** href 以外でこの項目をアクティブ表示する追加 path prefix。 */
  match?: string[];
}

interface NavGroupDef {
  heading: string;
  items: NavDef[];
}

/** グループ上に単独表示するホーム。 */
export const NAV_HOME: NavDef = { label: "ダッシュボード", href: "/" };

/** studio 管理 IA。リファクタ Phase 1.4 で「送出/運用 (放送当直)」を別ホスト ops.* の
 * 🔴放送コンソールへ分離 (docs/refactor-service-split.md) → studio は編成・制作 + 経営・権利 +
 * 設定に専念。サイドバーと Dashboard ランチャの単一正本 (両者がこれを参照し宛先がドリフトしない)。
 * 納品=将来 ICS-DELIVERY、経営・権利=将来 ICS-BACKOFFICE の切り出し候補 (Phase 2/3)。 */
export const NAV_GROUPS: NavGroupDef[] = [
  {
    heading: "編成・制作",
    items: [
      { label: "編成", href: "/scheduling", match: ["/program-form", "/graphic-cues", "/live-rundown"] },
      { label: "週間編成", href: "/week", match: ["/series"] },
      // 配信枠の予約作成 (稼働監視は 🔴放送コンソール。§6 決定①-a 両面)
      { label: "配信枠", href: "/slots" },
      { label: "番組専用枠", href: "/youtube/dedicated" },
      { label: "配信プリセット", href: "/youtube/presets" },
      { label: "YT テンプレート", href: "/youtube/templates" },
      { label: "生入力", href: "/live-sources" },
    ],
  },
  {
    heading: "コンテンツ",
    items: [
      { label: "素材", href: "/medialib", match: ["/cuesheet"] },
      // CM 割付 (契約/請求は経営・権利。§6 決定①-b 分割)
      { label: "CM割付", href: "/sales" },
    ],
  },
  {
    heading: "経営・権利",
    items: [
      { label: "請求", href: "/billing" },
      { label: "権利", href: "/rights" },
      { label: "会員統計", href: "/members/stats" },
    ],
  },
  {
    // #27 ファンクラブ (fanclub app 独立モジュール境界)。「経営・権利」には相乗りさせない
    // (将来 ICS-BACKOFFICE 切り出し候補とは別系統のため)。
    heading: "ファンクラブ",
    items: [{ label: "クリエイター", href: "/creators" }],
  },
  {
    heading: "設定",
    items: [
      { label: "チャンネル", href: "/channels" },
      { label: "時計プリセット", href: "/clock-presets" },
      // awstats 的な画面。backoffice.* からもこの画面へリンクする (集計は複製しない・単一の真実源)。
      { label: "アクセス統計", href: "/access-stats" },
    ],
  },
];

/** path が項目の href(または match prefix)に一致するか。"/" はホーム専用(完全一致)。 */
function isActive(href: string, match: string[] | undefined, path: string): boolean {
  if (href === "/") return path === "/";
  const prefixes = [href, ...(match ?? [])];
  return prefixes.some((p) => path === p || path.startsWith(p + "/"));
}

/** 現在 location から home + 各グループ項目の active を算出して DS 型へ写す。
 * DS(StudioSidebar)は dumb のまま、アクティブ判定だけアプリが持つ。 */
export function useSidebarNav(): {
  home: SidebarNavItem;
  groups: SidebarGroup[];
} {
  const path = useLocation().pathname;
  return {
    home: { ...NAV_HOME, active: isActive(NAV_HOME.href, undefined, path) },
    groups: NAV_GROUPS.map((g) => ({
      heading: g.heading,
      items: g.items.map((it) => ({
        label: it.label,
        href: it.href,
        active: isActive(it.href, it.match, path),
      })),
    })),
  };
}
