// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
/* 地図CGテンプレ共有: 射影 + 下地SVG + JMA色スケール。window.ICSTV_GEO (geo.js) 前提。
 * 生成ジオメトリ出典: 気象庁 予報区等GIS (政府標準利用規約=CC BY 相当。帰属は NOTICE)。 */
(function () {
  "use strict";
  var SVGNS = "http://www.w3.org/2000/svg";
  var GEO = window.ICSTV_GEO || { proj: {}, base: [], tsunami: {}, eew: {} };
  var P = GEO.proj;

  /** 緯度経度 → [x,y] 画素。build-geo.mjs と同一式 (焼き込みパスと実行時プロットの整合を保証)。 */
  function project(lat, lon) {
    return [P.offX + (lon - P.lonMin) * P.cosR * P.k, P.offY + (P.latMax - lat) * P.k];
  }
  function el(tag, attrs) {
    var e = document.createElementNS(SVGNS, tag);
    for (var k in attrs) if (attrs[k] != null) e.setAttribute(k, attrs[k]);
    return e;
  }
  /** 下地(都道府県界)を g に描く。 */
  function paintBase(g, opt) {
    opt = opt || {};
    GEO.base.forEach(function (d) {
      g.appendChild(
        el("path", {
          d: d,
          fill: opt.fill || "#8a9bb0",
          stroke: opt.stroke || "#59697e",
          "stroke-width": opt.sw != null ? opt.sw : 1,
          "stroke-linejoin": "round",
        }),
      );
    });
  }
  /** 津波予報区(海岸線)を code のパス群で描く。 */
  function paintTsunamiCoast(g, code, attrs) {
    (GEO.tsunami[code] || []).forEach(function (d) {
      g.appendChild(el("path", Object.assign({ d: d, fill: "none" }, attrs)));
    });
  }
  /** 緊急地震速報 予報区(面)を name(空白除去)のパス群で描く。 */
  function paintEewRegion(g, name, attrs) {
    var key = String(name || "").replace(/\s+/g, "");
    (GEO.eew[key] || []).forEach(function (d) {
      g.appendChild(el("path", Object.assign({ d: d }, attrs)));
    });
  }

  // 震度コード → 表示ラベル/上付き/配色 (ユーザ提供の震度別カラー準拠。7 のみ黄枠の紫)。
  var INT = {
    "1": { l: "1", bg: "#5a6b86", fg: "#fff" },
    "2": { l: "2", bg: "#3d7ba6", fg: "#fff" },
    "3": { l: "3", bg: "#1b6fd6", fg: "#fff" },
    "4": { l: "4", bg: "#e6b422", fg: "#2e2500" },
    "5-": { l: "5", s: "−", bg: "#f39100", fg: "#fff" },
    "5+": { l: "5", s: "＋", bg: "#e2680b", fg: "#fff" },
    "6-": { l: "6", s: "−", bg: "#e8380d", fg: "#fff" },
    "6+": { l: "6", s: "＋", bg: "#b3121a", fg: "#fff" },
    "7": { l: "7", bg: "#7a1fa2", fg: "#fff", border: "#ffd400" },
  };
  function intStyle(code) {
    return INT[code] || INT["1"];
  }
  /** 震度コード → 表示ラベル ("5-"→"5弱" 相当は数字+上付き記号で表現)。 */
  function intText(code) {
    var s = intStyle(code);
    return s.l + (s.s || "");
  }
  /** 震度の並べ替え用ランク (弱=.0/強=.5)。 */
  var INT_RANK = { "1": 1, "2": 2, "3": 3, "4": 4, "5-": 5, "5+": 5.5, "6-": 6, "6+": 6.5, "7": 7 };
  function intRank(code) {
    return INT_RANK[code] || 0;
  }

  // 津波区分 → 色 (紫=大津波警報 / 赤=津波警報 / 黄=津波注意報 / 淡青=津波予報)。
  var GRADE = { major: "#c026d3", warning: "#ff2323", advisory: "#ffdd00", forecast: "#7fd4ff" };
  function gradeColor(g) {
    return GRADE[g] || GRADE.forecast;
  }
  var GRADE_LABEL = { major: "大津波警報", warning: "津波警報", advisory: "津波注意報", forecast: "津波予報" };

  window.ICSTV_MAP = {
    GEO: GEO,
    project: project,
    el: el,
    paintBase: paintBase,
    paintTsunamiCoast: paintTsunamiCoast,
    paintEewRegion: paintEewRegion,
    intStyle: intStyle,
    intText: intText,
    intRank: intRank,
    gradeColor: gradeColor,
    GRADE_LABEL: GRADE_LABEL,
  };
})();
