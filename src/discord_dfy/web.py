"""Web search and page fetching through DeepSeek's Search / Fetch API (same API key)."""

import logging

from openai import APIError, APIStatusError, AsyncOpenAI

log = logging.getLogger(__name__)
SUMMARY_CHARS = 700
PAGE_CHARS = 30000


class WebError(Exception):
    """A search or fetch that failed, worded for the model."""


class Web:
    def __init__(self, client: AsyncOpenAI):
        self.client = client.with_options(max_retries=1, timeout=60.0)

    async def search(self, query: str, count: int = 5) -> list[dict]:
        query = " ".join(str(query or "").split())[:128]
        if not query:
            raise WebError("搜索词是空的")
        count = min(10, max(1, int(count or 5)))
        data = await self._post("/v1/tools/search", {"query": query, "count": count})
        results = data.get("results") if isinstance(data, dict) else None
        return [item for item in results or [] if isinstance(item, dict)]

    async def fetch(self, url: str) -> dict:
        url = str(url or "").strip()
        if not url.startswith(("http://", "https://")):
            raise WebError("只能打开 http(s) 网址")
        data = await self._post("/v1/tools/fetch", {"url": url[:8192]})
        if not isinstance(data, dict) or not data.get("content"):
            raise WebError("这个网页没有取到内容")
        return data

    async def _post(self, path: str, body: dict):
        try:
            return await self.client.post(path, body=body, cast_to=object)
        except APIStatusError as error:
            log.warning("Web request failed; path=%s; status=%s", path, error.status_code)
            reason = {
                402: "DeepSeek 余额不足",
                403: "这个网页不允许抓取",
                404: "这个网页打不开或者没有内容",
                429: "请求太频繁了，稍后再试",
            }.get(error.status_code, f"HTTP {error.status_code}")
            raise WebError(reason) from None
        except APIError as error:
            log.warning("Web request failed; path=%s; error_type=%s", path, type(error).__name__)
            raise WebError(f"连不上（{type(error).__name__}）") from None


def format_results(results: list[dict], summary_chars: int = SUMMARY_CHARS) -> str:
    if not results:
        return "没有搜到结果"
    blocks = []
    for index, item in enumerate(results, 1):
        date = (item.get("date") or "")[:10]
        site = item.get("site_name") or ""
        head = f"[{index}] {item.get('title') or '（无标题）'}"
        meta = " · ".join(part for part in (site, date) if part)
        text = (item.get("summary") or item.get("snippet") or "").strip()
        if len(text) > summary_chars:
            text = text[:summary_chars] + "…"
        blocks.append(
            f"{head}\n{item.get('url', '')}" + (f"\n{meta}" if meta else "") + f"\n{text}"
        )
    return "\n\n".join(blocks)


def format_page(page: dict, url: str) -> str:
    content = str(page.get("content") or "")
    if len(content) > PAGE_CHARS:
        content = content[:PAGE_CHARS] + f"\n…（页面太长，只保留前 {PAGE_CHARS} 字）"
    return f"# {page.get('title') or url}\n{url}\n\n{content}"
