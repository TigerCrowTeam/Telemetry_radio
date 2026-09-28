#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path


def status(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=2) as response:
        return json.loads(response.read())


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the two-node conducted-link acceptance")
    parser.add_argument("--pi", default="master@192.168.2.48")
    parser.add_argument("--remote-dir", default="/home/master/telemetry-radio")
    parser.add_argument("--acquisition-seconds", type=float, default=15)
    parser.add_argument("--acquisition-timeout-seconds", type=float, default=45)
    parser.add_argument("--measurement-seconds", type=float, default=180)
    parser.add_argument("--status-url", default="http://127.0.0.1:8765/status")
    parser.add_argument("--output", default="acceptance-result.json")
    parser.add_argument(
        "--expected-frequencies-hz",
        default="905000000,915000000,925000000",
        help="comma-separated frequencies which must all be observed",
    )
    parser.add_argument("--max-heartbeat-age-ms", type=float, default=2500.0)
    parser.add_argument("--max-heartbeat-p95-ms", type=float, default=2100.0)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    socket_path = Path(tempfile.gettempdir()) / f"telemetry-acceptance-ssh-{os.getpid()}"
    ssh = ["ssh", "-S", str(socket_path), args.pi]
    local = None
    try:
        subprocess.run(
            ["ssh", "-M", "-S", str(socket_path), "-o", "ControlPersist=300", "-fN", args.pi],
            check=True,
        )
        remote_command = (
            f"cd {args.remote_dir} && "
            "pkill -f '^[^ ]*python[^ ]* -m telemetry_radio' || true; "
            "nohup .venv/bin/python -m telemetry_radio "
            "--config configs/demo-master-qpsk-b205.yaml --status json --status-interval 5 "
            "> /tmp/telemetry-acceptance-master.log 2>&1 &"
        )
        subprocess.run([*ssh, remote_command], check=True)
        local_log = (root / "acceptance-workstation.log").open("w", encoding="utf-8")
        local = subprocess.Popen(
            [
                str(root / ".venv/bin/python"),
                "-m",
                "telemetry_radio",
                "--config",
                "configs/demo-gcs-qpsk-b205.yaml",
                "--status",
                "json",
                "--status-interval",
                "5",
            ],
            cwd=root,
            stdout=local_log,
            stderr=subprocess.STDOUT,
        )
        time.sleep(args.acquisition_seconds)
        acquisition_deadline = time.monotonic() + args.acquisition_timeout_seconds
        while True:
            baseline = status(args.status_url)
            if (
                baseline.get("synchronized")
                and baseline.get("flight_controller_heartbeat_age_ms") is not None
                and not baseline.get("flight_controller_heartbeat_stale", True)
            ):
                break
            if time.monotonic() >= acquisition_deadline:
                break
            time.sleep(0.25)
        expected_frequencies = {
            float(value) for value in args.expected_frequencies_hz.split(",") if value.strip()
        }
        observed_frequencies: set[float] = set()
        synchronized_throughout = True
        heartbeat_fresh_throughout = True
        maximum_heartbeat_age_ms = 0.0
        deadline = time.monotonic() + args.measurement_seconds
        final = baseline
        while time.monotonic() < deadline:
            final = status(args.status_url)
            active_frequency = final.get("active_frequency_hz")
            if active_frequency is not None:
                observed_frequencies.add(float(active_frequency))
            synchronized_throughout &= bool(final.get("synchronized"))
            heartbeat_age = final.get("flight_controller_heartbeat_age_ms")
            if heartbeat_age is None:
                heartbeat_fresh_throughout = False
            else:
                maximum_heartbeat_age_ms = max(
                    maximum_heartbeat_age_ms, float(heartbeat_age)
                )
                heartbeat_fresh_throughout &= (
                    float(heartbeat_age) <= args.max_heartbeat_age_ms
                )
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
        final = status(args.status_url)
        active_frequency = final.get("active_frequency_hz")
        if active_frequency is not None:
            observed_frequencies.add(float(active_frequency))
        attitude = final.get("mavlink_by_type", {}).get("ATTITUDE", {})
        heartbeat = final.get("mavlink_by_type", {}).get("HEARTBEAT[1:1]", {})
        checks = {
            "synchronized": synchronized_throughout and bool(final.get("synchronized")),
            "flight_controller_heartbeat_fresh": heartbeat_fresh_throughout and not bool(
                final.get("flight_controller_heartbeat_stale", True)
            ),
            "all_frequencies_observed": expected_frequencies <= observed_frequencies,
            "attitude_p95_ms": attitude.get("interval_p95_ms", float("inf")) <= 220,
            "heartbeat_p95_ms": heartbeat.get("interval_p95_ms", float("inf"))
            <= args.max_heartbeat_p95_ms,
            "decoder_output_drops": final.get("decoder_output_drops", 0) == 0,
            "crc_failures": final.get("crc_failures", 0) == baseline.get("crc_failures", 0),
            "timed_transmit_late": final.get("timed_transmit_late", 0)
            == baseline.get("timed_transmit_late", 0),
            "transmit_errors": final.get("transmit_errors", 0)
            == baseline.get("transmit_errors", 0),
        }
        result = {
            "passed": all(checks.values()),
            "checks": checks,
            "expected_frequencies_hz": sorted(expected_frequencies),
            "observed_frequencies_hz": sorted(observed_frequencies),
            "maximum_flight_controller_heartbeat_age_ms": maximum_heartbeat_age_ms,
            "baseline": baseline,
            "final": final,
        }
        (root / args.output).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"passed": result["passed"], "checks": checks}, indent=2))
        return 0 if result["passed"] else 1
    finally:
        if local is not None and local.poll() is None:
            local.send_signal(signal.SIGINT)
            try:
                local.wait(timeout=5)
            except subprocess.TimeoutExpired:
                local.terminate()
        subprocess.run(
            [*ssh, "pkill -f '^[^ ]*python[^ ]* -m telemetry_radio' || true"],
            check=False,
        )
        subprocess.run(["ssh", "-S", str(socket_path), "-O", "exit", args.pi], check=False)


if __name__ == "__main__":
    raise SystemExit(main())
