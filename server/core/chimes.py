# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""速報チャイム音源ライブラリの R2 保存 / 選択 (layer41・速報テロップと同時発火)。

ライブラリ (ChimeSound) に音源を貯め、カテゴリ別 (ChannelChime) に「どれを使うか」を選ぶ 2 層。
音声は正規化せず **そのまま** R2 に保存する (core.thumbnails と同型)。key は uuid 付きの
content-addressed (`chime/lib/<uuid>.<ext>`) で、clip 名が一意になり送出ノードの古いキャッシュ事故を
避ける。配布は slate と同じ standing prefetch manifest 経由でライブラリ全体を agent が pin+DL する。
"""

from __future__ import annotations

import contextlib
import uuid

from core import r2
from core.models import ChannelChime, ChimeCategory, ChimeSound

CHIME_PREFIX = "chime/lib/"
MAX_BYTES = 5 * 1024 * 1024  # 5MB (効果音は数秒・小容量)
# content-type → 拡張子。CasparCG (FFmpeg producer) が再生できる音声形式に限る。
_ALLOWED = {
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/mp4": "m4a",
    "audio/aac": "aac",
    "audio/x-aac": "aac",
    "audio/ogg": "ogg",
    "audio/webm": "webm",
    "audio/flac": "flac",
}


class ChimeError(ValueError):
    pass


def _ext_for(django_file) -> str:
    content_type = (getattr(django_file, "content_type", "") or "").lower()
    ext = _ALLOWED.get(content_type)
    if ext:
        return ext
    # content-type が曖昧なブラウザ向けに拡張子からも判定 (例 application/octet-stream)。
    name = (getattr(django_file, "name", "") or "").lower()
    for e in ("mp3", "wav", "m4a", "aac", "ogg", "webm", "flac"):
        if name.endswith("." + e):
            return e
    raise ChimeError("音声は MP3 / WAV / M4A / AAC / OGG / WebM / FLAC のみ対応です")


def add_to_library(name: str, django_file) -> ChimeSound:
    """音源をライブラリに登録 (R2 保存 + ChimeSound 作成)。検証 NG は ChimeError。"""
    ext = _ext_for(django_file)
    if django_file.size and django_file.size > MAX_BYTES:
        raise ChimeError("ファイルサイズは 5MB 以内にしてください")
    label = (name or "").strip() or (getattr(django_file, "name", "") or "チャイム").strip()
    key = f"{CHIME_PREFIX}{uuid.uuid4().hex}.{ext}"
    content_type = (getattr(django_file, "content_type", "") or "").lower() or "audio/mpeg"
    r2.put_object(key, django_file.read(), content_type)
    return ChimeSound.objects.create(
        name=label[:120],
        r2_key=key,
        original_filename=(getattr(django_file, "name", "") or "")[:255],
        content_type=content_type[:64],
        size_bytes=int(django_file.size or 0),
    )


def remove_from_library(sound_id: int) -> bool:
    """ライブラリから音源を削除 (R2 オブジェクトも消す)。選択中なら ChannelChime.sound は
    on_delete=SET_NULL で自動的に未選択へ戻る。存在すれば True。"""
    sound = ChimeSound.objects.filter(pk=sound_id).first()
    if sound is None:
        return False
    key = sound.r2_key
    sound.delete()
    _safe_delete(key)
    return True


def select(channel, category: str, sound_id: int | None) -> ChannelChime:
    """カテゴリ (eew|weather|general) に使う音源を選ぶ。sound_id=None で未選択 (既定へフォールバック)。"""
    cat = (category or "").strip().lower()
    if cat not in ChimeCategory.values:
        raise ChimeError(f"不正なカテゴリ: {category}")
    sound = ChimeSound.objects.filter(pk=sound_id).first() if sound_id else None
    if sound_id and sound is None:
        raise ChimeError("指定の音源が見つかりません")
    obj, _ = ChannelChime.objects.update_or_create(
        channel=channel, category=cat, defaults={"sound": sound}
    )
    return obj


def _safe_delete(key: str) -> None:
    # R2 削除失敗で UI を止めない (孤児オブジェクトは手動掃除可)。
    with contextlib.suppress(Exception):
        r2.delete_object(key)
