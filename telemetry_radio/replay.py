from __future__ import annotations

import argparse
import json

from .authentication import load_public_keys
from .recording import describe_frames, load_manifest, replay_frames, replay_iq


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay a telemetry-radio capture")
    parser.add_argument("capture", help="capture directory containing manifest.json")
    parser.add_argument(
        "--source",
        choices=("iq", "frames"),
        default="iq",
        help="rerun demodulation from IQ or framing from decoded frames",
    )
    parser.add_argument(
        "--public-key",
        action="append",
        default=[],
        metavar="NODE_ID=PATH",
        help="trusted node public key; repeat for each captured source",
    )
    args = parser.parse_args()
    try:
        manifest = load_manifest(args.capture)
        paths = {}
        for value in args.public_key:
            node, separator, path = value.partition("=")
            if not separator:
                raise ValueError("--public-key must use NODE_ID=PATH")
            paths[int(node)] = path
        if manifest.get("authenticated") and not paths:
            raise ValueError("authenticated capture requires --public-key")
        keys = load_public_keys(paths) if paths else None
        wires = replay_iq(args.capture) if args.source == "iq" else replay_frames(args.capture)
        for result in describe_frames(wires, keys):
            print(json.dumps(result, separators=(",", ":")))
    except ValueError as exc:
        parser.exit(1, f"telemetry-radio-replay: {exc}\n")


if __name__ == "__main__":
    main()
