from __future__ import annotations
import logging
import os


def _probe_langsmith_key(api_key: str, endpoint: str, timeout: float = 3.0) -> bool:
    """Best-effort probe that the LangSmith key is currently accepted.

    A 200 from `/info` means the key is good. Anything else (auth error,
    network timeout, DNS failure) → tracing is disabled rather than letting
    the background uploader thread retry forever. Returns False on any failure.
    """
    try:
        import requests  # local import — keep cold-start cost off the import path
    except ImportError:
        return False
    url = endpoint.rstrip("/") + "/info"
    try:
        resp = requests.get(
            url,
            headers={"x-api-key": api_key, "User-Agent": "msba-tracing-probe/1.0"},
            timeout=timeout,
        )
        return 200 <= resp.status_code < 300
    except Exception:
        return False


def init_langsmith_tracing() -> None:
    """Enable LangSmith tracing only when the key actually works.

    Without this probe, an invalid key still spawns the background uploader
    threads which spam 403s and (on this Windows env) have been observed to
    interact badly with chromadb's rust client, producing access violations.
    """
    os.environ.setdefault("LANGCHAIN_TRACING_V2", "false")
    os.environ.setdefault("LANGCHAIN_PROJECT", "MSBA_AI_Agents_Demo")

    if os.environ.get("LANGCHAIN_TRACING_V2", "false").lower() != "true":
        return

    api_key = os.environ.get("LANGCHAIN_API_KEY", "").strip()
    if not api_key:
        print("[Tracing] LANGCHAIN_API_KEY not set — tracing disabled.")
        os.environ["LANGCHAIN_TRACING_V2"] = "false"
        return

    endpoint = os.environ.get("LANGCHAIN_ENDPOINT", "https://api.smith.langchain.com").strip()
    if not _probe_langsmith_key(api_key, endpoint):
        print(f"[Tracing] LangSmith key did not authenticate against {endpoint} — tracing disabled.")
        os.environ["LANGCHAIN_TRACING_V2"] = "false"
        return

    # Suppress per-request retry noise even on the happy path.
    logging.getLogger("langsmith").setLevel(logging.ERROR)
    print("[Tracing] LangSmith tracing enabled.")
