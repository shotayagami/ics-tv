// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { ChannelTabs, ScheduleList, type ScheduleRow, type TabItem } from "./atoms";
import { ToastProvider } from "./toast";

import { Comments } from "./Comments";
import { useChannelData } from "./hooks";
import { ProgramInfo } from "./ProgramInfo";
import { VideoPlayer } from "./VideoPlayer";

export function PlayerPage({ slug, homeBase }: { slug: string; homeBase: string }) {
  const { detail, tabs } = useChannelData(slug);

  if (!detail) {
    return (
      <section className="player-page">
        <div className="player">
          <div className="placeholder">読み込み中…</div>
        </div>
      </section>
    );
  }

  const tabItems: TabItem[] = tabs.map((c) => ({
    slug: c.slug,
    name: c.name,
    tint: c.tint,
    href: `/ch/${c.slug}/`,
    active: c.slug === slug,
    pinned: c.pinned,
  }));
  const rows: ScheduleRow[] = detail.day_list.map((p) => ({
    id: p.id,
    time: p.time,
    title: p.title,
    genre: p.genre,
    color: p.color,
    isNow: p.is_now,
    isRerun: p.is_rerun,
    rowBg: p.row_bg,
    timeColor: p.time_color,
    titleColor: p.title_color,
    // 再放送 (フィラー由来の合成行) は番組詳細が無いので非リンク。
    href: p.is_rerun ? undefined : `${homeBase}/program/${p.id}/`,
  }));

  return (
    <ToastProvider>
    <section className="player-page">
      <ChannelTabs items={tabItems} homeHref={`${homeBase}/`} />
      <div className="player-body">
        <div className="player-main">
          <VideoPlayer
            hlsUrl={detail.hls_url ?? null}
            youtubeBroadcastId={detail.youtube_broadcast_id ?? null}
            channelShort={detail.short}
            channelTint={detail.tint}
            channelName={detail.name}
            slug={slug}
            paused={detail.paused ?? false}
            nextOnAir={detail.next_on_air ?? null}
            gateReason={detail.gate_reason || null}
            homeBase={homeBase}
            // fc_join の遷移先: 番組詳細ページ (/program/<id>/) は public_visible のみでゲート対象の
            // あらすじ/出演者/サムネを晒してしまうため使わず、series_url (ファンクラブ カードが
            // 正しくゲートされた状態で表示される) へ誘導する (#27 Phase B レビューで発見)。
            joinHref={detail.current?.series_url ? `${homeBase}${detail.current.series_url}` : null}
          />
          <ProgramInfo detail={detail} homeBase={homeBase} />
          <Comments slug={slug} homeBase={homeBase} />
        </div>
        <aside className="today">
          <div className="hd">
            <span className="ttl">{detail.short} ・ 本日の番組</span>
            <a href={`${homeBase}/guide/week/?ch=${slug}`}>週間 ›</a>
          </div>
          <div className="list">
            <ScheduleList items={rows} />
          </div>
        </aside>
      </div>
    </section>
    </ToastProvider>
  );
}
