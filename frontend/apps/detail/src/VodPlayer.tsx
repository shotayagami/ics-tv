// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useRef } from "react";

import { readCookie } from "./hooks";

interface Props {
  playUrl: string;
  resumeMs: number;
  progressUrl: string;
  poster: string;
  captionsUrl: string;
}

const CAPTION_PREF = "icstv:player:captions"; // "on" / "off" (ch 横断・PLAYER-06 字幕永続化)

/** 見逃し録画プレイヤー: 続きから再生 + 視聴位置の記録 (#PERS-02) + 字幕 (#PLAYER-04)。
 *
 * MP4 ネイティブ再生 (<video src>) に薄く乗せるだけ。署名URLは props 経由 (本体は no-store)。
 * 命令的な video 要素なので React は描画のみ担当し、再生制御は ref + イベントで行う。 */
export function VodPlayer({ playUrl, resumeMs, progressUrl, poster, captionsUrl }: Props) {
  const ref = useRef<HTMLVideoElement>(null);

  // 続きから再生: メタデータ確定後、終端付近でなければ resume 位置へ。
  useEffect(() => {
    const v = ref.current;
    if (!v || resumeMs <= 5000) return;
    const onMeta = () => {
      try {
        if (resumeMs < (v.duration - 5) * 1000) v.currentTime = resumeMs / 1000;
      } catch {
        /* duration 不明等は無視 */
      }
    };
    v.addEventListener("loadedmetadata", onMeta, { once: true });
    return () => v.removeEventListener("loadedmetadata", onMeta);
  }, [resumeMs]);

  // 視聴位置の記録: 再生中 15s 毎 + 一時停止 + タブ非表示で POST (会員のみ progressUrl が来る)。
  useEffect(() => {
    const v = ref.current;
    if (!v || !progressUrl) return;
    const report = () => {
      if (!v.duration || isNaN(v.duration)) return;
      const b = new FormData();
      b.append("position_ms", String(Math.floor(v.currentTime * 1000)));
      b.append("duration_ms", String(Math.floor(v.duration * 1000)));
      fetch(progressUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-CSRFToken": readCookie("csrftoken") },
        body: b,
        keepalive: true,
      }).catch(() => {});
    };
    const timer = window.setInterval(() => {
      if (!v.paused) report();
    }, 15000);
    const onHidden = () => {
      if (document.hidden) report();
    };
    v.addEventListener("pause", report);
    document.addEventListener("visibilitychange", onHidden);
    return () => {
      window.clearInterval(timer);
      v.removeEventListener("pause", report);
      document.removeEventListener("visibilitychange", onHidden);
    };
  }, [progressUrl]);

  // 字幕 (#PLAYER-04): 前回の ON/OFF を復元し、ユーザ操作 (ネイティブCC メニュー) で保存。
  // 自動生成字幕なので既定は OFF (CC ボタンから任意で ON、以後 ch 横断で永続)。
  useEffect(() => {
    const v = ref.current;
    if (!v || !captionsUrl) return;
    const tracks = v.textTracks;
    const readPref = () => {
      try {
        return localStorage.getItem(CAPTION_PREF);
      } catch {
        return null;
      }
    };
    const applyPref = () => {
      const t = tracks[0];
      if (!t) return;
      // 既定 OFF。保存が "on" のときだけ表示する。
      t.mode = readPref() === "on" ? "showing" : "disabled";
    };
    const onChange = () => {
      const t = tracks[0];
      if (!t) return;
      try {
        localStorage.setItem(CAPTION_PREF, t.mode === "showing" ? "on" : "off");
      } catch {
        /* localStorage 不可 (プライベート等) は無視 */
      }
    };
    // track がまだ無い場合は addtrack を待つ。
    tracks.addEventListener("addtrack", applyPref);
    tracks.addEventListener("change", onChange);
    applyPref();
    return () => {
      tracks.removeEventListener("addtrack", applyPref);
      tracks.removeEventListener("change", onChange);
    };
  }, [captionsUrl]);

  // 字幕 <track> は同一オリジン (相対パス) なので crossOrigin 不要。
  // crossOrigin を付けると R2 の署名URL(別オリジン)の src が credentialed CORS で失敗するため付けない。
  return (
    <video
      ref={ref}
      controls
      playsInline
      preload="metadata"
      poster={poster || undefined}
      src={playUrl}
    >
      {captionsUrl && (
        <track kind="subtitles" srcLang="ja" label="日本語（自動生成）" src={captionsUrl} />
      )}
    </video>
  );
}
