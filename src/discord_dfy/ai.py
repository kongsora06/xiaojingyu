"""DeepSeek endpoint and default model shared by every mode."""

BASE_URL = "https://api.deepseek.com"
MODEL = "deepseek-flash"


def error_kind(error: BaseException) -> str:
    """The error's type and what caused it (APIConnectionError(ConnectTimeout)), for logs.

    Only type names: messages may carry request details.
    """
    name = type(error).__name__
    cause = error.__cause__ or error.__context__
    return f"{name}({type(cause).__name__})" if cause is not None else name
