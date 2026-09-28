import json
import os
import pwd
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from telemetry_radio.deployment_check import check_installation, main


def _installation(tmp_path: Path) -> tuple[Path, Path]:
    private = Ed25519PrivateKey.generate()
    private_path = tmp_path / "private.pem"
    public_path = tmp_path / "public.pem"
    private_path.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    private_path.chmod(0o600)
    public_path.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    config = tmp_path / "gcs.yaml"
    config.write_text(
        f"""role: gcs
node_id: 255
authentication:
  private_key_file: {private_path}
  public_key_files:
    255: {public_path}
mavlink:
  connection: udp:127.0.0.1:14550
frequency:
  frequencies_hz: [915000000]
  epoch: 0
""",
        encoding="utf-8",
    )
    unit = tmp_path / "telemetry-radio@.service"
    unit.write_text(
        "\n".join(
            (
                f"User={pwd.getpwuid(os.getuid()).pw_name}",
                "Restart=on-failure",
                "KillSignal=SIGTERM",
                "StandardOutput=journal",
                "WantedBy=multi-user.target",
            )
        ),
        encoding="utf-8",
    )
    return config, unit


def test_offline_installation_checks_valid_files(tmp_path):
    config, unit = _installation(tmp_path)
    results = check_installation(config, unit, pwd.getpwuid(os.getuid()).pw_name)

    assert not [result for result in results if result.status == "FAIL"]
    assert {result.check for result in results} >= {
        "configuration",
        "service account",
        "authentication keys",
        "systemd unit",
    }
    assert next(result for result in results if result.check == "serial device").status == "SKIP"


def test_installation_reports_missing_service_account_and_unit_directives(tmp_path):
    config, unit = _installation(tmp_path)
    unit.write_text("User=telemetry-radio\n", encoding="utf-8")

    results = check_installation(config, unit, "account-that-cannot-exist")

    failures = {result.check: result.detail for result in results if result.status == "FAIL"}
    assert "service account" in failures
    assert "Restart=on-failure" in failures["systemd unit"]


def test_cli_json_returns_failure_for_missing_config(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(
        "sys.argv",
        [
            "telemetry-radio-deployment-check",
            "--config",
            str(tmp_path / "missing.yaml"),
            "--offline",
            "--json",
        ],
    )

    try:
        main()
    except SystemExit as exc:
        assert exc.code == 1
    else:
        raise AssertionError("missing configuration should fail")

    report = json.loads(capsys.readouterr().out)
    assert report[0]["check"] == "configuration"
    assert report[0]["status"] == "FAIL"
