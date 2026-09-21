// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useRef } from "react";

import Hls from "hls.js";

import { createHlsTokenLoader, hlsTokenOf, stripHlsToken } from "@icstv/api";

/**
 * チャンネルカード/ヒーローの常時ライブ映像 (HLS, muted autoplay)。home.html のバニラ実装を移植:
 * online/offline トグル + ライブ追従ウォッチドッグ (凍結→復帰)。12s ポーリングの再レンダでも
 * <video> は破棄されず再生を維持する。hlsUrl の署名トークン (#27) は再取得のたびに変わるため、
 * 作り直しの判定は token を除いた URL で行い、token はローダーが取得直前に差し替える。
 */
export function LiveBackground({
  hlsUrl,
  online,
  poster,
  logoSize,
}: {
  hlsUrl: string;
  online: boolean;
  poster: string;
  logoSize?: string;
}) {
  const vRef = useRef<HTMLVideoElement>(null);
  const hlsRef = useRef<Hls | null>(null);
  const onlineRef = useRef(online);
  onlineRef.current = online;
  const hlsKey = stripHlsToken(hlsUrl);
  const hlsUrlRef = useRef(hlsUrl);
  hlsUrlRef.current = hlsUrl;
  const hlsTokenRef = useRef("");
  hlsTokenRef.current = hlsTokenOf(hlsUrl);

  useEffect(() => {
    const v = vRef.current;
    if (!v || !hlsUrl) return;
    const play = () => {
      const p = v.play();
      if (p && p.catch) p.catch(() => {});
    };
    const seekLive = () => {
      try {
        const h = hlsRef.current;
        if (h && h.liveSyncPosition != null) v.currentTime = h.liveSyncPosition;
        else if (v.buffered.length) v.currentTime = v.buffered.end(v.buffered.length - 1);
      } catch {
        /* noop */
      }
    };
    const cleanups: Array<() => void> = [];

    if (v.canPlayType("application/vnd.apple.mpegurl")) {
      const onMeta = () => play();
      v.addEventListener("loadedmetadata", onMeta);
      cleanups.push(() => v.removeEventListener("loadedmetadata", onMeta));
      if (onlineRef.current) v.src = hlsUrlRef.current;
    } else if (Hls.isSupported()) {
      const hls = new Hls({
        lowLatencyMode: false,
        maxBufferLength: 10,
        backBufferLength: 10,
        liveSyncDurationCount: 3,
        loader: createHlsTokenLoader(Hls.DefaultConfig.loader, () => hlsTokenRef.current),
      });
      hlsRef.current = hls;
      hls.attachMedia(v);
      hls.on(Hls.Events.MEDIA_ATTACHED, () => {
        if (onlineRef.current) hls.loadSource(hlsUrlRef.current);
      });
      hls.on(Hls.Events.MANIFEST_PARSED, () => play());
      hls.on(Hls.Events.ERROR, (_e, d) => {
        const h = hlsRef.current;
        if (!d.fatal || !h) return;
        if (d.type === Hls.ErrorTypes.NETWORK_ERROR) {
          if (onlineRef.current) h.startLoad();
        } else if (d.type === Hls.ErrorTypes.MEDIA_ERROR) {
          h.recoverMediaError();
        } else {
          try {
            h.destroy();
          } catch {
            /* noop */
          }
          hlsRef.current = null;
        }
      });
    }

    // ライブ追従ウォッチドッグ (5s 周期で currentTime 前進を監視)。タブ非表示/offline は休止。
    let wdLast = -1;
    let wdStuck = 0;
    const wd = window.setInterval(() => {
      if (document.hidden || !onlineRef.current || v.paused || v.ended) {
        wdLast = v.currentTime;
        wdStuck = 0;
        return;
      }
      if (wdLast >= 0 && v.currentTime - wdLast < 0.25) {
        wdStuck++;
        const h = hlsRef.current;
        if (h) {
          try {
            h.startLoad();
          } catch {
            /* noop */
          }
        }
        seekLive();
        play();
        if (wdStuck >= 3 && h) {
          try {
            h.recoverMediaError();
          } catch {
            /* noop */
          }
          wdStuck = 0;
        }
      } else {
        wdStuck = 0;
      }
      wdLast = v.currentTime;
    }, 5000);
    cleanups.push(() => window.clearInterval(wd));

    return () => {
      cleanups.forEach((f) => f());
      const h = hlsRef.current;
      if (h) {
        try {
          h.destroy();
        } catch {
          /* noop */
        }
        hlsRef.current = null;
      }
    };
  }, [hlsKey]);

  // online トグル (ポーリングで変化)。video 要素は破棄せず再生/停止のみ切替える。
  useEffect(() => {
    const v = vRef.current;
    if (!v) return;
    if (online) {
      const h = hlsRef.current;
      if (h) {
        try {
          h.startLoad();
        } catch {
          /* noop */
        }
      } else if (hlsUrlRef.current && !v.getAttribute("src")) {
        v.src = hlsUrlRef.current;
      }
      const p = v.play();
      if (p && p.catch) p.catch(() => {});
    } else {
      try {
        v.pause();
      } catch {
        /* noop */
      }
    }
  }, [online, hlsUrl]);

  const posterSrc = online ? `${poster}?ts=${Math.floor(Date.now() / 1000 / 10)}` : "";

  return (
    <>
      <div className="live-frame" />
      <div className="scanline" />
      <div className="frame-logo">
        <span style={logoSize ? { fontSize: logoSize } : undefined}>ICS-TV</span>
      </div>
      {online && (
        <img
          className="liveimg"
          alt=""
          src={posterSrc}
          onError={(e) => {
            e.currentTarget.style.display = "none";
          }}
          onLoad={(e) => {
            e.currentTarget.style.display = "";
          }}
        />
      )}
      {hlsUrl && (
        <video
          ref={vRef}
          className={online ? "livevid" : "livevid off"}
          muted
          autoPlay
          playsInline
          preload="none"
          poster={poster}
        />
      )}
    </>
  );
}
