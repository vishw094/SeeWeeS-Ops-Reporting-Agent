from __future__ import annotations
import os
import logging

def init_langsmith_tracing() -> None:
    os.environ.setdefault("LANGCHAIN_TRACING_V2", "false")
    os.environ.setdefault("LANGCHAIN_PROJECT", "MSBA_AI_Agents_Demo")

    if os.environ.get("LANGCHAIN_TRACING_V2", "false").lower() != "true":
        return

    if not os.environ.get("LANGCHAIN_API_KEY"):
        print("[Tracing] LANGCHAIN_API_KEY not set — tracing disabled.")
        os.environ["LANGCHAIN_TRACING_V2"] = "false"
        return

    # Suppress the per-request 403 spam; a single warning is enough
    logging.getLogger("langsmith").setLevel(logging.ERROR)
    print("[Tracing] LangSmith tracing enabled.")
