"""Launch the dashboard: python -m flightdeals.dashboard [--host] [--port]"""

from __future__ import annotations

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Flight-deals dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    uvicorn.run("flightdeals.dashboard.app:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
