from __future__ import annotations

import os
from pathlib import Path

import httpx

ENV_FILE = Path(os.path.expanduser(os.environ.get("LLM_HARNESS_ENV", "~/work/.env")))
BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
GOOGLE_ENDPOINT = "https://www.googleapis.com/customsearch/v1"
MAX_COUNT = 10
TIMEOUT = 15


def load_env() -> dict:
    if not ENV_FILE.is_file():
        return {}
    values: dict[str, str] = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _normalize(results: list[dict], count: int, engine: str) -> list[dict] | None:
    seen: set[str] = set()
    out: list[dict] = []
    for r in results:
        url = r.get("url", "")
        if not url or url in seen:
            continue
        seen.add(url)
        out.append({
            "title": r.get("title") or url,
            "url": url,
            "snippet": (r.get("description") or "").strip(),
            "engine": engine,
        })
        if len(out) == count:
            break
    return out or None


def _brave(key: str, query: str, count: int) -> tuple[list[dict] | None, str]:
    try:
        resp = httpx.get(
            BRAVE_ENDPOINT, params={"q": query, "count": count},
            headers={"Accept": "application/json", "X-Subscription-Token": key},
            timeout=TIMEOUT)
    except httpx.HTTPError as e:
        return None, f"brave: network error {type(e).__name__}"
    if resp.status_code == 429:
        return None, "brave: monthly quota exhausted (HTTP 429)"
    if resp.status_code != 200:
        return None, f"brave: HTTP {resp.status_code}"
    return _normalize(resp.json().get("web", {}).get("results", []), count, "brave"), ""


def _google(env: dict, query: str, count: int) -> tuple[list[dict] | None, str]:
    key = env.get("GOOGLE_SEARCH_JSON_API_KEY", "")
    cx = env.get("GOOGLE_CSE_ENGINE_ID", "")
    if not key or not cx:
        missing = [n for n, v in
                   (("GOOGLE_SEARCH_JSON_API_KEY", key),
                    ("GOOGLE_CSE_ENGINE_ID", cx)) if not v]
        return None, f"google: missing {', '.join(missing)} in {ENV_FILE}"
    try:
        resp = httpx.get(
            GOOGLE_ENDPOINT,
            params={"key": key, "cx": cx, "q": query, "num": count},
            timeout=TIMEOUT)
    except httpx.HTTPError as e:
        return None, f"google: network error {type(e).__name__}"
    if resp.status_code == 429:
        return None, "google: daily quota exhausted (HTTP 429)"
    if resp.status_code != 200:
        try:
            msg = resp.json()["error"]["message"]
        except Exception:
            msg = f"HTTP {resp.status_code}"
        return None, f"google: {msg}"
    return _normalize(resp.json().get("items", []), count, "google"), ""


def _format(results: list[dict]) -> str:
    lines = [f"Web search returned {len(results)} results:"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']} [{r['engine']}]\n   {r['url']}")
        if r["snippet"]:
            lines.append(f"   {r['snippet']}")
    return "\n".join(lines)


def search(query: str, count: int = 8) -> str:
    query = (query or "").strip()
    count = min(max(int(count or 8), 1), MAX_COUNT)
    if not query:
        return "error: query is empty"
    env = load_env()
    engines: list[tuple[str, object]] = []
    brave_key = env.get("BRAVE_SEARCH_JSON_API_KEY", "")
    if brave_key:
        engines.append(("brave", lambda: _brave(brave_key, query, count)))
    engines.append(("google", lambda: _google(env, query, count)))
    failures: list[str] = []
    for name, call in engines:
        results, err = call()
        if results:
            return _format(results)
        failures.append(err)
    return "error: " + " | ".join(failures)


def tool_web_search(root: str, args: dict) -> str:
    return search(args.get("query", ""), int(args.get("count") or 8))
