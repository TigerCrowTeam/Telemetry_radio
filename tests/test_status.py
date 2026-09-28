import json
import logging

from telemetry_radio.config import StatusConfig
from telemetry_radio.statistics import LinkStatistics
from telemetry_radio.status import (
    StatusService,
    format_json,
    format_prometheus,
    format_text,
    render_endpoint,
)
from telemetry_radio.status_cli import render_status_response


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


def populated_statistics():
    statistics = LinkStatistics()
    statistics.increment("packets_transmitted", 4)
    statistics.increment("retries", 1)
    statistics.set_gauge("active_frequency_hz", 915e6)
    statistics.set_gauge("synchronized", True)
    return statistics


def test_human_and_json_status_formatters():
    snapshot = populated_statistics().snapshot()
    text = format_text(snapshot)
    assert "frequency=915.000 MHz" in text
    assert "tx=4" in text
    assert json.loads(format_json(snapshot))["retries"] == 1


def test_prometheus_formatter_includes_node_and_link_metrics():
    output = format_prometheus(populated_statistics().snapshot(), "master", 1)
    assert 'telemetry_radio_info{role="master",node_id="1"} 1' in output
    assert "# TYPE telemetry_radio_packets_transmitted counter" in output
    assert "# TYPE telemetry_radio_synchronized gauge" in output
    assert "telemetry_radio_packets_transmitted 4" in output
    assert "telemetry_radio_synchronized 1" in output


def test_http_status_metrics_and_health_endpoint_rendering():
    statistics = populated_statistics()
    snapshot = statistics.snapshot()
    code, content_type, body = render_endpoint("/status", snapshot, "slave", 2)
    assert code == 200 and content_type == "application/json"
    assert json.loads(body)["active_frequency_hz"] == 915e6
    assert "telemetry_radio_retries 1" in render_endpoint("/metrics", snapshot, "slave", 2)[2]
    assert render_endpoint("/healthz", snapshot, "slave", 2)[0] == 200
    statistics.set_gauge("synchronized", False)
    assert render_endpoint("/healthz", statistics.snapshot(), "slave", 2)[0] == 503
    assert render_endpoint("/missing", snapshot, "slave", 2)[0] == 404


def test_periodic_status_service_uses_injected_clock(caplog):
    clock = Clock()
    service = StatusService(
        populated_statistics(),
        StatusConfig(output="json", interval_seconds=5, http_enabled=False),
        "gcs",
        255,
        clock,
    )
    with caplog.at_level(logging.INFO):
        service.maybe_report()
        assert not caplog.records
        clock.now = 5
        service.maybe_report()
        assert '"packets_transmitted":4' in caplog.records[-1].message


def test_status_cli_renders_http_json_as_text_or_pretty_json():
    body = format_json(populated_statistics().snapshot())
    assert "frequency=915.000 MHz" in render_status_response(body, "text")
    assert json.loads(render_status_response(body, "json"))["packets_transmitted"] == 4
