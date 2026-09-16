// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
/** 時計フォントの一元定義。
 *
 * ClockPresetsPage / ClockEditorPage / ClockStyleOverride の選択肢、
 * Remotion / React プレビューの font-family、Google Fonts 動的ロードを共用する。
 *
 * 送出側 CasparCG テンプレート
 *   deploy/playout-node/casparcg/template/clock/corner.html の GOOGLE_FONT_MAP
 * はブラウザ非モジュール環境ゆえ別実装。**ここを更新したら corner.html も揃えること。** */

export interface ClockFontOption {
  value: string;
  label: string;
  group: string;
}

interface FontDef {
  value: string;   // slug (DB 保存値)
  label: string;   // UI 表示名
  group: string;   // optgroup 見出し
  css: string;     // CSS font-family 文字列
  family: string;  // Google Fonts family 名
  weights: string; // css2 の wght@ に渡す weight 列 ("" = 単一ウェイト)
}

// グループ順 = 表示順。日本語 → 欧文の順に並べる。
const FONTS: FontDef[] = [
  // ---- 日本語（ゴシック）----
  { value: "noto-sans-jp",     label: "Noto Sans JP（既定）",       group: "日本語（ゴシック）", css: '"Noto Sans JP",sans-serif',           family: "Noto Sans JP",           weights: "400;700;900" },
  { value: "zen-kaku-gothic",  label: "Zen Kaku Gothic New",        group: "日本語（ゴシック）", css: '"Zen Kaku Gothic New",sans-serif',    family: "Zen Kaku Gothic New",    weights: "400;700;900" },
  { value: "zen-kaku-antique", label: "Zen Kaku Gothic Antique",    group: "日本語（ゴシック）", css: '"Zen Kaku Gothic Antique",sans-serif', family: "Zen Kaku Gothic Antique", weights: "400;700;900" },
  { value: "biz-udgothic",     label: "BIZ UDGothic",               group: "日本語（ゴシック）", css: '"BIZ UDGothic",sans-serif',           family: "BIZ UDGothic",           weights: "400;700" },
  { value: "m-plus-1p",        label: "M PLUS 1p",                  group: "日本語（ゴシック）", css: '"M PLUS 1p",sans-serif',              family: "M PLUS 1p",              weights: "400;700;900" },
  { value: "murecho",          label: "Murecho",                    group: "日本語（ゴシック）", css: '"Murecho",sans-serif',                family: "Murecho",                weights: "400;700;900" },
  { value: "sawarabi-gothic",  label: "Sawarabi Gothic",            group: "日本語（ゴシック）", css: '"Sawarabi Gothic",sans-serif',        family: "Sawarabi Gothic",        weights: "400" },

  // ---- 日本語（明朝）----
  { value: "noto-serif-jp",    label: "Noto Serif JP",              group: "日本語（明朝）",     css: '"Noto Serif JP",serif',               family: "Noto Serif JP",          weights: "400;700;900" },
  { value: "shippori-mincho",  label: "Shippori Mincho",            group: "日本語（明朝）",     css: '"Shippori Mincho",serif',             family: "Shippori Mincho",        weights: "400;700;800" },
  { value: "zen-old-mincho",   label: "Zen Old Mincho",             group: "日本語（明朝）",     css: '"Zen Old Mincho",serif',              family: "Zen Old Mincho",         weights: "400;700;900" },
  { value: "kaisei-decol",     label: "Kaisei Decol",               group: "日本語（明朝）",     css: '"Kaisei Decol",serif',                family: "Kaisei Decol",           weights: "400;700" },
  { value: "sawarabi-mincho",  label: "Sawarabi Mincho",            group: "日本語（明朝）",     css: '"Sawarabi Mincho",serif',             family: "Sawarabi Mincho",        weights: "400" },

  // ---- 日本語（丸・ポップ・装飾）----
  { value: "zen-maru-gothic",  label: "Zen Maru Gothic（丸ゴ）",    group: "日本語（丸・装飾）", css: '"Zen Maru Gothic",sans-serif',        family: "Zen Maru Gothic",        weights: "400;700;900" },
  { value: "mplus-rounded",    label: "M PLUS Rounded 1c（丸ゴ）",  group: "日本語（丸・装飾）", css: '"M PLUS Rounded 1c",sans-serif',      family: "M PLUS Rounded 1c",      weights: "400;700;900" },
  { value: "kosugi-maru",      label: "Kosugi Maru（丸ゴ）",        group: "日本語（丸・装飾）", css: '"Kosugi Maru",sans-serif',            family: "Kosugi Maru",            weights: "400" },
  { value: "dela-gothic-one",  label: "Dela Gothic One（極太見出）", group: "日本語（丸・装飾）", css: '"Dela Gothic One",sans-serif',        family: "Dela Gothic One",        weights: "400" },
  { value: "reggae-one",       label: "Reggae One（見出し）",       group: "日本語（丸・装飾）", css: '"Reggae One",sans-serif',             family: "Reggae One",             weights: "400" },
  { value: "rocknroll-one",    label: "RocknRoll One（ポップ）",    group: "日本語（丸・装飾）", css: '"RocknRoll One",sans-serif',          family: "RocknRoll One",          weights: "400" },
  { value: "yuji-syuku",       label: "Yuji Syuku（筆）",           group: "日本語（丸・装飾）", css: '"Yuji Syuku",serif',                  family: "Yuji Syuku",             weights: "400" },

  // ---- 欧文（表示・コンデンス）----
  { value: "oswald",           label: "Oswald",                     group: "欧文（表示）",       css: '"Oswald",sans-serif',                 family: "Oswald",                 weights: "400;700" },
  { value: "bebas-neue",       label: "Bebas Neue",                 group: "欧文（表示）",       css: '"Bebas Neue",sans-serif',             family: "Bebas Neue",             weights: "400" },
  { value: "anton",            label: "Anton（極太）",              group: "欧文（表示）",       css: '"Anton",sans-serif',                  family: "Anton",                  weights: "400" },
  { value: "saira-condensed",  label: "Saira Condensed",            group: "欧文（表示）",       css: '"Saira Condensed",sans-serif',        family: "Saira Condensed",        weights: "400;700;900" },
  { value: "teko",             label: "Teko（コンデンス）",         group: "欧文（表示）",       css: '"Teko",sans-serif',                   family: "Teko",                   weights: "400;700" },

  // ---- 欧文（テック・SF）----
  { value: "orbitron",         label: "Orbitron",                   group: "欧文（テック・SF）", css: '"Orbitron",sans-serif',               family: "Orbitron",               weights: "400;700;900" },
  { value: "michroma",         label: "Michroma",                   group: "欧文（テック・SF）", css: '"Michroma",sans-serif',               family: "Michroma",               weights: "400" },
  { value: "rajdhani",         label: "Rajdhani",                   group: "欧文（テック・SF）", css: '"Rajdhani",sans-serif',               family: "Rajdhani",               weights: "400;700" },
  { value: "chakra-petch",     label: "Chakra Petch",               group: "欧文（テック・SF）", css: '"Chakra Petch",sans-serif',           family: "Chakra Petch",           weights: "400;700" },
  { value: "exo-2",            label: "Exo 2",                      group: "欧文（テック・SF）", css: '"Exo 2",sans-serif',                  family: "Exo 2",                  weights: "400;700;800" },

  // ---- 欧文（モノスペース）----
  { value: "share-tech-mono",  label: "Share Tech Mono",            group: "欧文（モノスペース）", css: '"Share Tech Mono",monospace',        family: "Share Tech Mono",        weights: "400" },
  { value: "space-mono",       label: "Space Mono",                 group: "欧文（モノスペース）", css: '"Space Mono",monospace',             family: "Space Mono",             weights: "400;700" },
  { value: "jetbrains-mono",   label: "JetBrains Mono",             group: "欧文（モノスペース）", css: '"JetBrains Mono",monospace',         family: "JetBrains Mono",         weights: "400;700;800" },
  { value: "ibm-plex-mono",    label: "IBM Plex Mono",              group: "欧文（モノスペース）", css: '"IBM Plex Mono",monospace',          family: "IBM Plex Mono",          weights: "400;700" },
];

