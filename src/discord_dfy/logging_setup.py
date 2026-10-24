"""Safe console logging, including third-party debug and exception suppression."""

import logging
import re


class SafeFormatter(logging.Formatter):
    def __init__(self, secrets: tuple[str, ...]) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")
        self._secrets = tuple(secret for secret in secrets if secret)

    def formatException(self, exc_info: object) -> str:
        # SDK/Discord exception bodies may include prompts or credentials.
        return "[exception details omitted]"

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret in self._secrets:
            text = text.replace(secret, "[REDACTED]")
        return re.sub(r"(?i)(authorization\s*[:=]\s*)(?:bot|bearer)\s+\S+", r"\1[REDACTED]", text)


def configure_logging(*, level: str = "INFO", secrets: tuple[str, ...] = ()) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(SafeFormatter(secrets))
    logging.basicConfig(level=level, handlers=[handler], force=True)
    # Provider request-body debug logs are not appropriate for a chat application.
    # Override OPENAI_LOG and inherited settings even when the app uses LOG_LEVEL=DEBUG.
    for name in ("openai", "httpx", "httpx2", "httpcore", "httpcore2", "discord", "aiosqlite"):
        logger = logging.getLogger(name)
        logger.setLevel(logging.CRITICAL)
        logger.handlers.clear()
        logger.propagate = True
