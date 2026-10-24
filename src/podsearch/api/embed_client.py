"""Async client for the embedder service. The policy from docs/DECISIONS.md lives here, in one place:

  * the connection pool is the bulkhead: at most 8 requests in flight, and a full pool fails within 0.1 s,
    so a slow embedder cannot use up every API worker (the UI and /audio keep working)
  * connect 0.5 s, read 2 s; ONE retry on connect errors only, never on timeouts or 503s (a retry would
    only add load to an embedder that is already struggling)
  * the answer must match the contract: right model and the collection's vector size
"""
import httpx
import numpy as np


class EmbedUnavailable(Exception):
    """The embedder cannot answer right now. /search turns this into 503 + Retry-After."""

    def __init__(self, kind: str, retry_after: int = 5):
        super().__init__(kind)
        self.kind, self.retry_after = kind, retry_after


class EmbedContractError(Exception):
    """The embedder runs a different model (or vector size) than the collection was built with."""


class EmbedClient:
    def __init__(self, base_url: str, model: str, dim: int, transport: "httpx.AsyncBaseTransport | None" = None):
        self.model, self.dim = model, dim
        self.http = httpx.AsyncClient(
            base_url=base_url, transport=transport,
            limits=httpx.Limits(max_connections=8),
            timeout=httpx.Timeout(connect=0.5, read=2.0, write=1.0, pool=0.1))

    async def embed(self, text: str) -> np.ndarray:
        r = await self._post(text)
        if r.status_code == 503:
            # The embedder says when to come back: 1 s when overloaded, 30 s while its model is loading.
            raise EmbedUnavailable("overloaded" if r.headers.get("Retry-After") == "1" else "loading",
                                   int(r.headers.get("Retry-After", 5)))
        if r.status_code != 200:
            raise EmbedUnavailable(f"http_{r.status_code}")
        body = r.json()
        if body.get("model") != self.model or body.get("dim") != self.dim:
            raise EmbedContractError(f"embedder returned model={body.get('model')} dim={body.get('dim')}, "
                                     f"expected model={self.model} dim={self.dim}")
        return np.asarray(body["vector"], dtype=np.float32)

    async def _post(self, text: str) -> httpx.Response:
        for attempt in (1, 2):
            try:
                return await self.http.post("/embed", json={"text": text})
            except httpx.ConnectError:
                if attempt == 2:
                    raise EmbedUnavailable("connect", 30) from None   # no endpoints: embedder is down or loading
            except httpx.PoolTimeout:
                raise EmbedUnavailable("pool_full", 1) from None
            except httpx.TimeoutException:
                raise EmbedUnavailable("timeout", 1) from None
            except httpx.HTTPError:
                raise EmbedUnavailable("transport", 5) from None
        raise AssertionError("unreachable")

    async def close(self) -> None:
        await self.http.aclose()