/** フラットな選択肢一覧。 */
export const CLOCK_FONT_OPTIONS: ClockFontOption[] = FONTS.map(
  ({ value, label, group }) => ({ value, label, group }),
);

/** optgroup 用にグループ化した選択肢（表示順を保持）。 */
export const CLOCK_FONT_GROUPS: { group: string; options: ClockFontOption[] }[] = (() => {
  const order: string[] = [];
  const byGroup = new Map<string, ClockFontOption[]>();
  for (const o of CLOCK_FONT_OPTIONS) {
    if (!byGroup.has(o.group)) { byGroup.set(o.group, []); order.push(o.group); }
    byGroup.get(o.group)!.push(o);
  }
  return order.map((group) => ({ group, options: byGroup.get(group)! }));
})();

/** slug → CSS font-family 文字列。 */
export const CLOCK_FONT_FAMILY_CSS: Record<string, string> = Object.fromEntries(
  FONTS.map((f) => [f.value, f.css]),
);

/** slug → Google Fonts の {family, weights}。 */
export const CLOCK_GOOGLE_FONTS: Record<string, { family: string; weights: string }> =
  Object.fromEntries(FONTS.map((f) => [f.value, { family: f.family, weights: f.weights }]));

const DEFAULT_FONT_CSS = CLOCK_FONT_FAMILY_CSS["noto-sans-jp"];

/** slug → CSS font-family（未知 slug は既定にフォールバック）。 */
export function clockFontFamily(slug: string | undefined): string {
  return (slug && CLOCK_FONT_FAMILY_CSS[slug]) || DEFAULT_FONT_CSS;
}

const _loaded = new Set<string>();

/** プレビュー用に Google Font を <link> で動的ロードする（冪等）。
 * noto-sans-jp は studio 全体で既にロード済みのためスキップ。 */
export function loadClockGoogleFont(slug: string | undefined): void {
  if (typeof document === "undefined") return;
  if (!slug || slug === "noto-sans-jp" || _loaded.has(slug)) return;
  const f = CLOCK_GOOGLE_FONTS[slug];
  if (!f) return;
  _loaded.add(slug);
  const id = "gf-clock-" + slug;
  if (document.getElementById(id)) return;
  const link = document.createElement("link");
  link.id = id;
  link.rel = "stylesheet";
  link.href = "https://fonts.googleapis.com/css2?family=" +
    f.family.replace(/ /g, "+") +
    (f.weights ? ":wght@" + f.weights : "") +
    "&display=swap";
  document.head.appendChild(link);
}
