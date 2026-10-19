#!/usr/bin/env python3
"""Server entry point for the Nova agent.

Creates the agent first, then starts the FastAPI server with uvicorn.

Usage:
    python -m novacode_cli.server_main
    python -m novacode_cli.server_main --port 8080 --host 0.0.0.0
"""

from __future__ import annotations

import argparse
import logging
import sys
import time


def main() -> None:
    parser = argparse.ArgumentParser(description="Nova Agent Server")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address")
    parser.add_argument("--port", type=int, default=8000, help="Bind port")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload")
    parser.add_argument("--log-level", default="info", help="Logging level")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger(__name__)

    # ── Create agent before starting server ──────────────────────────────
    logger.info("Creating agent (this may take ~30s)...")
    start = time.time()
    from novacode_cli.server.app import _create_server_agent, set_agent

    agent, backend, config = _create_server_agent()
    set_agent(agent, backend, config)
    elapsed = time.time() - start
    logger.info("Agent created in %.1fs. Starting server...", elapsed)

    # ── Start server ─────────────────────────────────────────────────────
    try:
        import uvicorn
    except ImportError:
        from novacode_cli.install_commands import install_command

        command = install_command(["fastapi", "uvicorn"], "server")
        print(f"uvicorn is required. Install with: {command}")
        sys.exit(1)

    uvicorn.run(
        "novacode_cli.server.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
        http="h11",  # Use h11 instead of httptools (Windows compat)
    )


if __name__ == "__main__":
    main()
