# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""会員 URL (公開ホスト config.urls_public 配下に /members/ で mount)。

公開ホスト限定 (HostUrlconfMiddleware)。デコレータ/テンプレはリテラルパスを使うが、
ビュー間リンク用に name も付ける。
"""

from django.urls import path

from members import views

app_name = "members"

urlpatterns = [
    path("", views.profile, name="profile"),
    path("register/", views.register, name="register"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("edit/", views.profile_edit, name="profile_edit"),
    path("password/", views.password_change, name="password_change"),
    # パスワードリセット (未ログイン)
    path("password/reset/", views.password_reset_request, name="password_reset_request"),
    path("password/reset/confirm/", views.password_reset_confirm, name="password_reset_confirm"),
    # 退会 (アカウント削除)
    path("delete/", views.account_delete, name="account_delete"),
    # メールアドレス変更
    path("email/", views.email_change, name="email_change"),
    path("email/confirm/", views.email_change_confirm, name="email_change_confirm"),
    # マイリスト (あとで見る) #PERS-01
    path("favorites/", views.favorites_list, name="favorites"),
    path("favorites/<int:program_id>/toggle/", views.favorite_toggle, name="favorite_toggle"),
    # ファンクラブ (デジタル会員証) #27
    path("fanclub/", views.fanclub_list, name="fanclub_list"),
    path("fanclub/<slug:creator_slug>/card/", views.fanclub_card, name="fanclub_card"),
    # お気に入りチャンネル (ピン留め) #EPG-04
    path(
        "channels/<slug:slug>/toggle/",
        views.channel_favorite_toggle,
        name="channel_favorite_toggle",
    ),
    # 視聴リマインド (番組開始前メール) #EPG-03
    path("reminders/<int:program_id>/toggle/", views.reminder_toggle, name="reminder_toggle"),
    # 視聴履歴・続きから #PERS-02
    path("history/", views.watch_history, name="watch_history"),
    path("watch/<int:program_id>/progress/", views.watch_progress, name="watch_progress"),
    # チャンネルコメント (投稿=確認済み会員 / 削除=本人)
    path("comments/<slug:slug>/post/", views.comment_post, name="comment_post"),
    path("comments/<int:comment_id>/delete/", views.comment_delete, name="comment_delete"),
    # メール確認 (本人確認の1要素)
    path("verify/email/", views.verify_email, name="verify_email"),
    path("verify/email/send/", views.send_email_code, name="send_email_code"),
    path("verify/email/confirm/", views.confirm_email_code, name="confirm_email_code"),
    # 未認証ゲートの着地ページ
    path("verify-required/", views.verify_required, name="verify_required"),
    # 2FA 設定 (認証アプリ登録 / メール2FA / 解除)
    path("2fa/", views.two_factor_settings, name="two_factor"),
    path("2fa/totp/setup/", views.totp_setup, name="totp_setup"),
    path("2fa/email/enable/", views.email_2fa_enable, name="email_2fa_enable"),
    path("2fa/disable/", views.two_factor_disable, name="two_factor_disable"),
    # ログインの第2要素
    path("login/2fa/totp/", views.login_2fa_totp, name="login_2fa_totp"),
    path("login/2fa/email/", views.login_2fa_email, name="login_2fa_email"),
]
