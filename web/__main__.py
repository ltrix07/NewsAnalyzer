"""Run the redirect ASGI service with uvicorn."""

from __future__ import annotations

import argparse

import uvicorn

from engine.config import get_settings
from engine.observability import configure_logging


def main() -> None:
    """Parse bind options and start the ASGI server."""

    parser = argparse.ArgumentParser(description="Run the newsAnalyzer web service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    configure_logging(get_settings())
    uvicorn.run("web.app:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
