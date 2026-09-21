// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { Link } from "react-router-dom";

import type { LinkLike } from "./atoms";

/** ナビ系アトム (ChannelPicker 等) に渡す react-router アダプタ。href→to でクライアントルーティング。 */
export const RouterLink: LinkLike = ({ href, className, children }) => (
  <Link to={href} className={className}>
    {children}
  </Link>
);
