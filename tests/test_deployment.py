import re
from pathlib import Path

import yaml

from telemetry_radio.statistics import LinkStatistics

ROOT = Path(__file__).parents[1]
SERVICE = ROOT / "deployment/systemd/telemetry-radio@.service"
ALERTS = ROOT / "deployment/prometheus/telemetry-radio-alerts.yml"


def test_systemd_service_runs_named_config_as_unprivileged_user():
    unit = SERVICE.read_text(encoding="utf-8")
    assert "User=telemetry-radio" in unit
    assert "Group=telemetry-radio" in unit
    assert "--config /etc/telemetry-radio/%i.yaml" in unit
    assert "StateDirectory=telemetry-radio" in unit
    assert "WantedBy=multi-user.target" in unit


def test_systemd_service_restarts_failures_and_logs_to_journal():
    unit = SERVICE.read_text(encoding="utf-8")
    assert "Restart=on-failure" in unit
    assert "RestartSec=5" in unit
    assert "StartLimitBurst=5" in unit
    assert "StandardOutput=journal" in unit
    assert "StandardError=journal" in unit
    assert "SyslogIdentifier=telemetry-radio-%i" in unit
    assert "KillSignal=SIGTERM" in unit


def test_prometheus_alert_rules_reference_exported_metrics():
    rules = yaml.safe_load(ALERTS.read_text(encoding="utf-8"))
    expressions = " ".join(rule["expr"] for group in rules["groups"] for rule in group["rules"])
    referenced = set(re.findall(r"telemetry_radio_[a-z_]+", expressions))
    exported = {
        f"telemetry_radio_{name}"
        for name in (
            *LinkStatistics.COUNTERS,
            "uptime_seconds",
            "queue_depth",
            "queue_capacity",
            "awaiting_acknowledgements",
            "estimated_signal_power",
            "active_frequency_hz",
            "frequency_slot",
            "transmit_allowed",
            "synchronized",
        )
    }
    assert referenced <= exported
    assert {rule["labels"]["severity"] for group in rules["groups"] for rule in group["rules"]} == {
        "warning",
        "critical",
    }
