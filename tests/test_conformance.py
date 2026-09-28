import json
from pathlib import Path

import pytest

from telemetry_radio.conformance import validate_fixture


def test_protocol_v4_golden_vectors_are_unchanged():
    assert validate_fixture() == ["data", "ack", "sync"]


def test_conformance_validator_reports_accidental_wire_change(tmp_path):
    fixture = json.loads(Path("fixtures/protocol_v4.json").read_text(encoding="utf-8"))
    fixture["vectors"][0]["wire_hex"] = "00" + fixture["vectors"][0]["wire_hex"][2:]
    changed = tmp_path / "changed.json"
    changed.write_text(json.dumps(fixture), encoding="utf-8")
    with pytest.raises(ValueError, match="signed wire bytes changed"):
        validate_fixture(changed)
