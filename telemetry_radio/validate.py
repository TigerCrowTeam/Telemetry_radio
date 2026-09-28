from __future__ import annotations

import argparse
from pathlib import Path

from .config import load_config, validate_network
from .sdr import make_soapy_device, validate_device_capabilities


def validate_hardware(config) -> str:
    try:
        import SoapySDR
        from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_TX
    except ImportError as exc:
        raise RuntimeError("SoapySDR Python bindings are required for hardware validation") from exc
    device = make_soapy_device(SoapySDR, config.sdr.device)
    validate_device_capabilities(
        device,
        config.sdr,
        config.frequency.frequencies_hz,
        SOAPY_SDR_RX,
        SOAPY_SDR_TX,
    )
    return str(device.getHardwareInfo())


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate telemetry-radio configuration")
    parser.add_argument("configs", nargs="+", help="one config, or every network config")
    parser.add_argument(
        "--hardware",
        action="store_true",
        help="also check one config against its auto-detected/configured SDR",
    )
    args = parser.parse_args()
    try:
        loaded = {str(Path(path)): load_config(path) for path in args.configs}
        if len(loaded) > 1:
            validate_network(loaded)
        if args.hardware:
            if len(loaded) != 1:
                raise ValueError("--hardware accepts exactly one node configuration")
            details = validate_hardware(next(iter(loaded.values())))
            print(f"hardware valid: {details}")
        for path, config in loaded.items():
            print(f"valid: {path} ({config.role} node {config.node_id})")
        if len(loaded) > 1:
            print(f"network valid: {len(loaded)} nodes")
    except (OSError, RuntimeError, ValueError) as exc:
        parser.exit(1, f"telemetry-radio-validate: {exc}\n")


if __name__ == "__main__":
    main()
