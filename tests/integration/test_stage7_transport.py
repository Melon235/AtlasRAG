"""Real local HTTP transport checks for SearXNG JSON and DeepSeek contracts."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from pydantic import SecretStr
from stage7.helpers import answer_input, generation

from atlasrag.providers.generation.deepseek import DeepSeekGenerationProvider
from atlasrag.providers.web.searxng import SearxngSearchProvider
from atlasrag.services.answer.generation_input import GenerationInputBuilder
from atlasrag.services.answer.profile import AnswerProfile

pytestmark = pytest.mark.integration


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        assert self.path.startswith("/search?")
        body = json.dumps(
            {
                "results": [
                    {
                        "title": "Protocol result",
                        "url": "https://example.com/result",
                        "content": "An external snippet about protocols.",
                    }
                ]
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        assert self.path == "/chat/completions"
        length = int(self.headers["Content-Length"])
        payload = json.loads(self.rfile.read(length))
        assert payload["messages"][0]["role"] == "system"
        text = json.dumps(
            {"answer_text": "Grounded [L1].", "used_citation_ids": ["L1"]}
        )
        body = json.dumps({"choices": [{"message": {"content": text}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture()
def local_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.asyncio
async def test_real_searxng_json_transport_and_deepseek_structured_transport(
    local_server: str,
) -> None:
    async with httpx.AsyncClient(trust_env=False) as client:
        results = await SearxngSearchProvider(
            base_url=f"{local_server}/", client=client
        ).search("protocol")
        assert results and results[0].domain == "example.com"

        provider = DeepSeekGenerationProvider(
            model_id="deepseek-chat",
            api_key=SecretStr("local-test-secret"),
            client=client,
            endpoint=f"{local_server}/chat/completions",
        )
        input = GenerationInputBuilder(profile=AnswerProfile()).build(answer_input())
        result = await provider.generate(input)
        assert result == generation("Grounded [L1].", ("L1",))
