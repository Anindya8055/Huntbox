"""Stage 1 -- Product Hunt GraphQL v2 client.

Paginates `posts(order: VOTES, ...)` with `first`/`after` cursors until the
requested limit is met or the API runs out of results.

Note: the v2 API redacts maker names/handles, so nothing here tries to read
maker identity -- product/post data only.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.models import Product
from app.timeframes import DateRange

log = logging.getLogger("huntbox.producthunt")

API_URL = "https://api.producthunt.com/v2/api/graphql"
PAGE_SIZE = 50

POSTS_QUERY = """
query TopPosts($after: String, $first: Int!, $postedAfter: DateTime!, $postedBefore: DateTime!) {
  posts(
    order: VOTES
    first: $first
    after: $after
    postedAfter: $postedAfter
    postedBefore: $postedBefore
  ) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        name
        tagline
        description
        votesCount
        commentsCount
        createdAt
        url
        website
        topics(first: 6) { edges { node { name } } }
      }
    }
  }
}
"""


class ProductHuntError(RuntimeError):
    """A user-presentable failure talking to Product Hunt."""


def _node_to_product(node: dict[str, Any], rank: int) -> Product:
    topics = [
        edge["node"]["name"]
        for edge in (node.get("topics") or {}).get("edges", [])
        if (edge.get("node") or {}).get("name")
    ]
    return Product(
        rank=rank,
        product_name=node.get("name") or "Untitled",
        tagline=node.get("tagline") or "",
        description=node.get("description") or "",
        votes=node.get("votesCount") or 0,
        comments=node.get("commentsCount") or 0,
        producthunt_url=node.get("url") or "",
        website_url=node.get("website") or "",
        topics=topics,
        launch_date=(node.get("createdAt") or "")[:10],
    )


class ProductHuntClient:
    """Stateless by design: no response cache, no reused connection pool.

    Every top_posts() call opens a fresh AsyncClient and hits the API, so a
    "Hunt" can never serve stale data. Do not add caching here -- the whole
    point of the tool is that ranks and vote counts are current.
    """

    def __init__(self, token: str | None, timeout: float = 30.0) -> None:
        self._token = token
        self._timeout = timeout

    def available(self) -> tuple[bool, str]:
        if not self._token:
            return False, (
                "PRODUCTHUNT_API_TOKEN is missing. Add it to your .env file and restart the server."
            )
        return True, ""

    async def top_posts(self, rng: DateRange, limit: int) -> list[Product]:
        ok, reason = self.available()
        if not ok:
            raise ProductHuntError(reason)

        posted_after, posted_before = rng.to_api_bounds()
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        collected: list[dict[str, Any]] = []
        cursor: str | None = None

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            while len(collected) < limit:
                variables = {
                    "first": min(PAGE_SIZE, limit - len(collected)),
                    "after": cursor,
                    "postedAfter": posted_after,
                    "postedBefore": posted_before,
                }
                payload = await self._post(client, headers, variables)
                posts = payload.get("posts") or {}
                edges = posts.get("edges") or []
                if not edges:
                    break

                collected.extend(edge["node"] for edge in edges if edge.get("node"))

                page_info = posts.get("pageInfo") or {}
                if not page_info.get("hasNextPage"):
                    break
                cursor = page_info.get("endCursor")
                if not cursor:
                    break

        log.info(
            "Fetched %d posts for %s..%s (limit %d)",
            len(collected), rng.start, rng.end, limit,
        )
        return [_node_to_product(node, i) for i, node in enumerate(collected[:limit], start=1)]

    async def _post(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        variables: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            resp = await client.post(
                API_URL,
                headers=headers,
                json={"query": POSTS_QUERY, "variables": variables},
            )
        except httpx.TimeoutException as exc:
            raise ProductHuntError("Product Hunt took too long to respond. Try again.") from exc
        except httpx.HTTPError as exc:
            raise ProductHuntError(f"Could not reach Product Hunt: {exc}") from exc

        if resp.status_code in (401, 403):
            raise ProductHuntError(
                "Product Hunt rejected the token (HTTP %d). Check PRODUCTHUNT_API_TOKEN."
                % resp.status_code
            )
        if resp.status_code == 429:
            retry = resp.headers.get("Retry-After", "a moment")
            raise ProductHuntError(
                f"Product Hunt rate limit reached. Wait {retry} and try a smaller limit."
            )
        if resp.status_code >= 500:
            raise ProductHuntError(
                f"Product Hunt is having trouble (HTTP {resp.status_code}). Try again shortly."
            )
        if resp.status_code != 200:
            raise ProductHuntError(f"Unexpected Product Hunt response (HTTP {resp.status_code}).")

        try:
            body = resp.json()
        except ValueError as exc:
            raise ProductHuntError("Product Hunt returned a malformed response.") from exc

        if body.get("errors"):
            messages = "; ".join(
                str(e.get("message", "unknown error")) for e in body["errors"][:3]
            )
            log.warning("GraphQL errors: %s", messages)
            raise ProductHuntError(f"Product Hunt query failed: {messages}")

        data = body.get("data")
        if not data:
            raise ProductHuntError("Product Hunt returned no data for this query.")
        return data
