// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { memo, useEffect, useRef } from "react";

import Hls from "hls.js";

import { createHlsTokenLoader, hlsTokenOf, stripHlsToken } from "@icstv/api";

import { LiveBadge } from "./atoms";

import { useHeartbeat } from "./hooks";

interface VideoPlayerProps {
  hlsUrl: string | null;
  youtubeBroadcastId: string | null;
  channelShort: string;
  channelTint: string;
  channelName: string;
  slug: string;
  // 放送休止中 (broadcast_windows 窓外)。true のとき hlsUrl は null で来る想定 (再生せず休止表示)。
  paused?: boolean;
  nextOnAir?: string | null;
  // exposure_policy のサイト会員限定 / ファンクラブ ティア限定 (#27 Phase B) ゲート理由
  // ('login' | 'subscribe' | 'fc_join' | 'fc_unavailable')。空/null は非ゲート。
  gateReason?: string | null;
  homeBase?: string;
  // fc_join 時の遷移先 (現在番組の詳細ページ。ファンクラブ加入導線を表示する)。無ければリンク無し表示。
  joinHref?: string | null;
}

interface QOpt {
  key: string;
  label: string;
  sub?: string;
  kind: "auto" | "video" | "audio";
  levelIndex?: number;
  url?: string;
}

const PLAY =
  '<svg width="20" height="20" viewBox="0 0 16 16"><path d="M3.5 2 L13.5 8 L3.5 14 Z" fill="#fff"></path></svg>';
const PAUSE =
  '<svg width="20" height="20" viewBox="0 0 16 16"><rect x="3" y="2" width="3.4" height="12" rx="1" fill="#fff"></rect><rect x="9.6" y="2" width="3.4" height="12" rx="1" fill="#fff"></rect></svg>';
const MUTED =
  '<svg width="20" height="20" viewBox="0 0 18 18"><path d="M2 6.5 H5 L9 3 V15 L5 11.5 H2 Z" fill="#fff"></path><path d="M12 6 L16 12 M16 6 L12 12" stroke="#fff" stroke-width="1.5" stroke-linecap="round"></path></svg>';
const UNMUTED =
  '<svg width="20" height="20" viewBox="0 0 18 18"><path d="M2 6.5 H5 L9 3 V15 L5 11.5 H2 Z" fill="#fff"></path><path d="M11.5 6 C13 7.5 13 10.5 11.5 12" fill="none" stroke="#fff" stroke-width="1.5" stroke-linecap="round"></path></svg>';

/**
 * 公開メインプレイヤー (CF HLS)。epg.html のバニラ実装を忠実移植:
 * 自動再生の muted フォールバック / 音量・ミュートの localStorage 記憶 (ザッピング維持) /
 * ライブ追従ウォッチドッグ / 画質・音声のみ切替 / PiP / keepAlive。
 * memo + page 安定 props で再レンダしない (命令的 DOM 操作と React 描画を衝突させない)。
 */
