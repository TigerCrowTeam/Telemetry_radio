from __future__ import annotations

import argparse
import logging
from dataclasses import replace

from .app import run
from .config import load_config


def main() -> None:
    parser = argparse.ArgumentParser(description="MAVLink SoapySDR telemetry radio")
    parser.add_argument("--config", required=True, help="path to YAML configuration")
    parser.add_argument(
        "--status",
        choices=("off", "text", "json"),
        help="override periodic status output format",
    )
    parser.add_argument("--status-interval", type=float, help="override status interval in seconds")
    parser.add_argument("--status-http-port", type=int, help="override local status HTTP port")
    parser.add_argument(
        "--no-status-http", action="store_true", help="disable the status HTTP server"
    )
    parser.add_argument(
        "--allow-unauthenticated",
        action="store_true",
        help="allow insecure operation without authentication (isolated bench use only)",
    )
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        if config.authentication.private_key_file is None and not args.allow_unauthenticated:
            raise ValueError(
                "authentication.private_key_file is required; use --allow-unauthenticated only on an isolated bench"
            )
        status = config.status
        if args.status is not None:
            status = replace(status, output=args.status)
        if args.status_interval is not None:
            if args.status_interval <= 0:
                raise ValueError("--status-interval must be positive")
            status = replace(status, interval_seconds=args.status_interval)
        if args.status_http_port is not None:
            if not 1 <= args.status_http_port <= 65535:
                raise ValueError("--status-http-port must be between 1 and 65535")
            status = replace(status, http_port=args.status_http_port)
        if args.no_status_http:
            status = replace(status, http_enabled=False)
        config = replace(config, status=status)
        logging.basicConfig(
            level=getattr(logging, config.log_level.upper(), logging.INFO),
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        run(config)
    except (OSError, RuntimeError, ValueError) as exc:
        parser.exit(1, f"telemetry-radio: {exc}\n")


if __name__ == "__main__":
    main()
