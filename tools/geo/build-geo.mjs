#!/usr/bin/env node
// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * 地図(CG)テンプレ用ジオメトリ生成。GeoJSON(緯度経度) → 1920x1080 の SVG パスへ等距円筒射影。
 *
 * 出典/ライセンス:
 *   - 都道府県界・海岸線: 気象庁「地震情報／都道府県等」GIS
 *     (www.data.jma.go.jp/developer/gis.html, 20190125_AreaInformationPrefectureEarthquake_GIS)
 *   - 津波予報区(海岸線): 気象庁「津波予報区」GIS (20240520_AreaTsunami_GIS)
 *   - 緊急地震速報 地方/府県予報区: 気象庁「緊急地震速報／地方予報区・府県予報区」GIS
 *   いずれも政府標準利用規約(CC BY 相当)。出典明記で商用利用可。
 *
 * 使い方: node build-geo.mjs <tsunami.geojson> <pref.geojson> [eew-region.geojson] > geo.js
 * shp→geojson は mapshaper で事前生成:
 *   mapshaper in.shp encoding=shift_jis -simplify 0.6% keep-shapes -o format=geojson out.geojson
 *
 * 出力 geo.js は window.ICSTV_GEO に {proj, base, tsunami, eew} を載せる (CEF テンプレが読む)。
 * proj は緯度経度→画素の射影パラメータ。テンプレは同じ式で観測点 latlon を打つ (整合を保証)。
 */
import { readFileSync } from "node:fs";

const W = 1920;
const H = 1080;
const PAD = 8;
// 全国フレーム(主要4島+沖縄)。太平洋の孤立小島(南鳥島 154E 等)はフレーム外→viewBox でクリップ。
const FRAME = { lonMin: 122.9, lonMax: 146.3, latMin: 24.0, latMax: 45.6 };

const refLat = (FRAME.latMin + FRAME.latMax) / 2;
const cosR = Math.cos((refLat * Math.PI) / 180);
const spanX = (FRAME.lonMax - FRAME.lonMin) * cosR;
const spanY = FRAME.latMax - FRAME.latMin;
const k = Math.min((W - 2 * PAD) / spanX, (H - 2 * PAD) / spanY);
const offX = (W - spanX * k) / 2;
const offY = (H - spanY * k) / 2;

/** 緯度経度 → [x,y] 画素 (0.1px 丸め)。テンプレ側 JS も同一式を使う。 */
function project(lon, lat) {
  const x = offX + (lon - FRAME.lonMin) * cosR * k;
  const y = offY + (FRAME.latMax - lat) * k;
  return [Math.round(x * 10) / 10, Math.round(y * 10) / 10];
}

/** 座標配列(1リング) → "M x y L x y … " (面は Z 付き)。連続同一点は間引く。 */
function ringToPath(coords, close) {
  let d = "";
  let px = null;
  let py = null;
  for (const [lon, lat] of coords) {
    const [x, y] = project(lon, lat);
    if (x === px && y === py) continue;
    d += (d === "" ? "M" : "L") + x + " " + y;
    px = x;
    py = y;
  }
  return d ? d + (close ? "Z" : "") : "";
}

/** GeoJSON geometry → パス文字列配列 (Multi* を展開)。面=Polygon は閉じ、線=LineString は開いたまま。 */
function geomToPaths(geom) {
  const out = [];
  if (!geom || !geom.coordinates) return out;
  const push = (ring, close) => {
    const d = ringToPath(ring, close);
    if (d) out.push(d);
  };
  const t = geom.type;
  const c = geom.coordinates;
  if (t === "Polygon") c.forEach((r) => push(r, true));
  else if (t === "MultiPolygon") c.forEach((poly) => poly.forEach((r) => push(r, true)));
  else if (t === "LineString") push(c, false);
  else if (t === "MultiLineString") c.forEach((l) => push(l, false));
  return out;
}

function load(path) {
  return JSON.parse(readFileSync(path, "utf8")).features;
}

const [tsunamiPath, prefPath, eewPath] = process.argv.slice(2);

// 都道府県界 (下地・両マップ共通)。
const base = [];
for (const f of load(prefPath)) base.push(...geomToPaths(f.geometry));

// 津波予報区(海岸線) code→パス群。detail JSON の Area.Code と一致。
const tsunami = {};
for (const f of load(tsunamiPath)) {
  const code = String(f.properties.code);
  (tsunami[code] ||= []).push(...geomToPaths(f.geometry));
}

// 緊急地震速報 予報区(面) name→パス群。EEW は名称連携ゆえ name キー(空白除去で正規化)。
const eew = {};
if (eewPath) {
  for (const f of load(eewPath)) {
    const name = String(f.properties.name || "").replace(/\s+/g, "");
    if (!name) continue;
    (eew[name] ||= []).push(...geomToPaths(f.geometry));
  }
}

const proj = {
  W, H,
  lonMin: FRAME.lonMin, latMax: FRAME.latMax, cosR,
  k: Math.round(k * 1000) / 1000,
  offX: Math.round(offX * 10) / 10, offY: Math.round(offY * 10) / 10,
};

const payload = { proj, base, tsunami, eew };
process.stdout.write(
  "/* 生成物: tools/geo/build-geo.mjs。出典: 気象庁 予報区等GIS + 国土(政府標準利用規約=CC BY 相当)。手編集不可。 */\n" +
    "window.ICSTV_GEO=" + JSON.stringify(payload) + ";\n",
);
