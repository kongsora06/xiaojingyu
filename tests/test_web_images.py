import json

import httpx2
import pytest
from openai import AsyncOpenAI

from discord_dfy.ai import BASE_URL
from discord_dfy.images import data_url, image_type, load_block, read_images, text_of
from discord_dfy.web import Web, WebError, format_page, format_results

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20


def test_image_type_is_judged_by_content():
    assert image_type(PNG) == "png"
    assert image_type(b"\xff\xd8\xff\xe0rest") == "jpeg"
    assert image_type(b"GIF89a...") == "gif"
    assert image_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "webp"
    assert image_type(b"<svg>") is None and data_url(b"<svg>") is None
    assert data_url(PNG).startswith("data:image/png;base64,")


def test_stored_image_blocks_load_or_degrade_to_text(tmp_path):
    path = tmp_path / "a.png"
    path.write_bytes(PNG)
    assert load_block({"type": "image_file", "path": str(path)})["type"] == "image_url"
    missing = load_block({"type": "image_file", "path": str(tmp_path / "gone.png")})
    assert missing["type"] == "text" and "gone.png" in missing["text"]
    content = [{"type": "text", "text": "看"}, {"type": "image_file", "path": str(path)}]
    assert text_of(content) == "看\n[图片 a.png]" and text_of("纯文字") == "纯文字"


class Attachment:
    def __init__(self, name, data, content_type="image/png"):
        self.filename, self.data, self.content_type, self.size = (
            name,
            data,
            content_type,
            len(data),
        )

    async def read(self):
        return self.data


async def test_read_images_skips_non_images_and_fake_images():
    message = type("M", (), {})()
    message.attachments = [
        Attachment("a.png", PNG),
        Attachment("notes.txt", b"hi", "text/plain"),
        Attachment("fake.png", b"not really"),
    ]
    assert await read_images(message) == [("a.png", PNG)]


def web_client(handler):
    return AsyncOpenAI(
        api_key="fake-key",
        base_url=BASE_URL,
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )


async def test_web_search_and_fetch_use_deepseek_tools_api():
    requests = []

    async def handler(request):
        requests.append((request.url.path, json.loads(request.content)))
        if request.url.path.endswith("/search"):
            return httpx2.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "天气",
                            "url": "https://tq.example/1",
                            "site_name": "墨迹",
                            "date": "2026-10-22T16:00:00+00:00",
                            "summary": "晴 " * 500,
                        }
                    ]
                },
            )
        if "missing" in json.loads(request.content)["url"]:
            return httpx2.Response(404, json={"error": {"message": "nope"}})
        return httpx2.Response(200, json={"title": "页", "content": "正文"})

    async with web_client(handler) as client:
        web = Web(client)
        results = await web.search("  上海   天气 ", count=50)
        text = format_results(results, 20)
        assert text.startswith("[1] 天气\nhttps://tq.example/1\n墨迹 · 2026-10-22\n晴")
        assert text.endswith("…")
        assert format_page(await web.fetch("https://a.example"), "https://a.example").startswith(
            "# 页\nhttps://a.example\n\n正文"
        )
        with pytest.raises(WebError, match="打不开"):
            await web.fetch("https://missing.example")
        with pytest.raises(WebError):
            await web.fetch("file:///etc/passwd")
        with pytest.raises(WebError):
            await web.search("   ")
    assert requests[0] == ("/v1/tools/search", {"query": "上海 天气", "count": 10})
    assert format_results([]) == "没有搜到结果"
