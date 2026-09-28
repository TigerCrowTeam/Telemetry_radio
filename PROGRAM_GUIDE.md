# Telemetry Radio v1.0 Program Guide

## What this program does

Telemetry Radio is a Python application that carries MAVLink traffic across a
bidirectional software-defined-radio link. The current v1.0 installation connects a
Cube Orange flight controller to QGroundControl through two Ettus B205/B205mini SDRs:

```text
Cube Orange -> Raspberry Pi -> B205 SDR <-> B205 SDR -> workstation -> QGroundControl
```

The Raspberry Pi is the **master** radio node. It reads MAVLink from the Cube and
transmits selected telemetry. The workstation is the **GCS** radio node. It delivers
received MAVLink to QGroundControl over UDP and returns GCS requests and commands to
the aircraft side.

The v1.0 hardware-specific configurations are:

- `configs/demo-master-qpsk-b205.yaml` — Raspberry Pi, Cube, and SDR serial `3457464`
- `configs/demo-gcs-qpsk-b205.yaml` — workstation, QGroundControl, and SDR serial
  `3457480`

These files contain machine-specific serial-device paths, key paths, and SDR serial
numbers. Update those values if the hardware or user accounts change.

## v1.0 capabilities

The program currently supports:

- Bidirectional MAVLink transport between a flight controller and a GCS
- QPSK and BPSK modem implementations
- Reed-Solomon parity, interleaving, packet CRC, and configurable bit repetition
- Ed25519 packet authentication and per-node public-key trust
- Replay protection with persistent replay state
- Priority handling for heartbeat, attitude, control, and command traffic
- Stale-telemetry replacement and coalescing to favor current orientation data
- Acknowledgements, retries, duplicate suppression, and bounded queues
- Time-division duplex scheduling on a shared half-duplex RF channel
- Hardware-timed B205 transmit/receive operations
- Clock synchronization between radio nodes
- Synchronized frequency hopping and loss-of-synchronization recovery
- MAVLink routing among master, GCS, and optional slave nodes
- Local text/JSON status, an HTTP status API, Prometheus metrics, and alert rules
- Bounded IQ/frame recording and replay for troubleshooting
- Simulation and automated protocol, DSP, routing, reliability, and scheduling tests

### Current v1.0 radio profile

The tested v1.0 conducted-link profile uses:

| Setting | Value |
|---|---:|
| Modulation | QPSK |
| Sample rate | 2 Msps |
| RF bandwidth | 1 MHz |
| Samples per symbol | 4 |
| Reed-Solomon parity | 16 bytes |
| Interleaver depth | 16 |
| Frequencies | 900, 920, 940, 960, 980, 1000 MHz |
| Hop pattern | Ping-pong |
| Hop interval | 250 ms (4 hops/second) |
| Largest adjacent hop | 20 MHz |
| TX gain | 50 |
| RX gain | 40 |
| TX port | `TX/RX` |
| RX port | `RX2` |

Both endpoints must use the identical ordered frequency list, epoch, hopping mode,
hop interval, modulation, FEC, and TDD parameters.

## Limitations and safety boundaries

This is a first release, not a flight-qualified command-and-control radio.

- The accepted setup is a **conducted cabled bench link with 60 dB total attenuation
  between transmitter and receiver**. Do not connect a transmitter directly to an
  SDR receiver.
- The configured frequencies and gains are not authorization to radiate. Confirm
  local spectrum rules, licensing, emissions, antennas, power, and duty cycle before
  any over-the-air test.
- Six channels from 900 to 1000 MHz are the characterized v1.0 plan. Wider plans,
  more channels, and faster hopping did not consistently preserve telemetry quality.
- A 200 ms interval can work for short runs, but was not repeatable over longer
  measurements. v1.0 therefore uses the safer 250 ms interval.
- Even at 250 ms, long-run orientation timing can occasionally exceed the project's
  strict 220 ms ATTITUDE-p95 target. The link can remain connected while motion in
  QGroundControl appears delayed or choppy.
- Normal B205/UHD tuning sometimes invokes synthesizer calibration and can take well
  over 100 ms. The experimental timed-tuning path does not provide AD936x fast-lock
  profile recall and remains disabled.
- High-rate attitude traffic is prioritized. Bulk operations such as full parameter
  downloads, mission transfers, and other low-priority MAVLink traffic can be slow.
- Authentication provides identity and integrity, but not confidentiality. MAVLink
  payloads are not encrypted by this application.
- Clock agreement is essential. A large host-time difference can put the two radios
  on different frequencies or TDD slots.
- USB bandwidth, SDR power stability, Cube serial availability, and host scheduling
  affect operation. Power the Cube independently and ensure each B205 has a stable
  USB 3 connection.
- Do not use the link as the sole safety mechanism. The vehicle must retain suitable
  onboard failsafes and a safe response to loss of communications.

## Installation

Use Python 3.10 or newer on both computers. Install the system's UHD and SoapySDR UHD
support first, then install the Python project:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[sdr]'
```

The operating system may provide the `SoapySDR` Python module instead of pip. Verify
that the chosen Python interpreter can import it and can discover the B205 before
starting the radio.

Validate the paired v1.0 configurations:

```bash
.venv/bin/python -m telemetry_radio.validate \
  configs/demo-gcs-qpsk-b205.yaml \
  configs/demo-master-qpsk-b205.yaml
