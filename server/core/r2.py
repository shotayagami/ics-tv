# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""R2 (Cloudflare R2 = S3 互換) クライアントの共用ラッパ。

docs/delivery.md「R2 クライアントは core へ切り出して共用する」。
medialib (mezzanine upload) と delivery (presigned 納品アップロード) の両方が使う。
env: R2_ENDPOINT_URL / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY / R2_REGION / R2_BUCKET。
"""

from __future__ import annotations

import os


def client():
    # boto3 は遅延 import (URLconf 解析時に未インストール環境で落ちないように)。実行時のみ必要。
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT_URL"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name=os.environ.get("R2_REGION", "auto"),
    )


def bucket() -> str:
    return os.environ["R2_BUCKET"]


# ---- presigned マルチパートアップロード (納品ポータル #5) ----


def create_multipart(key: str, content_type: str) -> str:
    """マルチパートアップロードを開始し upload_id を返す。"""
    resp = client().create_multipart_upload(Bucket=bucket(), Key=key, ContentType=content_type)
    return resp["UploadId"]


def presign_part(key: str, upload_id: str, part_number: int, expires: int = 3600) -> str:
    """1 パート分の presigned PUT URL (upload_part)。短命・パート単位。"""
    return client().generate_presigned_url(
        "upload_part",
        Params={
            "Bucket": bucket(),
            "Key": key,
            "UploadId": upload_id,
            "PartNumber": part_number,
        },
        ExpiresIn=expires,
    )


def list_multipart_parts(key: str, upload_id: str) -> list[dict]:
    """providerが保持するpart番号/ETag/Sizeを全ページ取得する。

    Sizeはmedialib.upload_services.complete_upload()が宣言サイズ(expected_size_bytes)との
    照合に使う (presign_partはContent-Lengthを署名しないため、宣言超過のpartをclientが書けてしまう
    ことへの対策・所有者決定F)。
    """
    c = client()
    marker: int | None = None
    parts: list[dict] = []
    while True:
        params: dict[str, object] = {"Bucket": bucket(), "Key": key, "UploadId": upload_id}
        if marker is not None:
            params["PartNumberMarker"] = marker
        response = c.list_parts(**params)
        parts.extend(
            {"PartNumber": item["PartNumber"], "ETag": item["ETag"], "Size": item["Size"]}
            for item in response.get("Parts", [])
        )
        if not response.get("IsTruncated"):
            return parts
        marker = response.get("NextPartNumberMarker")
        if marker is None:
            raise RuntimeError("truncated multipart part list did not include a marker")


def complete_multipart(key: str, upload_id: str, parts: list[dict]):
    """全パート完了。parts=[{"PartNumber": n, "ETag": "..."}]。"""
    return client().complete_multipart_upload(
        Bucket=bucket(),
        Key=key,
        UploadId=upload_id,
        MultipartUpload={"Parts": parts},
    )


def abort_multipart(key: str, upload_id: str):
    from botocore.exceptions import ClientError

    try:
        return client().abort_multipart_upload(Bucket=bucket(), Key=key, UploadId=upload_id)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "NoSuchUpload":
            return None
        raise


def delete_object(key: str):
    """R2 オブジェクトを削除 (納品ファイル削除時の実体掃除)。存在しなくても 2xx (冪等)。"""
    return client().delete_object(Bucket=bucket(), Key=key)


def put_object(key: str, body: bytes, content_type: str):
    """小さめのオブジェクト (サムネ等) を直接 PUT。マルチパート不要のもの用。"""
    return client().put_object(Bucket=bucket(), Key=key, Body=body, ContentType=content_type)


def get_object(key: str) -> tuple[bytes, str]:
    """オブジェクトの (bytes, content_type) を取得 (サムネのアプリ経由配信用)。"""
    r = client().get_object(Bucket=bucket(), Key=key)
    return r["Body"].read(), r.get("ContentType", "application/octet-stream")


def presign_get(key: str, expires: int = 600) -> str:
    """プレビュー/DL 用の期限付き署名 GET URL。"""
    return client().generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket(), "Key": key},
        ExpiresIn=expires,
    )


def presign_put(key: str, content_type: str, expires: int = 3600) -> str:
    """アップロード用の期限付き署名 PUT URL (agent の録画クリップ投入等)。"""
    return client().generate_presigned_url(
        "put_object",
        Params={"Bucket": bucket(), "Key": key, "ContentType": content_type},
        ExpiresIn=expires,
    )


def list_objects(prefix: str) -> list[dict]:
    """prefix 配下のオブジェクト一覧 (key/size/etag/last_modified)。ページング対応 (#22 天気予報取り込み)。

    last_modified は R2 サーバ時刻 (Cloudflare) の tz-aware datetime。外部機の時計を信用せず
    鮮度判定に使う (docs/normalize-offload.md — heartbeat/進捗の stale 判定)。
    """
    c = client()
    b = bucket()
    out: list[dict] = []
    token: str | None = None
    while True:
        kwargs: dict = {"Bucket": b, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        resp = c.list_objects_v2(**kwargs)
        for o in resp.get("Contents", []):
            out.append(
                {
                    "key": o["Key"],
                    "size": o.get("Size"),
                    "etag": o.get("ETag"),
                    "last_modified": o.get("LastModified"),
                }
            )
        if resp.get("IsTruncated"):
            token = resp.get("NextContinuationToken")
        else:
            break
    return out


def head_object(key: str) -> dict | None:
    """オブジェクトのメタ (last_modified/size) を取得。存在しなければ None。

    last_modified は R2 サーバ時刻の tz-aware datetime (外部機の時計に依存しない鮮度判定用)。
    """
    from botocore.exceptions import ClientError

    try:
        r = client().head_object(Bucket=bucket(), Key=key)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code in ("404", "NoSuchKey", "NotFound"):
            return None
        raise
    return {"last_modified": r.get("LastModified"), "size": r.get("ContentLength")}


def copy_object(src_key: str, dst_key: str, content_type: str | None = None):
    """同一バケット内のサーバサイド copy (ダウンロード/アップロードを介さない)。

    boto3 マネージド copy が大容量 (>5GB) は自動でマルチパート copy (UploadPartCopy) に切替える。
    正規化オフロードの成果物を out/ から最終 mezzanine/ キーへ昇格する用途。
    """
    extra: dict[str, str] = {}
    if content_type:
        extra["MetadataDirective"] = "REPLACE"
        extra["ContentType"] = content_type
    client().copy(
        {"Bucket": bucket(), "Key": src_key},
        bucket(),
        dst_key,
        ExtraArgs=extra or None,
    )
