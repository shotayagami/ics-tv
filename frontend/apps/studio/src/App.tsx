// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useState } from "react";
import { Navigate, Route, Routes } from "react-router-dom";

import { api } from "@icstv/api";
import { EmptyState, StudioLayout, StudioSidebar } from "./atoms";

import { RouterLink } from "./links";
import { useSidebarNav } from "./nav";

import { AccessStatsPage } from "./pages/AccessStatsPage";
import { BillingPage } from "./pages/BillingPage";
import { ChannelSettingsPage } from "./pages/ChannelSettingsPage";
import { ClockEditorPage } from "./pages/ClockEditorPage";
import { ChannelsPage } from "./pages/ChannelsPage";
import { CreatorDetailPage } from "./pages/CreatorDetailPage";
import { CreatorsPage } from "./pages/CreatorsPage";
import { CueSheetPage } from "./pages/CueSheetPage";
import { Dashboard } from "./pages/Dashboard";
import { GraphicCuesPage } from "./pages/GraphicCuesPage";
import { LiveRundownPage } from "./pages/LiveRundownPage";
import { RundownTemplatePage } from "./pages/RundownTemplatePage";
import { LiveSourcesPage } from "./pages/LiveSourcesPage";
import { MedialibPage } from "./pages/MedialibPage";
import { MembersStats } from "./pages/MembersStats";
import { ProgramFormPage } from "./pages/ProgramFormPage";
import { RightsDashboard } from "./pages/RightsDashboard";
import { SalesPage } from "./pages/SalesPage";
import { SeriesDetailPage } from "./pages/SeriesDetailPage";
import { SeriesFormsPage } from "./pages/SeriesFormsPage";
import { SeriesPage } from "./pages/SeriesPage";
import { SeriesPostsPage } from "./pages/SeriesPostsPage";
import { SeriesSubmissionsPage } from "./pages/SeriesSubmissionsPage";
import { SlotsPage } from "./pages/SlotsPage";
import { TimelinePage } from "./pages/TimelinePage";
import { WeekGridPage } from "./pages/WeekGridPage";
import { YouTubePresetsPage } from "./pages/YouTubePresetsPage";
import { YouTubeTemplatesPage } from "./pages/YouTubeTemplatesPage";
import { ProgramBroadcastPage } from "./pages/ProgramBroadcastPage";
import { ClockPresetsPage } from "./pages/ClockPresetsPage";
import { VideoStudioPage } from "./pages/VideoStudioPage";

/** ch 単位の画面 (/scheduling, /series) の index = 先頭チャンネルへリダイレクト。 */
function ChannelRedirect({ base }: { base: string }) {
  const [slug, setSlug] = useState<string | null>(null);
  const [empty, setEmpty] = useState(false);
  useEffect(() => {
    api
      .GET("/api/v1/admin/scheduling/channels")
      .then(({ data }) => (data && data.length ? setSlug(data[0].slug) : setEmpty(true)))
      .catch(() => setEmpty(true));
  }, []);
  if (empty) return <EmptyState loading>チャンネルがありません。</EmptyState>;
  if (!slug) return <EmptyState loading>読み込み中…</EmptyState>;
  return <Navigate to={`/${base}/${slug}`} replace />;
}

/** studio 管理 SPA のシェル: 左サイドバーナビ + ルート (nav.ts の編成・制作/コンテンツ/経営・権利/
 * 設定)。放送当直 (運用 ops) は別ホスト ops.* の 🔴放送コンソールへ分離 (リファクタ Phase 1.4)。
 * 未移行の旧 Django 画面へは footer の admin リンクで戻す。 */
export function App() {
  const { home, groups } = useSidebarNav();
  // 🔴放送コンソールは別ホスト ops.* (studio.*→ops.* のサブドメイン置換で導出)。
  const opsUrl = window.location.origin.replace("studio", "ops");
  return (
    <StudioLayout
      sidebar={
        <StudioSidebar
          home={home}
          groups={groups}
          footer={
            <>
              <a href={opsUrl}>🔴 放送コンソール ↗</a>
              <a href="/admin/">← Django admin</a>
            </>
          }
          linkComponent={RouterLink}
        />
      }
    >
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/scheduling" element={<ChannelRedirect base="scheduling" />} />
        <Route path="/scheduling/:slug" element={<TimelinePage />} />
        <Route path="/program-form/:slug" element={<ProgramFormPage />} />
        <Route path="/week" element={<ChannelRedirect base="week" />} />
        <Route path="/week/:slug" element={<WeekGridPage />} />
        <Route path="/series" element={<ChannelRedirect base="series" />} />
        <Route path="/series/:slug" element={<SeriesPage />} />
        <Route path="/series/:slug/new" element={<SeriesDetailPage />} />
        <Route path="/series/:slug/edit/:seriesId" element={<SeriesDetailPage />} />
        <Route path="/series/:channelSlug/forms/:seriesId" element={<SeriesFormsPage />} />
        <Route path="/series/:channelSlug/posts/:seriesId" element={<SeriesPostsPage />} />
        <Route path="/series/:channelSlug/submissions/:seriesId/:formId" element={<SeriesSubmissionsPage />} />
        <Route path="/graphic-cues/:slug/:owner/:ownerId" element={<GraphicCuesPage />} />
        <Route path="/slots" element={<ChannelRedirect base="slots" />} />
        <Route path="/slots/:slug" element={<SlotsPage />} />
        <Route path="/youtube/templates" element={<YouTubeTemplatesPage />} />
        <Route path="/youtube/presets" element={<YouTubePresetsPage />} />
        <Route path="/youtube/dedicated" element={<ChannelRedirect base="youtube/dedicated" />} />
        <Route path="/youtube/dedicated/:slug" element={<ProgramBroadcastPage />} />
        <Route path="/live-sources" element={<LiveSourcesPage />} />
        <Route path="/channels" element={<ChannelsPage />} />
        <Route path="/channels/:slug/settings" element={<ChannelSettingsPage />} />
        <Route path="/channels/:slug/clock" element={<ClockEditorPage />} />
        <Route path="/clock-presets" element={<ClockPresetsPage />} />
        <Route path="/medialib" element={<MedialibPage />} />
        <Route path="/cuesheet/:assetId" element={<CueSheetPage />} />
        <Route path="/live-rundown/:slug/:programId" element={<LiveRundownPage />} />
        <Route path="/rundown-template/:slug/:slotId" element={<RundownTemplatePage />} />
        <Route path="/members/stats" element={<MembersStats />} />
        <Route path="/rights" element={<RightsDashboard />} />
        <Route path="/billing" element={<BillingPage />} />
        <Route path="/sales" element={<SalesPage />} />
        <Route path="/video-studio" element={<VideoStudioPage />} />
        <Route path="/access-stats" element={<AccessStatsPage />} />
        <Route path="/creators" element={<CreatorsPage />} />
        <Route path="/creators/:creatorId" element={<CreatorDetailPage />} />
        <Route path="*" element={<Dashboard />} />
      </Routes>
    </StudioLayout>
  );
}
