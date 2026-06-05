from __future__ import annotations

import argparse
import os
import webbrowser

import uvicorn


def main() -> int:
    parser = argparse.ArgumentParser(description="Run SmartInventory web app")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8766")))
    parser.add_argument("--open", action="store_true", help="Open the web app in a browser")
    parser.add_argument("--config", default=os.environ.get("SMARTINVENTORY_CONFIG", ""))
    args = parser.parse_args()

    if args.config:
        os.environ["SMARTINVENTORY_CONFIG"] = args.config

    url = f"http://{args.host}:{args.port}"
    if args.open:
        webbrowser.open(url)

    uvicorn.run(
        "inventory_web.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
