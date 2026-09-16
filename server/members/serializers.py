# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""会員系の API/WS 共有シリアライザ。

コメントの dict 表現を GET 一覧 / POST 応答 / WS ブロードキャストで統一する
(api.schemas.CommentItem と同形)。body は生テキストで返し、エスケープ/改行整形は
クライアント (React) が描画時に行う (サーバで HTML を組まない = XSS 面が小さい)。
"""

from django.utils import timezone


def comment_payload(comment, *, badge: bool) -> dict:
    lt = timezone.localtime(comment.created_at)
    return {
        "id": comment.id,
        "member_id": comment.member_id,
        "nickname": comment.member.nickname,
        "badge": badge,
        "created_ts": int(comment.created_at.timestamp()),
        # 表示用 "n/j H:i" (Django date filter と同形・先頭ゼロなし月日)
        "time": f"{lt.month}/{lt.day} {lt:%H:%M}",
        "program_title": comment.program_title or "",
        "body": comment.body,
    }
