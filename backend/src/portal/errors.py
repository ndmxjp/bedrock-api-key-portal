"""API error type and the user-facing (Japanese) messages for each error code."""

MESSAGES: dict[str, str] = {
    "UNAUTHENTICATED": "認証が必要です。再度ログインしてください。",
    "INVALID_TOKEN": "認証情報が無効です。再度ログインしてください。",
    "EMAIL_NOT_VERIFIED": "メールアドレスの確認が完了していません。",
    "DOMAIN_NOT_ALLOWED": "このメールドメインは利用対象外です。",
    "ISSUANCE_DISABLED": "現在、APIキーの発行を停止しています。",
    "REGISTRATION_CAP_REACHED": "PoCの登録人数の上限に達しています。",
    "ALREADY_ISSUED": "有効なAPIキーが既に発行されています。キー全文は再表示できません。",
    "ISSUING": "APIキーを発行処理中です。しばらく待ってから画面を更新してください。",
    "RATE_LIMITED": "操作が短時間に繰り返されました。少し待ってから再試行してください。",
    "KEY_NOT_FOUND": "失効できるAPIキーがありません。",
    "INVALID_REQUEST": "リクエストの形式が正しくありません。",
    "NOT_FOUND": "指定されたAPIは存在しません。",
    "FORBIDDEN": "この操作を行う権限がありません。",
    "USER_NOT_FOUND": "指定された利用者は見つかりません。",
    "SLOT_IN_USE": (
        "有効なキーまたは発行処理中のキーがあるため、枠を解放できません。先にキーを失効してください。"
    ),
    "NO_SLOT": "この利用者は登録枠を使用していません。",
    "USER_SUSPENDED": "管理者によりAPIキーの利用が一時停止されています。管理者に連絡してください。",
    "NOT_SUSPENDED": "この利用者は一時停止されていません。",
    "INVALID_CAP": "登録枠の値が正しくありません。",
    "INVALID_AGE": "有効期限の値が正しくないか、許可された上限を超えています。",
    "AWS_API_ERROR": "AWS側の処理に失敗しました。時間をおいて再試行してください。",
    "INTERNAL_ERROR": "内部エラーが発生しました。管理者に連絡してください。",
}


class ApiError(Exception):
    """An error that maps directly to an HTTP response. Never put secrets in it."""

    def __init__(self, status: int, code: str, message: str | None = None) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message or MESSAGES.get(code, MESSAGES["INTERNAL_ERROR"])

    def to_body(self) -> dict[str, dict[str, str]]:
        return {"error": {"code": self.code, "message": self.message}}
