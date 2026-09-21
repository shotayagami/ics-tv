// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useState } from "react";

import { api } from "@icstv/api";
import { Button, Chip, ProgressBar, StatusPill } from "./atoms";
import { useToast } from "./toast";

import { type ChannelDetail, useNowTick } from "./hooks";

function pad(n: number): string {
  return (n < 10 ? "0" : "") + n;
}

function hhmm(ts: number): string {
  const d = new Date(ts * 1000);
  return pad(d.getHours()) + ":" + pad(d.getMinutes());
}

/** 番組情報 (チップ/タイトル/進行バー・カウントダウン/お気に入り・共有)。毎秒 tick で進行更新。 */
export function ProgramInfo({ detail, homeBase }: { detail: ChannelDetail; homeBase: string }) {
  const now = useNowTick();
  const toast = useToast();
  const [pinned, setPinned] = useState(detail.is_pinned);
  const cur = detail.current;
  const up = detail.upcoming;

  let pct = 0;
  let remainMin = 0;
  let countdown = "--:--";
  if (cur && cur.end_ts > cur.start_ts) {
    pct = Math.max(0, Math.min(100, ((now - cur.start_ts) / (cur.end_ts - cur.start_ts)) * 100));
    const rem = Math.max(0, Math.floor(cur.end_ts - now));
    countdown = pad(Math.floor(rem / 60)) + ":" + pad(rem % 60);
    remainMin = Math.max(0, Math.ceil(rem / 60));
  }

  async function togglePin() {
    const { data, response } = await api.POST("/api/v1/channels/{slug}/pin", {
      params: { path: { slug: detail.slug } },
    });
    if (response.status === 401) {
      window.location.href = `${homeBase}/members/login/?next=/ch/${detail.slug}/`;
      return;
    }
    if (data) setPinned(data.pinned);
  }

  function share() {
    const url = location.href;
    const title = cur?.title ?? detail.name;
    if (navigator.share) void navigator.share({ url, title }).catch(() => {});
    else
      void navigator.clipboard
        ?.writeText(url)
        .then(() => toast.success("URL をコピーしました"))
        .catch(() => toast.error("コピーに失敗しました"));
  }

  return (
    <div className="pinfo">
      <div className="chips">
        <Chip label={detail.name} color={detail.tint} />
        {cur?.genre && <Chip label={cur.genre} genre />}
        {cur ? (
          <>
            <StatusPill tone="live">放送中</StatusPill>
            {cur.is_rerun && <StatusPill tone="neutral">再放送</StatusPill>}
            <span className="st">
              {hhmm(cur.start_ts)}–{hhmm(cur.end_ts)}
            </span>
          </>
        ) : (
          <StatusPill tone="neutral">編成情報なし</StatusPill>
        )}
      </div>
      <h1>{cur ? cur.title : detail.name}</h1>
      {cur && (
        <div className="pcard">
          <div className="left">
            <ProgressBar pct={pct} />
            <div className="lbl">
              <span>あと {remainMin}分</span>
              <span>{up ? `次：${up.title} ${hhmm(up.start_ts)}` : ""}</span>
            </div>
          </div>
          <div className="right">
            <span className="t">次の番組まで</span>
            <span className="cd tabnum">{countdown}</span>
          </div>
        </div>
      )}
      <div className="actions-row">
        <Button variant="pin" active={pinned} onClick={togglePin}>
          {pinned ? "★ お気に入り済み" : "☆ よく見るに追加"}
        </Button>
        <Button variant="pin" onClick={share}>
          共有
        </Button>
      </div>
    </div>
  );
}