function VideoPlayerInner({
  hlsUrl,
  youtubeBroadcastId,
  channelShort,
  channelTint,
  channelName,
  slug,
  paused = false,
  nextOnAir = null,
  gateReason = null,
  homeBase = "",
  joinHref = null,
}: VideoPlayerProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const playRef = useRef<HTMLButtonElement>(null);
  const muteRef = useRef<HTMLButtonElement>(null);
  const volRef = useRef<HTMLInputElement>(null);
  const fullRef = useRef<HTMLButtonElement>(null);
  const pipRef = useRef<HTMLButtonElement>(null);
  const gearRef = useRef<HTMLButtonElement>(null);
  const settingsRef = useRef<HTMLDivElement>(null);
  const coverRef = useRef<HTMLDivElement>(null);
  const qlabelRef = useRef<HTMLSpanElement>(null);
  const seekRef = useRef<HTMLInputElement>(null);
  const liveRef = useRef<HTMLButtonElement>(null);

  useHeartbeat(slug, () => {
    if (document.hidden) return false;
    const v = videoRef.current;
    if (hlsUrl) return !!v && !v.paused && !v.ended; // HLS: 実再生中のみ
    if (youtubeBroadcastId) return true; // YouTube: 表示中を視聴とみなす
    return false;
  });

  // 署名トークン (#27) は API を再取得するたびに新しくなる。プレイヤーの作り直しは token を
  // 除いた URL が変わったときだけに限定し、token 自体は ref 経由でローダーへ渡して
  // リクエストごとに差し替える (詳細は @icstv/api の hlsAuth)。
  const hlsKey = hlsUrl ? stripHlsToken(hlsUrl) : null;
  const hlsUrlRef = useRef(hlsUrl);
  hlsUrlRef.current = hlsUrl;
  const hlsTokenRef = useRef("");
  hlsTokenRef.current = hlsUrl ? hlsTokenOf(hlsUrl) : "";

  useEffect(() => {
    const v = videoRef.current;
    const src = hlsUrlRef.current;
    if (!v || !src) return;

    const cleanups: Array<() => void> = [];
    const on = (
      t: EventTarget,
      ev: string,
      fn: EventListenerOrEventListenerObject,
      opts?: AddEventListenerOptions,
    ) => {
      t.addEventListener(ev, fn, opts);
      cleanups.push(() => t.removeEventListener(ev, fn, opts));
    };

    let hls: Hls | null = null;
    let isNative = false;

    // --- 音量/ミュートの前回設定 (localStorage・チャンネル横断) ---
    const AUDIO_PREF = "icstv:player:audio";
    let pref: { vol?: number; muted?: boolean } | null = null;
    try {
      pref = JSON.parse(localStorage.getItem(AUDIO_PREF) || "null");
    } catch {
      /* noop */
    }
    if (pref && typeof pref.vol === "number") v.volume = Math.max(0, Math.min(1, pref.vol));
    v.muted = !(pref && pref.muted === false && v.volume > 0); // 前回「音あり」なら unmuted で開始
    let pendingUnmute = false; // 自動再生がブロックされ暫定ミュート中か
    let userEngaged = false; // ユーザが一度でも操作したか
    let userPaused = false; // ユーザが明示的に一時停止したか (ブラウザ起因の不意な pause と区別)
    const savePref = () => {
      try {
        localStorage.setItem(AUDIO_PREF, JSON.stringify({ vol: v.volume, muted: v.muted }));
      } catch {
        /* noop */
      }
    };
    const restoreAudio = () => {
      if (!pendingUnmute) return;
      pendingUnmute = false;
      if (v.volume === 0) v.volume = (pref && pref.vol) || 1;
      v.muted = false;
    };

    const tryPlay = () => {
      userPaused = false; // 再生を意図 → 不意の pause 判定をリセット
      const p = v.play();
      if (p && p.catch)
        p.catch(() => {
          // 初回自動再生が unmuted で弾かれたときだけ muted で再生を確保し、解除は最初の gesture へ
          if (!v.muted && !userEngaged) {
            v.muted = true;
            pendingUnmute = true;
            const p2 = v.play();
            if (p2 && p2.catch) p2.catch(() => {});
          }
        });
    };
    const seekLive = () => {
      try {
        if (hls && hls.liveSyncPosition != null) v.currentTime = hls.liveSyncPosition;
        else if (v.buffered && v.buffered.length) v.currentTime = v.buffered.end(v.buffered.length - 1);
      } catch {
        /* noop */
      }
    };

    if (v.canPlayType("application/vnd.apple.mpegurl")) {
      isNative = true;
      v.src = src;
      on(v, "loadedmetadata", tryPlay);
    } else if (Hls.isSupported()) {
      // minAutoBitrate: 自動 ABR が音声のみ variant を選ばないよう下限を引く (音声のみは手動選択時のみ)
      hls = new Hls({
        lowLatencyMode: false,
        maxBufferLength: 30,
        // タイムシフト (#PLAYER-03): 巻き戻し窓ぶん再生済みセグメントを保持し再フェッチ無しで戻れる。
        backBufferLength: 90,
        liveSyncDurationCount: 3,
        minAutoBitrate: 150000,
        // manifest に焼かれた古い署名トークンのまま期限切れで止まらないよう、取得直前に差し替える。
        loader: createHlsTokenLoader(Hls.DefaultConfig.loader, () => hlsTokenRef.current),
      });
      hls.loadSource(src);
      hls.attachMedia(v);
      hls.on(Hls.Events.MANIFEST_PARSED, tryPlay);
      hls.on(Hls.Events.ERROR, (_e, data) => {
        if (!data || !data.fatal || !hls) return;
        if (data.type === Hls.ErrorTypes.NETWORK_ERROR) hls.startLoad();
        else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) hls.recoverMediaError();
        else {
          try {
            hls.destroy();
          } catch {
            /* noop */
          }
        }
      });
    }

    // --- タイムシフト (#PLAYER-03・決定#24): DVR 窓内で巻き戻し/一時停止/ライブ復帰 ---
    // behindLive のとき、下の自動 live 追従 (recover/watchdog/stalled/pendingLiveSync の seekLive) を
    // 抑止し、意図した巻き戻し位置を live 端へ引き戻さないようにする。凍結復帰 (再バッファ) 自体は残す。
    const LIVE_EPS = 4; // 端から 4s 以内は「ライブ」とみなす
    let behindLive = false;
    let seeking = false; // シークバー ドラッグ中は value 自動更新を止める
    const seek = seekRef.current;
    const liveBtn = liveRef.current;
    const liveEdge = () => {
      try {
        if (v.seekable && v.seekable.length) return v.seekable.end(v.seekable.length - 1);
        if (hls && hls.liveSyncPosition != null) return hls.liveSyncPosition;
      } catch {
        /* noop */
      }
      return v.currentTime;
    };
    const dvrStart = () => {
      try {
        if (v.seekable && v.seekable.length) return v.seekable.start(0);
      } catch {
        /* noop */
      }
      return Math.max(0, liveEdge() - 60);
    };
    const syncLiveBtn = () => {
      if (liveBtn) liveBtn.classList.toggle("at-live", !behindLive);
    };
    const refreshLive = () => {
      const edge = liveEdge();
      behindLive = edge - v.currentTime > LIVE_EPS;
      if (seek && !seeking) {
        const start = dvrStart();
        seek.min = String(Math.floor(start));
        seek.max = String(Math.ceil(edge));
        seek.value = String(v.currentTime);
        seek.disabled = edge - start < 3; // 窓が極小 (ほぼライブ端のみ) はシーク無効
      }
      syncLiveBtn();
    };
    if (seek) {
      on(seek, "pointerdown", () => {
        seeking = true;
      });
      // touch では pointerup が来ず pointercancel/change になることがあるため三重で解除 (フリーズ防止)。
      const endSeek = () => {
        seeking = false;
      };
      on(seek, "pointerup", endSeek);
      on(seek, "pointercancel", endSeek);
      on(seek, "change", endSeek);
      on(seek, "input", () => {
        const t = +seek.value;
        try {
          v.currentTime = t;
        } catch {
          /* noop */
        }
        behindLive = liveEdge() - t > LIVE_EPS;
        syncLiveBtn();
        if (v.paused && !userPaused) tryPlay();
      });
    }
    if (liveBtn)
      on(liveBtn, "click", () => {
        behindLive = false;
        seekLive();
        if (v.paused) {
          userPaused = false;
          tryPlay();
        }
        syncLiveBtn();
      });
    const liveTick = window.setInterval(refreshLive, 500);
    cleanups.push(() => window.clearInterval(liveTick));
    refreshLive();

    // 非アクティブ復帰の一元化。userPaused (本人の一時停止) は尊重し、止まっていれば追従再開。
    // behindLive のときは live 端へ引き戻さず現在位置で再バッファするだけ (巻き戻し位置を保持)。
    // force=true は復帰イベント用 (visibilitychange/pageshow)、focus/pause は paused 時のみ。
    const recover = (force: boolean) => {
      if (userPaused) return;
      if (!force && !v.paused) return;
      if (hls) {
        try {
          hls.startLoad();
        } catch {
          /* noop */
        }
      }
      if (!behindLive) seekLive();
      tryPlay();
    };
    // タブ表示復帰 (visibilitychange) に加え、別ウィンドウへフォーカスが移って戻ったケース
    // (タブは hidden にならず visibilitychange が発火しない) を window focus で拾う。bfcache 復帰は pageshow。
    on(document, "visibilitychange", () => {
      if (!document.hidden) recover(true);
    });
    on(window, "focus", () => recover(false));
    on(window, "pageshow", () => recover(true));
    // タブ可視中にブラウザ起因で不意に pause したら再開 (本人の一時停止は userPaused で除外)。
    on(v, "pause", () => {
      if (!document.hidden) recover(false);
    });
    on(v, "stalled", () => {
      // behindLive: live へ飛ばさず現在位置で再バッファ (巻き戻し位置を保持)。
      if (behindLive) {
        if (hls) {
          try {
            hls.startLoad();
          } catch {
            /* noop */
          }
        }
        tryPlay();
      } else {
        seekLive();
      }
    });
    on(v, "ended", tryPlay);

    // --- ライブ追従ウォッチドッグ (currentTime 凍結を 5s 周期で検出→復帰) ---
    let wdLast = -1;
    let wdStuck = 0;
    const wd = window.setInterval(() => {
      if (v.paused || v.ended) {
        wdLast = v.currentTime;
        wdStuck = 0;
        return;
      }
      if (wdLast >= 0 && v.currentTime - wdLast < 0.25) {
        wdStuck++;
        if (hls) {
          try {
            hls.startLoad();
          } catch {
            /* noop */
          }
        }
        if (!behindLive) seekLive(); // behindLive: 凍結は現在位置で再バッファ (live へ飛ばさない)
        tryPlay();
        if (wdStuck >= 2 && hls) {
          try {
            hls.recoverMediaError();
          } catch {
            /* noop */
          }
        }
      } else {
        wdStuck = 0;
      }
      wdLast = v.currentTime;
    }, 5000);
    cleanups.push(() => window.clearInterval(wd));

    // --- keepAlive (無音 AudioContext で省電力スリープ抑止) + 初回 gesture 処理 ---
    let ac: AudioContext | null = null;
    const keepAlive = () => {
      try {
        if (!ac) {
          const Ctx = window.AudioContext || (window as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
          if (!Ctx) return;
          ac = new Ctx();
          const osc = ac.createOscillator();
          const g = ac.createGain();
          g.gain.value = 0;
          osc.connect(g);
          g.connect(ac.destination);
          osc.start();
        }
        if (ac.state === "suspended") void ac.resume();
      } catch {
        /* noop */
      }
    };
    const onGesture = (e: Event) => {
      keepAlive();
      userEngaged = true;
      const tgt = e.target as Element | null;
      if (!(tgt && tgt.closest && tgt.closest("#icstv-mute, #icstv-vol"))) restoreAudio();
      if (v.paused) tryPlay();
    };
    ["pointerdown", "keydown", "touchstart"].forEach((ev) => on(document, ev, onGesture, { passive: true }));
    on(v, "playing", keepAlive);
    cleanups.push(() => {
      try {
        void ac?.close();
      } catch {
        /* noop */
      }
    });

    // --- コントロールバー (再生/ミュート/音量/全画面) ---
    const bp = playRef.current;
    const bm = muteRef.current;
    const bf = fullRef.current;
    const vol = volRef.current;
    let lastVol = pref && typeof pref.vol === "number" && pref.vol > 0 ? pref.vol : 1;
    const syncPlay = () => {
      if (bp) bp.innerHTML = v.paused ? PLAY : PAUSE;
    };
    const syncMute = () => {
      if (bm) bm.innerHTML = v.muted || v.volume === 0 ? MUTED : UNMUTED;
    };
    const syncVol = () => {
      if (vol) vol.value = String(v.muted ? 0 : v.volume);
      syncMute();
    };
    syncPlay();
    syncVol();
    on(v, "play", syncPlay);
    on(v, "pause", syncPlay);
    on(v, "volumechange", syncVol);
    if (bp)
      on(bp, "click", () => {
        if (v.paused) tryPlay();
        else {
          userPaused = true; // 明示的な一時停止 → recover で勝手に再開しない
          v.pause();
        }
      });
    if (bm)
      on(bm, "click", () => {
        if (v.muted || v.volume === 0) {
          v.muted = false;
          if (v.volume === 0) v.volume = lastVol || 1;
        } else {
          lastVol = v.volume;
          v.muted = true;
        }
        pendingUnmute = false;
        savePref();
      });
    if (vol)
      on(vol, "input", () => {
        const val = +vol.value;
        v.volume = val;
        v.muted = val === 0;
        if (val > 0) lastVol = val;
        pendingUnmute = false;
        savePref();
      });
    if (bf)
      on(bf, "click", () => {
        const box = v.closest(".player");
        if (document.fullscreenElement) void document.exitFullscreen();
        else if (box && box.requestFullscreen) void box.requestFullscreen();
      });

    // --- ピクチャインピクチャ (標準 → webkit フォールバック → 非対応は degrade) ---
    const bpip = pipRef.current;
    if (bpip) {
      const vAny = v as unknown as Record<string, unknown>;
      const canStd = !!(
        document.pictureInPictureEnabled &&
        typeof v.requestPictureInPicture === "function" &&
        !v.disablePictureInPicture
      );
      const canWebkit =
        typeof vAny.webkitSetPresentationMode === "function" &&
        typeof vAny.webkitSupportsPresentationMode === "function" &&
        (vAny.webkitSupportsPresentationMode as (m: string) => boolean)("picture-in-picture");
      if (!canStd && !canWebkit) {
        bpip.hidden = true;
      } else {
        bpip.hidden = false;
        on(bpip, "click", () => {
          try {
            if (canStd) {
              if (document.pictureInPictureElement) void document.exitPictureInPicture();
              else void v.requestPictureInPicture();
            } else {
              const mode = vAny.webkitPresentationMode === "picture-in-picture" ? "inline" : "picture-in-picture";
              (vAny.webkitSetPresentationMode as (m: string) => void)(mode);
            }
          } catch {
            /* noop */
          }
        });
      }
    }

    // --- 画質切替 + 音声のみ ---
    const panel = settingsRef.current;
    const gear = gearRef.current;
    const cover = coverRef.current;
    const qlabel = qlabelRef.current;
    if (panel && gear) {
      let opts: QOpt[] = [];
      let curKey = "auto";
      let pendingLiveSync = false;

      const QUALITY_PREF = "icstv:player:quality";
      let qsaved: { kind?: string; label?: string } | null = null;
      try {
        qsaved = JSON.parse(localStorage.getItem(QUALITY_PREF) || "null");
      } catch {
        /* noop */
      }
      const saveQuality = (o: QOpt) => {
        try {
          localStorage.setItem(QUALITY_PREF, JSON.stringify({ kind: o.kind, label: o.label }));
        } catch {
          /* noop */
        }
      };
      const resolveSavedKey = (): string | null => {
        if (!qsaved || !qsaved.kind) return null;
        for (const o of opts) {
          if (o.kind !== qsaved.kind) continue;
          if (o.kind === "video" && o.label !== qsaved.label) continue;
          return o.key;
        }
        return null;
      };
      const resolveUrl = (base: string, rel: string) => {
        try {
          return new URL(rel, base).href;
        } catch {
          return rel;
        }
      };
      const setCover = (active: boolean) => {
        if (cover) cover.hidden = !active;
      };
      const setLabel = (t: string) => {
        if (qlabel) qlabel.textContent = t;
      };

      const render = () => {
        panel.innerHTML = "";
        const h = document.createElement("div");
        h.className = "shd";
        h.textContent = "画質";
        panel.appendChild(h);
        opts.forEach((o) => {
          const b = document.createElement("button");
          b.className = "opt" + (o.key === curKey ? " sel" : "");
          b.setAttribute("role", "menuitemradio");
          b.innerHTML =
            '<svg class="ck" width="14" height="14" viewBox="0 0 14 14"><path d="M2 7.5 L6 11 L12 3" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"></path></svg>' +
            "<span>" +
            o.label +
            "</span>" +
            (o.sub ? '<span class="sub">' + o.sub + "</span>" : "");
          b.addEventListener("click", () => {
            select(o.key);
            saveQuality(o);
            panel.hidden = true;
          });
          panel.appendChild(b);
        });
      };

      const apply = (o: QOpt) => {
        if (isNative) {
          const want = o.kind === "auto" ? src : o.url;
          if (want && v.src !== want && v.currentSrc !== want) {
            v.src = want;
            tryPlay();
          }
        } else if (hls) {
          if (o.kind === "auto") {
            hls.currentLevel = -1;
          } else if (o.levelIndex != null) {
            hls.currentLevel = o.levelIndex;
            pendingLiveSync = true;
          }
        }
      };

      const select = (key: string) => {
        const o = opts.find((x) => x.key === key);
        if (!o) return;
        curKey = key;
        apply(o);
        setCover(o.kind === "audio");
        setLabel(o.kind === "auto" ? "自動" : o.label);
        render();
      };

      const publish = () => {
        let vids = 0;
        let hasAudio = false;
        for (const o of opts) {
          if (o.kind === "video") vids++;
          if (o.kind === "audio") hasAudio = true;
        }
        if (vids <= 1 && !hasAudio) {
          gear.hidden = true;
          return;
        }
        gear.hidden = false;
        if (curKey === "auto") {
          const sk = resolveSavedKey();
          if (sk) curKey = sk;
        }
        if (!opts.some((o) => o.key === curKey)) curKey = "auto";
        select(curKey);
      };

      const buildFromHls = () => {
        if (!hls || !hls.levels) return;
        const L = hls.levels;
        const vids: Array<{ i: number; h: number }> = [];
        let audioIdx = -1;
        for (let i = 0; i < L.length; i++) {
          if (L[i].height && L[i].height > 0) vids.push({ i, h: L[i].height });
          else if (!L[i].videoCodec) audioIdx = i;
        }
        vids.sort((a, b) => b.h - a.h);
        opts = [{ key: "auto", label: "自動", kind: "auto" }];
        vids.forEach((x) => opts.push({ key: "v" + x.i, label: x.h + "p", kind: "video", levelIndex: x.i }));
        if (audioIdx >= 0) opts.push({ key: "audio", label: "音声のみ", kind: "audio", levelIndex: audioIdx });
        publish();
      };

      const buildFromNative = () => {
        fetch(src, { credentials: "omit" })
          .then((r) => r.text())
          .then((txt) => {
            const lines = txt.split(/\r?\n/);
            const vids: Array<{ h: number; url: string }> = [];
            let audio: { url: string } | null = null;
            for (let i = 0; i < lines.length; i++) {
              if (lines[i].indexOf("#EXT-X-STREAM-INF") !== 0) continue;
              const attrs = lines[i].slice(lines[i].indexOf(":") + 1);
              let uri = "";
              for (let j = i + 1; j < lines.length; j++) {
                const u = lines[j].trim();
                if (u && u.charAt(0) !== "#") {
                  uri = u;
                  break;
                }
              }
              if (!uri) continue;
              const abs = resolveUrl(src, uri);
              const res = /RESOLUTION=\d+x(\d+)/.exec(attrs);
              const codecs = /CODECS="([^"]*)"/.exec(attrs);
              const hasVideo = res || (codecs && /avc|hvc|hev|mp4v/i.test(codecs[1]));
              if (res) vids.push({ h: parseInt(res[1], 10), url: abs });
              else if (!hasVideo) audio = { url: abs };
            }
            vids.sort((a, b) => b.h - a.h);
            opts = [{ key: "auto", label: "自動", kind: "auto" }];
            vids.forEach((x, k) => opts.push({ key: "v" + k, label: x.h + "p", kind: "video", url: x.url }));
            if (audio) opts.push({ key: "audio", label: "音声のみ", kind: "audio", url: audio.url });
            publish();
          })
          .catch(() => {});
      };

      on(gear, "click", (e) => {
        e.stopPropagation();
        panel.hidden = !panel.hidden;
      });
      on(document, "click", (e) => {
        if (panel.hidden) return;
        const tgt = e.target as Node;
        if (panel.contains(tgt) || gear.contains(tgt)) return;
        panel.hidden = true;
      });

      if (isNative) buildFromNative();
      else if (hls) {
        hls.on(Hls.Events.MANIFEST_PARSED, buildFromHls);
        hls.on(Hls.Events.LEVEL_SWITCHED, (_e, d) => {
          if (pendingLiveSync) {
            pendingLiveSync = false;
            if (!behindLive) seekLive(); // 巻き戻し中の画質切替は位置を保持
          }
          if (curKey === "auto" && hls && hls.levels[d.level]) {
            const hgt = hls.levels[d.level].height;
            if (hgt) setLabel("自動 " + hgt + "p");
          }
        });
      }
    }

    return () => {
      cleanups.forEach((fn) => fn());
      if (hls) {
        try {
          hls.destroy();
        } catch {
          /* noop */
        }
      }
    };
  }, [hlsKey]);

  // --- コントロール オートハイド: マウス静止/離脱で LIVE 表示と操作バーをフェードアウト、
  //     ポインタの動きでフェードイン。.player へ controls-hidden を付け外しし、見た目は CSS が担う。
  //     iframe(YouTube)は内部の動きを拾えず badge が隠れたままになるため HLS のみ対象。 ---
  useEffect(() => {
    if (!hlsUrl) return;
    const box = containerRef.current;
    if (!box) return;
    const HIDE_MS = 2800;
    let timer = 0;
    const hide = () => {
      const panel = settingsRef.current;
      const v = videoRef.current;
      if (panel && !panel.hidden) return; // 設定パネル展開中は維持
      if (v && v.paused) return; // 一時停止中は維持
      box.classList.add("controls-hidden");
    };
    const schedule = () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(hide, HIDE_MS);
    };
    const show = () => {
      box.classList.remove("controls-hidden");
      schedule();
    };
    const onLeave = (e: PointerEvent) => {
      if (e.pointerType === "touch") return; // タッチに「離れる」概念はない(tap直後の誤フェード回避)
      window.clearTimeout(timer);
      hide();
    };
    const v = videoRef.current;
    box.addEventListener("pointermove", show);
    box.addEventListener("pointerdown", show);
    box.addEventListener("pointerleave", onLeave);
    if (v) v.addEventListener("play", schedule); // 再生開始で無操作カウントダウン開始
    schedule();
    return () => {
      window.clearTimeout(timer);
      box.removeEventListener("pointermove", show);
      box.removeEventListener("pointerdown", show);
      box.removeEventListener("pointerleave", onLeave);
      if (v) v.removeEventListener("play", schedule);
    };
  }, [hlsKey]);

  // --- 描画: HLS / YouTube 埋め込み / 準備中 を出し分け ---
  return (
    <div className="player" ref={containerRef}>
      {hlsUrl ? (
        <video ref={videoRef} id="icstv-player" playsInline poster="" />
      ) : youtubeBroadcastId ? (
        <iframe
          src={`https://www.youtube.com/embed/${youtubeBroadcastId}?autoplay=1&mute=1`}
          title="live"
          allow="autoplay; encrypted-media"
          allowFullScreen
        />
      ) : (
        <>
          <div className="live-frame" />
          <div className="scanline" />
          <div className="frame-logo">
            <span style={{ fontSize: "clamp(40px,8vw,96px)", fontWeight: 800 }}>ICS-TV</span>
          </div>
          <div className="placeholder">
            {paused ? (
              `放送休止中${nextOnAir ? ` ・ 次回 ${nextOnAir} から` : ""}`
            ) : gateReason === "login" ? (
              <>
                この番組はログインが必要です。
                <a href={`${homeBase}/members/login/?next=/ch/${slug}/`}>ログインして視聴</a>
              </>
            ) : gateReason === "subscribe" ? (
              <>
                この番組は会員限定コンテンツです。
                <a href={`${homeBase}/subscriptions/`}>ご登録はこちら</a>
              </>
            ) : gateReason === "fc_join" ? (
              <>
                この番組はファンクラブ限定です。
                {joinHref ? <a href={joinHref}>加入について見る</a> : null}
              </>
            ) : gateReason === "fc_unavailable" ? (
              "この番組は現在ご視聴いただけません"
            ) : (
              "準備中"
            )}
          </div>
        </>
      )}

      <LiveBadge />
      <div className="bug">
        <i style={{ background: channelTint }} />
        {channelShort}
      </div>

      {hlsUrl && (
        <>
          <div className="audio-cover" ref={coverRef} hidden>
            <div className="ring" style={{ background: channelTint }}>
              {channelShort}
            </div>
            <div className="wav">
              {[0, 0.15, 0.3, 0.15, 0].map((d, i) => (
                <i key={i} style={{ animationDelay: `${d}s` }} />
              ))}
            </div>
            <div>
              <div className="t">{channelName}</div>
              <div className="s">音声のみで再生中</div>
            </div>
          </div>
          <div className="settings" ref={settingsRef} hidden role="menu" aria-label="設定" />
          <div className="ctrl">
            {/* タイムシフト シークバー (#PLAYER-03): DVR 窓内で巻き戻し。窓が極小なら disabled。 */}
            <input
              ref={seekRef}
              className="seek"
              type="range"
              min={0}
              max={0}
              step={1}
              defaultValue={0}
              aria-label="シーク (巻き戻し)"
            />
            <div className="bar">
              <button ref={playRef} id="icstv-play" type="button" aria-label="再生/一時停止" />
              <button ref={liveRef} className="live-pill" type="button" aria-label="ライブに戻る">
                LIVE
              </button>
              <span className="vol-wrap">
                <button ref={muteRef} id="icstv-mute" type="button" aria-label="ミュート切替" />
                <input
                  ref={volRef}
                  id="icstv-vol"
                  className="vol"
                  type="range"
                  min={0}
                  max={1}
                  step={0.01}
                  defaultValue={0}
                  aria-label="音量"
                />
              </span>
              <span className="hd js-qlabel" ref={qlabelRef}>
                HD
              </span>
              <button ref={gearRef} type="button" aria-label="設定" aria-haspopup="true" hidden>
                <svg width="19" height="19" viewBox="0 0 20 20">
                  <path d="M10 7a3 3 0 100 6 3 3 0 000-6z" fill="none" stroke="#fff" strokeWidth="1.5" />
                  <path
                    d="M10 2.5l1 2 2.2-.3.6 2.1 2 1-.9 2 .9 2-2 1-.6 2.1-2.2-.3-1 2-1-2-2.2.3-.6-2.1-2-1 .9-2-.9-2 2-1 .6-2.1 2.2.3z"
                    fill="none"
                    stroke="#fff"
                    strokeWidth="1.3"
                    strokeLinejoin="round"
                  />
                </svg>
              </button>
              <button ref={pipRef} type="button" aria-label="ピクチャインピクチャ" hidden>
                <svg width="19" height="19" viewBox="0 0 18 18">
                  <rect x="2" y="3.5" width="14" height="11" rx="1.5" fill="none" stroke="#fff" strokeWidth="1.5" />
                  <rect x="9" y="8.5" width="6" height="4.5" rx="1" fill="#fff" />
                </svg>
              </button>
              <button ref={fullRef} type="button" aria-label="全画面">
                <svg width="19" height="19" viewBox="0 0 18 18">
                  <path
                    d="M2 6 V2 H6 M16 6 V2 H12 M2 12 V16 H6 M16 12 V16 H12"
                    fill="none"
                    stroke="#fff"
                    strokeWidth="1.6"
                    strokeLinecap="round"
                  />
                </svg>
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

export const VideoPlayer = memo(VideoPlayerInner);
