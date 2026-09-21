// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { Link } from "react-router-dom";

import type { LinkLike } from "./atoms";

/** @icstv/studio-ui のナビ系アトム(StudioSidebar/ChannelPicker/SubNav/Breadcrumb)に渡す
 * react-router アダプタ。href→to に写し、フルリロードでなくクライアントルーティングにする。
 * active は呼び出し側(atom が active/activeSlug から className を決める。サイドバーは
 * nav.ts の useSidebarNav が現在ルートから boolean を算出)。 */
export const RouterLink: LinkLike = ({ href, className, children }) => (
  <Link to={href} className={className}>
    {children}
  </Link>
);