```

Add `--hardware` when the SDRs are connected and a hardware capability check is
appropriate. Do not run two programs against the same SDR simultaneously.

## Authentication setup

Each node needs its own Ed25519 private key and the public keys of every trusted
node. Key files must match the paths in the YAML configuration. Private keys must
not be committed to this repository or copied into logs and captures.

The application refuses normal startup when no private key is configured. The
`--allow-unauthenticated` option exists only for an isolated conducted bench and
must not be used over the air.

After replacing keys or copying a configuration to another machine, verify file
ownership and restrictive private-key permissions. Preserve replay-state files
during normal restarts; deliberately reset them only as part of coordinated key or
test-state maintenance.

## Hardware setup

For the established conducted test:

1. Power the Cube Orange externally and connect its USB/serial interface to the Pi.
2. Connect the Pi B205 to a stable USB 3 port.
3. Connect the workstation B205 to a stable USB 3 port.
4. Connect each B205 `TX/RX` output through the rated attenuated coax path to the
   other B205's `RX2` input.
5. Confirm the paths contain the intended attenuation and are rated for every
   configured frequency.
6. Start QGroundControl on the workstation. It listens for MAVLink on UDP port 14550.
7. Synchronize the Pi and workstation clocks using the same reliable time source.

## Starting v1.0

Start the master on the Raspberry Pi:

```bash
cd /home/master/telemetry-radio
.venv/bin/python -m telemetry_radio \
  --config configs/demo-master-qpsk-b205.yaml \
  --status text --status-interval 2
```

Start the GCS node on the workstation:

```bash
cd "/home/dev5/Documents/Vector Shift Alpha/Telemetry Radio"
.venv/bin/python -m telemetry_radio \
  --config configs/demo-gcs-qpsk-b205.yaml \
  --status text --status-interval 2
```

The order is not critical, although starting the Pi/master first makes acquisition
easier to observe. Allow several seconds for SDR initialization, clock alignment,
and telemetry acquisition. QGroundControl should then discover the vehicle through
`udpout:127.0.0.1:14550`.

## Monitoring operation

By default the local status server listens only on `127.0.0.1:8765`.

```bash
.venv/bin/python -m telemetry_radio.status_cli --format text
.venv/bin/python -m telemetry_radio.status_cli --format json
.venv/bin/python -m telemetry_radio.status_cli --format prometheus
```

The direct endpoints are:

- `GET /status` — complete JSON snapshot
- `GET /metrics` — Prometheus-format metrics
- `GET /healthz` — HTTP 200 while synchronized, otherwise HTTP 503

Important values to watch include:

- `synchronized` should remain true.
- `flight_controller_heartbeat_stale` should remain false.
- `flight_controller_heartbeat_age_ms` should normally stay below 2500 ms.
- `active_frequency_hz` should follow the configured ping-pong sequence.
- `frequency_hops` should continue increasing.
- `crc_failures`, `decoder_output_drops`, `timed_transmit_late`, and
  `transmit_errors` should not increase during a healthy run.
- ATTITUDE interval p95 indicates perceived orientation smoothness; lower is better.

Prometheus alert examples are retained in
`deployment/prometheus/telemetry-radio-alerts.yml`.

## Stopping the program

Use `Ctrl+C` in each radio terminal. A normal shutdown releases the SDR and closes
the MAVLink connection. If a detached process must be stopped, target only this
application:

```bash
pkill -f '^[^ ]*python[^ ]* -m telemetry_radio'
```

Confirm that no radio process remains before restarting or running a hardware test:

```bash
pgrep -af '^[^ ]*python[^ ]* -m telemetry_radio'
```

## Common problems

### QGroundControl shows no vehicle

Check that both radio processes are running, both SDR serial numbers are correct,
the nodes report synchronization, the Cube serial path exists, and QGroundControl is
listening on UDP 14550. Also confirm that another program is not occupying the SDR
or UDP endpoint.

### The nodes are on different frequencies

Compare the complete `frequency` sections in both YAML files and verify host clock
synchronization. The ordered channel list and timing values must match exactly.

### Telemetry is choppy

Check ATTITUDE interval statistics, queue depth, stale-packet counts, USB errors,
late transmissions, and heartbeat age. Do not reduce the hop interval below 250 ms
for the v1.0 profile without repeating conducted acceptance testing.

### SDR startup fails

Confirm USB 3 operation, SDR power, UHD/Soapy discovery, the configured serial
number, device permissions, and that no old radio or diagnostic process still owns
the B205. Startup retries are configured, but they cannot recover a missing or
exclusively owned device.

### Authentication fails

Verify that each private key matches its public key, every peer public key is listed
under the correct node ID, permissions allow the radio user to read the key, and
replay-state files are writable by that user.

## Development and verification

Run the automated suite after any code or configuration change:

```bash
python -m pip install -e '.[test]'
python -m pytest -q
```

The conducted two-node acceptance runner is `tools/run_acceptance.py`. It starts the
Pi and workstation nodes, waits for acquisition, samples link health, writes a JSON
result, and stops both processes. Its defaults are specific to the current bench, so
review its Pi address, remote directory, expected channel list, duration, and safety
setup before using it.

Any change to modulation, FEC, sample rate, TDD timing, hopping frequencies, dwell,
gain, antenna port, hardware, or host timing should be treated as a new radio profile
and revalidated on a properly attenuated conducted link before operational use.
