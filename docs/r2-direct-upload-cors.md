# R2 direct-upload CORS contract

Studio の multipart upload は、同一オリジンの API で短寿命の署名 URL を取得し、ブラウザから R2 へ `PUT` します。R2 bucket には Studio の実オリジンを明示した次の CORS rule が必要です。

```json
[
  {
    "AllowedOrigins": ["https://studio.example.invalid"],
    "AllowedMethods": ["PUT"],
    "AllowedHeaders": ["content-type"],
    "ExposeHeaders": ["ETag"],
    "MaxAgeSeconds": 300
  }
]
```

`studio.example.invalid` は配備先の正確な HTTPS origin に置き換えます。ワイルドカード origin は使いません。ブラウザは R2 request に cookie を送りません。`ETag` は multipart completion API に渡すため、response header として公開します。

API が返す part URL の期限は upload session の `expires_at` を越えません。クライアントは URL を保存せず、停止または URL 期限切れの後は同じ `Idempotency-Key` で start API を再実行して署名を更新します。
