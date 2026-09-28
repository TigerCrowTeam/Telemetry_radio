from __future__ import annotations

import argparse
import grp
import json
import platform
import pwd
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from .authentication import load_private_key, load_public_keys
from .config import RadioConfig, load_config
from .validate import validate_hardware


@dataclass(frozen=True)
class Result:
    check: str
    status: str
    detail: str


def _result(check: str, ok: bool, detail: str) -> Result:
    return Result(check, "PASS" if ok else "FAIL", detail)


def _run(*command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False, timeout=15)


def _mode_allows(path: Path, user: pwd.struct_passwd, groups: set[int], bits: int) -> bool:
    mode = path.stat()
    shift = 6 if mode.st_uid == user.pw_uid else 3 if mode.st_gid in groups else 0
    return ((mode.st_mode >> shift) & bits) == bits


def _path_access(path: Path, user: pwd.struct_passwd, write: bool = False) -> bool:
    """Evaluate conventional Unix mode bits for a service account without changing UID."""
    try:
        target = path if path.exists() else path.parent
        groups = {user.pw_gid}
        groups.update(item.gr_gid for item in grp.getgrall() if user.pw_name in item.gr_mem)
        if not _mode_allows(target, user, groups, 0o6 if write else 0o4):
            return False
        return all(
            _mode_allows(parent, user, groups, 0o1)
            for parent in target.resolve().parents
            if parent != Path("/")
        )
    except OSError:
        return False


def check_installation(config_path: Path, unit_path: Path, service_user: str) -> list[Result]:
    results: list[Result] = []
    try:
        config = load_config(config_path)
        results.append(Result("configuration", "PASS", f"{config.role} node {config.node_id}"))
    except (OSError, ValueError) as exc:
        return [Result("configuration", "FAIL", str(exc))]

    try:
        account = pwd.getpwnam(service_user)
        results.append(Result("service account", "PASS", f"uid={account.pw_uid}"))
    except KeyError:
        account = None
        results.append(Result("service account", "FAIL", f"user {service_user!r} does not exist"))

    if account is not None:
        results.append(
            _result(
                "configuration permissions",
                _path_access(config_path, account),
                f"{service_user} can read {config_path}",
            )
        )

    results.extend(_check_keys(config, account))
    results.extend(_check_serial(config, account))
    results.extend(_check_unit(unit_path, service_user))
    return results


def _check_keys(config: RadioConfig, account: pwd.struct_passwd | None) -> list[Result]:
    authentication = config.authentication
    if authentication.private_key_file is None:
        return [Result("authentication key", "FAIL", "private_key_file is not configured")]
    private_path = Path(authentication.private_key_file)
    try:
        load_private_key(private_path)
        load_public_keys(authentication.public_key_files)
        valid = True
        detail = "private and trusted public keys are valid Ed25519 keys"
    except ValueError as exc:
        valid = False
        detail = str(exc)
    results = [_result("authentication keys", valid, detail)]
    if account is not None:
        results.append(
            _result(
                "private-key permissions",
                _path_access(private_path, account),
                f"{account.pw_name} can read {private_path}",
            )
        )
    return results


def _check_serial(config: RadioConfig, account: pwd.struct_passwd | None) -> list[Result]:
    connection = config.mavlink.connection
    if not connection.startswith("/dev/"):
        return [Result("serial device", "SKIP", f"network connection: {connection}")]
    path = Path(connection)
    exists = path.exists()
    results = [_result("serial device", exists, f"{path} exists")]
    if exists and account is not None:
        results.append(
            _result(
                "serial permissions",
                _path_access(path, account, write=True),
                f"{account.pw_name} can write {path}",
            )
        )
    return results


def _check_unit(path: Path, service_user: str = "telemetry-radio") -> list[Result]:
    required = (
        f"User={service_user}",
        "Restart=on-failure",
        "KillSignal=SIGTERM",
        "StandardOutput=journal",
        "WantedBy=multi-user.target",
    )
    try:
        contents = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [Result("systemd unit", "FAIL", str(exc))]
    missing = [directive for directive in required if directive not in contents]
    return [
        _result(
            "systemd unit", not missing, "missing: " + ", ".join(missing) if missing else str(path)
        )
    ]


def check_host(instance: str) -> list[Result]:
    machine = platform.machine()
    model_path = Path("/proc/device-tree/model")
    model = (
        model_path.read_text(errors="replace").rstrip("\0") if model_path.exists() else "unknown"
    )
    results = [
        _result("Raspberry Pi host", "Raspberry Pi" in model, f"{model}; architecture={machine}"),
    ]
    service = f"telemetry-radio@{instance}.service"
    for label, command in (
        ("service enabled at boot", ("systemctl", "is-enabled", service)),
        ("service active", ("systemctl", "is-active", service)),
    ):
        completed = _run(*command)
        detail = (completed.stdout or completed.stderr).strip()
        results.append(_result(label, completed.returncode == 0, detail or "no output"))
    persistent = Path("/var/log/journal").is_dir()
    results.append(
        _result(
            "persistent journal",
            persistent,
            "/var/log/journal exists" if persistent else "/var/log/journal is absent",
        )
    )
    return results


def run_checks(
    config_path: Path,
    unit_path: Path,
    service_user: str,
    instance: str,
    live: bool,
    hardware: bool,
) -> list[Result]:
    results = check_installation(config_path, unit_path, service_user)
    if live:
        results.extend(check_host(instance))
    if hardware:
        try:
            details = validate_hardware(load_config(config_path))
            results.append(Result("SDR access", "PASS", details))
        except (OSError, RuntimeError, ValueError) as exc:
            results.append(Result("SDR access", "FAIL", str(exc)))
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Raspberry Pi deployment preflight")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--instance", help="systemd instance name; defaults to config filename")
    parser.add_argument(
        "--unit", type=Path, default=Path("/etc/systemd/system/telemetry-radio@.service")
    )
    parser.add_argument("--service-user", default="telemetry-radio")
    parser.add_argument("--offline", action="store_true", help="skip live host and systemd checks")
    parser.add_argument("--hardware", action="store_true", help="require configured SDR access")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    instance = args.instance or args.config.stem
    results = run_checks(
        args.config, args.unit, args.service_user, instance, not args.offline, args.hardware
    )
    if args.json:
        print(json.dumps([asdict(item) for item in results], indent=2))
    else:
        for item in results:
            print(f"{item.status:4} {item.check}: {item.detail}")
    if any(item.status == "FAIL" for item in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
