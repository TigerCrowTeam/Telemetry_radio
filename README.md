# Telemetry Radio

A small, modular Python reference implementation of MAVLink transport over a
SoapySDR radio. GCS, master, and slave nodes run the same package and differ only
by YAML configuration. MAVLink wire frames are transported unchanged inside a
protocol-version/source/destination/32-bit-sequence/length/CRC radio frame.

The current over-air protocol version is `4`. Receivers explicitly reject frames
from incompatible protocol versions and expose the rejection through the
`protocol_version_mismatches` link statistic. Because wire revisions can change
the frame header, every node in an existing deployment must be upgraded together.

Protocol v4 has immutable golden vectors in `fixtures/protocol_v4.json`. They
cover signed DATA, ACK, and SYNC frames plus exact CRC, FEC/interleaving, and modem
envelope bytes. Validate an implementation or release candidate with:

```bash
telemetry-radio-conformance fixtures/protocol_v4.json
```

The deterministic private seed in that fixture exists only to make signatures
reproducible and must never be used as an operational identity. An intentional
wire-format change requires a protocol-version increment and a new fixture file;
do not rewrite the v4 vectors, because they remain the compatibility contract for
recordings and independent implementations of version 4.

The link layer queues traffic during frequency changes, uses listen-before-talk
with randomized backoff, acknowledges and retries unicast frames, suppresses
duplicate delivery, and explicitly switches the SDR between receive and transmit.
The master also broadcasts clock beacons so a GCS or slave can scan the configured
channel list and correct its schedule after losing synchronization.

## Before you begin

You need three Linux computers (a Raspberry Pi is suitable), one for each role:

- **GCS:** SDR connected to the computer running the ground-control software.
- **Master:** SDR and the master aircraft's Cube Orange connected to the same
  computer.
- **Slave:** SDR and a slave aircraft's Cube Orange connected to the same
  computer. Each additional slave uses the same procedure and a unique node ID.

Install SoapySDR, its Python bindings, and the SoapySDR driver for your particular
radio through the Linux package manager. Package names vary by distribution and
SDR model.

> **RF safety:** Begin with cabled RF through suitable attenuation or in a
> shielded test setup. Confirm that the configured frequencies, power, bandwidth,
> antenna, and duty cycle are legal in your jurisdiction before transmitting.

## Step 1: Install the code on every computer

Copy or clone this repository onto every node. From the project directory, run:

```bash
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -e '.[test]'
```

`--system-site-packages` makes SoapySDR bindings installed by the operating
system visible inside the virtual environment.

Check that SoapySDR can find the attached radio:

```bash
SoapySDRUtil --find
SoapySDRUtil --probe
python -c "import SoapySDR; print(SoapySDR.Device.enumerate())"
```

If these commands fail, install or correct the SoapySDR driver for the attached
hardware before continuing.

Validate each file without opening MAVLink or SDR devices:

```bash
python -m telemetry_radio.validate configs/master.yaml
```

Validate the complete network together:

```bash
python -m telemetry_radio.validate \
  configs/gcs.yaml configs/master.yaml configs/slave.yaml
```

Multi-file validation requires exactly one GCS and master and checks duplicate
radio/MAVLink identities, identical ordered frequency schedules, matching sample
rate/bandwidth/modulation settings, shared routing maps, and references to unknown
nodes. Include every additional slave configuration in this command.

Once an SDR is connected, compare a single config to the ranges reported by that
device without opening streams or transmitting:

```bash
python -m telemetry_radio.validate --hardware configs/master.yaml
```

Hardware validation checks RX and TX channel availability plus sample rate,
bandwidth, every hop frequency, and gain. Normal radio startup performs the same
hardware checks before applying settings or opening streams. The installed package
also provides the `telemetry-radio-validate` command.

The Soapy backend supports queuing an RX+TX tune at a future hardware timestamp via
`tune_at()`. On the current B205mini UHD plugin this improves boundary determinism
but does not eliminate the host-side calibration delay. The plugin does not expose
AD936x fast-lock profile management; do not enable sub-500 ms production hopping
until a UHD/firmware fast-lock backend has been installed and conducted-link tested.

## Step 2: Synchronize every computer's clock

All nodes derive the active channel independently from UTC system time. Enable
network time synchronization on every computer:

```bash
sudo timedatectl set-ntp true
timedatectl status
```

The following values must be identical in `gcs.yaml`, `master.yaml`, and every
`slave.yaml`:

```yaml
frequency:
  frequencies_hz: [905000000, 915000000, 925000000]
  epoch: 1767225600
  interval_seconds: 300
  guard_seconds: 0.25
  reacquire_dwell_seconds: 1.0
  sync_loss_seconds: 15.0
```

Do not reorder the frequency list or independently change the epoch or interval.
For systems without reliable network access, use a GPS-disciplined system clock
or another mechanism that keeps every node on the same UTC time.

Configuration loading also rejects reserved or role-conflicting node IDs,
duplicate frequencies, non-finite numeric values, invalid guards/reacquisition
timers, malformed backoff/rate/queue settings, duplicate route destinations,
empty connections, invalid status ports, and unsupported log levels. These checks
confirm technical consistency; they do not determine whether a frequency or power
level is legally authorized in a particular location.

## Step 3: Configure and run the master

Connect the master Cube Orange and an SDR to the master computer. Locate the
Cube's stable serial-device name:

```bash
ls -l /dev/serial/by-id/
```

Edit `configs/master.yaml` and replace the serial connection with the matching
path:

```yaml
role: master
node_id: 1
relay_enabled: true

mavlink:
  connection: /dev/serial/by-id/usb-your-cube-device
  baud: 115200
```

If the current user cannot open the serial device, add it to the usual serial
device group, then log out and back in:

```bash
sudo usermod -aG dialout "$USER"
```

Start the master:

```bash
source .venv/bin/activate
python -m telemetry_radio --config configs/master.yaml
```

The master transports MAVLink for its own Cube and relays applicable traffic
between slave nodes and GCS node `255`.

## Step 4: Configure and run a slave

Connect the slave Cube Orange and an SDR to the slave computer. Find the Cube
under `/dev/serial/by-id/`, then edit `configs/slave.yaml`:

```yaml
role: slave
node_id: 2

mavlink:
  connection: /dev/serial/by-id/usb-your-slave-cube
  baud: 115200
```

Start the slave:

```bash
source .venv/bin/activate
python -m telemetry_radio --config configs/slave.yaml
```

For another slave, copy the configuration and assign a unique ID such as
`node_id: 3`. Do not reuse `1`, which belongs to the master, or `255`, which
belongs to the GCS.

Each Cube must also use a unique MAVLink system ID (`MAV_SYS_ID` in ArduPilot).
The system ID is independent of the radio node ID, although using matching values
such as system/node `2` keeps configuration simple.

## Step 5: Configure and run the GCS radio

Connect an SDR to the GCS computer. The example `configs/gcs.yaml` uses:

```yaml
role: gcs
node_id: 255

mavlink:
  connection: udp:127.0.0.1:14550
```

Start the radio process first:

```bash
source .venv/bin/activate
python -m telemetry_radio --config configs/gcs.yaml
```

Then start QGroundControl, Mission Planner, or another MAVLink GCS and configure
it to use UDP port `14550`. If the generic `udp:` form conflicts with the GCS's
listening behavior, try the explicit output form:

```yaml
mavlink:
  connection: udpout:127.0.0.1:14550
```

UDP behavior differs between GCS applications. Verify both incoming telemetry
and outgoing commands during bench testing.

## Step 6: Select a particular SDR when necessary

The examples use SoapySDR auto-detection:

```yaml
sdr:
  device: {}
```

When multiple radios are attached, copy the identifying fields reported by
`SoapySDRUtil --find`. For example:

```yaml
sdr:
  device:
    driver: lime
    serial: "000907..."
```

Gain, bandwidth, sample rate, channel, and samples per symbol can also be set in
this section. The supported ranges depend on the selected hardware.

Link reliability is configured separately:

```yaml
link:
  ack_timeout_seconds: 0.6
  max_retries: 4
  duplicate_window_seconds: 30.0
  channel_busy_threshold: 0.02
  backoff_min_seconds: 0.01
  backoff_max_seconds: 0.08
  queue_size: 256
  reserved_high_priority_slots: 32
  telemetry_rate_bytes_per_second: 6000
  telemetry_burst_bytes: 8192
  bulk_rate_bytes_per_second: 1000
  bulk_burst_bytes: 8192
  low_priority_max_age_seconds: 5
  coalesce_telemetry: true
```

Unicast data is retried until acknowledged or the retry limit is reached.
Broadcast data is not acknowledged because many simultaneous acknowledgements
would collide. Traffic remains queued during the retune guard interval; if the
bounded queue fills, priority determines whether new traffic is admitted.

The transmit queue uses these classes, from highest to lowest:

1. Radio-link acknowledgements and clock synchronization
2. Emergency MAVLink commands, including flight termination and arm/disarm
3. Commands and active control messages
4. Routine telemetry
5. Bulk log, file-transfer, and encapsulated-data traffic

Ordering remains FIFO within each class. If the queue is full, incoming traffic
can evict the oldest item from the lowest class only when the incoming item has a
higher priority. Lower- or equal-priority arrivals are dropped instead, protecting
already queued control traffic. Retransmissions retain the original packet's
priority.

Routine telemetry and bulk transfers use independent token buckets. The rate is
the sustained on-air envelope byte rate after packet framing, signature, CRC, FEC
padding/interleaving, preamble, and modem length fields. The burst setting is the
temporary allowance in those same bytes. A rate of `0` disables that class's limiter. Commands,
emergency traffic, link acknowledgements, and synchronization packets bypass
these low-priority rate limits.

`reserved_high_priority_slots` prevents telemetry and bulk traffic from consuming
the entire bounded queue. With telemetry coalescing enabled, a newer unsent sample
from the same MAVLink source system, message ID, and destination replaces the old
sample. This favors current state over a backlog of stale state. Low-priority
packets older than `low_priority_max_age_seconds` are discarded; commands and
link-control packets do not expire this way.

The example limits leave space below the simple BPSK modem's nominal gross bit
rate. They are starting values, not field-tuned values; retries, channel
contention, SDR turnaround, and guard intervals further reduce usable MAVLink
throughput. Every retransmission is charged again when it is actually sent.

### Link statistics

Each running node exposes a thread-safe, JSON-friendly snapshot through
`app.statistics.snapshot()`. It includes:

- Radio packets and complete on-air envelope bytes transmitted and received
- BPSK samples transmitted and calculated RF airtime in seconds
- Invalid packets and CRC failures
- Retries and acknowledgements sent/received
- Suppressed duplicates
- Queue drops, priority evictions, telemetry coalescing, and stale drops
- Rate-limit and busy-channel deferrals
- Transmit errors, synchronization losses, and successful reacquisitions
- MAVLink frames accepted from and delivered to local endpoints
- Queue depth and unacknowledged-packet count
- Estimated channel power, active frequency, frequency slot, and guard/TX state
- Synchronization state and process uptime

For example:

```python
snapshot = app.statistics.snapshot()
print(snapshot["active_frequency_hz"])
print(snapshot["retries"])
print(snapshot["queue_depth"])
print(snapshot["airtime_seconds_transmitted"])
```

The snapshot API is intentionally presentation-neutral. The human/JSON status
interface consumes the same atomic snapshots.

### Status CLI and HTTP endpoints

The example configurations periodically log a compact text summary and serve
status on loopback only:

```yaml
status:
  output: text             # off, text, or json
  interval_seconds: 10
  http_enabled: true
  http_host: 127.0.0.1
  http_port: 8765
```

Runtime flags can override these settings:

```bash
python -m telemetry_radio --config configs/master.yaml --status json
python -m telemetry_radio --config configs/master.yaml --status off --no-status-http
python -m telemetry_radio --config configs/master.yaml --status-http-port 9000
```

While the radio process is running, query it from the same computer:

```bash
python -m telemetry_radio.status_cli --format text
python -m telemetry_radio.status_cli --format json
python -m telemetry_radio.status_cli --format prometheus
```

An installed editable package also provides the `telemetry-radio-status` command.
Use `--url http://127.0.0.1:9000` when the port was changed. Direct HTTP endpoints
are:

- `GET /status` — complete JSON snapshot
- `GET /metrics` — Prometheus text exposition
- `GET /healthz` — HTTP 200 while synchronized, otherwise HTTP 503

The default loopback bind prevents remote access. Binding to a non-loopback
address exposes operational data without authentication; use a firewall or an
authenticated reverse proxy if remote monitoring is required.

### Leadership demonstration

The live frequency-hopping evidence view is available as:

```bash
telemetry-radio-demo
# or, from a source checkout:
python -m telemetry_radio.demo_monitor
```

It highlights the active channel and requires fresh MAVLink delivery on every
configured frequency before displaying a continuity PASS verdict.

Initial operational thresholds and ready-to-load Prometheus alert rules are in
`deployment/MONITORING.md` and
`deployment/prometheus/telemetry-radio-alerts.yml`. Calibrate device-dependent
signal and RF-quality limits during controlled bench testing.

## Run as a Linux system service

The templated unit in `deployment/systemd/telemetry-radio@.service` runs each
radio node under a dedicated account, restarts it after failures, shuts it down
gracefully with `SIGTERM`, and sends application output to the systemd journal.
The examples below install a node from a checkout at `/opt/telemetry-radio`:

```bash
sudo useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin telemetry-radio
sudo install -d -o root -g telemetry-radio -m 0750 /etc/telemetry-radio
sudo install -d -o root -g telemetry-radio -m 0750 /etc/telemetry-radio/keys
sudo install -o root -g telemetry-radio -m 0640 \
  configs/master.yaml /etc/telemetry-radio/master.yaml
sudo install -o root -g root -m 0644 \
  deployment/systemd/telemetry-radio@.service \
  /etc/systemd/system/telemetry-radio@.service
sudo install -o root -g root -m 0644 \
  deployment/telemetry-radio.default /etc/default/telemetry-radio
```

Create the virtual environment at the path used by the unit and make the
application tree read-only to the service account:

```bash
sudo install -d -o root -g root -m 0755 /opt/telemetry-radio
sudo cp -a . /opt/telemetry-radio/source
sudo python3 -m venv --system-site-packages /opt/telemetry-radio/.venv
sudo /opt/telemetry-radio/.venv/bin/pip install /opt/telemetry-radio/source
```

Grant the `telemetry-radio` user access to the serial and SDR devices using the
groups assigned by the machine's udev rules. On Raspberry Pi OS this commonly
includes `dialout` for `/dev/ttyACM*` and `plugdev` for USB SDRs:

```bash
ls -l /dev/ttyACM0
sudo usermod -aG dialout,plugdev telemetry-radio
```

If the SDR uses a different group, add that group instead. Then validate and
enable the named instance:

```bash
sudo -u telemetry-radio /opt/telemetry-radio/.venv/bin/telemetry-radio-validate \
  /etc/telemetry-radio/master.yaml
sudo systemctl daemon-reload
sudo systemctl enable --now telemetry-radio@master.service
systemctl status telemetry-radio@master.service
```

Before operational use, run the read-only deployment preflight. `--hardware`
opens the configured SDR and verifies its capabilities:

```bash
sudo /opt/telemetry-radio/.venv/bin/telemetry-radio-deployment-check \
  --config /etc/telemetry-radio/master.yaml --instance master --hardware
```

The preflight is read-only and does not exercise disruptive reboot, reconnection,
or service-recovery tests.

The instance name selects `/etc/telemetry-radio/<instance>.yaml`, so another
machine can use `telemetry-radio@gcs` or `telemetry-radio@slave2` after installing
the corresponding configuration. Optional common command-line overrides can be
placed in `/etc/default/telemetry-radio`.

Use journald for live and historical logs rather than a separate application log
file; journal retention and rotation remain under the operating system's policy:

```bash
journalctl -u telemetry-radio@master.service -f
journalctl -u telemetry-radio@master.service --since today
```

The YAML `log_level` setting controls verbosity. `INFO` is suitable for normal
operation; temporarily use `DEBUG` while diagnosing a link or hardware problem.

## Record and replay radio failures

Enable recording on a node by selecting a capture directory. IQ and decoded
frames can be enabled independently, though keeping both provides the best fault
record. The service account must have write access to the directory.

```yaml
recording:
  directory: /var/lib/telemetry-radio/captures/master
  iq: true
  frames: true
  max_file_bytes: 268435456
  max_sessions: 10
  min_free_bytes: 134217728
```

The supplied systemd unit creates `/var/lib/telemetry-radio` with ownership for
the service account. The application creates the configured subdirectories when
it starts; no broader filesystem write access is required.

Every process start creates a uniquely named `capture-...` session directory. The
oldest complete sessions are removed before a new session starts when
`max_sessions` would be exceeded; unrelated directories are never selected.
Each session contains:

- `manifest.json` — capture format, protocol, sample rate, and modem settings.
- `iq-00000.c64`, `iq-00001.c64`, ... — bounded raw little-endian complex64 IQ
  segments.
- `frames-00000.jsonl`, `frames-00001.jsonl`, ... — bounded base64 wire-frame
  segments with timestamps, validity, and decode errors.

Copy a capture from the radio computer and rerun the complete demodulation and
radio-frame decoder without SDR hardware:

```bash
telemetry-radio-replay ./capture-20260825T120000Z-1234-a1b2c3 --source iq
```

To bypass demodulation and investigate framing, CRC, or protocol failures from
the exact wire bytes that the modem originally produced:

```bash
telemetry-radio-replay ./capture-20260825T120000Z-1234-a1b2c3 --source frames
```

Replay reads all session segments in order and emits one compact JSON object per
recovered frame, including source, destination, sequence, kind, base64 payload, or
the precise decode error. Before each write, recording verifies that the configured
minimum free space will remain. A full disk, permission error, unavailable path, or
other capture failure disables recording, increments `recording_failures`, and logs
the reason without interrupting radio traffic. Raw IQ grows at eight bytes per
sample, so tune file/session limits for the installed storage and archive important
failure sessions before retention removes them.

### Forward-error correction and interleaving

Before modulation, every four payload bits are encoded as an extended Hamming
8/4 codeword. Receivers correct any single-bit codeword error, detect two-bit
errors, and still apply the radio frame CRC after correction so larger or
miscorrected patterns cannot be delivered silently. Codeword bits are transmitted
through a depth-16 block interleaver: a contiguous burst of up to 16 bits is
spread across separate codewords and can therefore be corrected.

The status interface exposes `fec_corrected_bits` and
`fec_uncorrectable_frames`. A rising corrected count shows that FEC is recovering
traffic; uncorrectable frames are discarded and counted as invalid packets before
MAVLink delivery. FEC doubles the encoded payload length and corresponding
airtime, so rate limits and RF duty-cycle planning should account for that
overhead.

FEC, signatures, and session identifiers change the air interface, so the protocol
version is now `4` and every node must be upgraded together.

### Packet authentication

Production configurations sign every radio packet—including MAVLink commands,
ACKs, and clock-synchronization beacons—with a per-node Ed25519 identity. A node
stores only its own private key and the public keys of its peers, so compromising a
slave does not expose the master's signing key. Signatures cover protocol version,
kind, source, destination, session ID, sequence, length, and payload and are
verified before packet interpretation.

Generate a separate key pair for every node on a trusted computer. The management
CLI creates a mode-`0600` private key, a mode-`0644` public key, and prints the
public-key fingerprint:

```bash
telemetry-radio-keys generate 1 ./provisioning --label initial
sudo install -o root -g telemetry-radio -m 0640 \
  provisioning/node1-initial-private.pem \
  /etc/telemetry-radio/keys/master-private.pem
```

Securely install each private key only on its owning node. Distribute public keys
to every node, then configure the local private identity and trusted node map:

```yaml
authentication:
  private_key_file: /etc/telemetry-radio/keys/master-private.pem
  public_key_files:
    1: [/etc/telemetry-radio/keys/master-public.pem]
    2: [/etc/telemetry-radio/keys/slave2-public.pem]
    255: [/etc/telemetry-radio/keys/gcs-public.pem]
  replay_window: 128
  replay_state_file: /var/lib/telemetry-radio/master-replay-state.json
  sync_max_age_seconds: 10
  sync_max_future_seconds: 10
```

The receiver rejects unsigned frames, unknown source identities, and invalid
signatures, incrementing `authentication_failures`. Every process start creates a
new time-ordered 64-bit session ID with random collision entropy. A per-source
sliding sequence window accepts limited
out-of-order traffic once, suppresses duplicates, rejects stale frames, and refuses
traffic from retired sessions. The replay window is atomically persisted so those
guarantees survive receiver restarts. Authenticated SYNC beacons must also progress
within the configured age and future tolerances before they can change the scheduler.

The normal CLI refuses to start without `private_key_file`.
For an isolated RF-shielded bench only, `--allow-unauthenticated` explicitly
enables insecure operation; direct software simulations do not need the flag.
Authentication does not encrypt traffic, so anyone receiving the signal can still
observe payloads.

Authenticated captures require the relevant public keys during frame inspection:

```bash
telemetry-radio-replay ./master-capture --source frames \
  --public-key 1=./master-public.pem \
  --public-key 2=./slave2-public.pem
```

Treat captures and private keys as sensitive operational material. Do not commit
private keys, copy them into logs, or reuse identities across unrelated networks.

For the two-node conducted setup, run the complete authenticated acceptance check
from the workstation with:

```bash
.venv/bin/python tools/run_acceptance.py
```

The command prompts once for the Pi SSH login, starts both endpoints, measures the
link, writes `acceptance-result.json`, and stops both endpoints. A passing result
requires synchronization, a fresh component-1 flight-controller heartbeat,
`ATTITUDE` p95 spacing no greater than 220 ms, heartbeat p95 spacing no greater
than 2.1 seconds, observation of 905/915/925 MHz, no decoder output drops, no new
CRC failures, and no new timed-late or transmit errors. The hopping candidate uses
a 20-second dwell, so keep the default three-minute measurement for a prolonged
test or use at least 45 seconds to guarantee observing all three channels.

#### Key lifecycle and rotation

Maintain an offline inventory containing each network name, node ID, public-key
fingerprint, creation date, activation date, and retirement date. Inspect keys or
confirm that a private/public pair matches without printing private material:

```bash
telemetry-radio-keys inspect ./node1-initial-public.pem
telemetry-radio-keys verify \
  ./node1-initial-private.pem ./node1-initial-public.pem
```

Keep one encrypted offline backup of each active private key with access limited to
authorized operators. Test restoration on an isolated computer using `verify`.
Never store backup passwords beside key archives. Public keys and fingerprints are
not secret and should be retained in the deployment inventory after retirement.

Rotate one node at a time with an overlap period:

1. Generate a new identity and independently compare its displayed fingerprint
   against the inventory.
2. Install the new public key on every node alongside the old public key by listing
   both paths under that source node's `public_key_files` entry.
3. Restart receivers and confirm their startup logs show both expected fingerprints.
4. Switch only the rotating node's `private_key_file` to the new private key and
   restart it.
5. Confirm authenticated traffic, ACKs, synchronization, and health metrics from
   every peer.
6. Remove the old public-key path from every receiver, restart them, and archive or
   securely destroy the retired private key according to policy.

For emergency revocation after suspected compromise, remove the compromised public
key from every node and restart receivers before provisioning a replacement. During
that interval, traffic signed by the revoked identity fails closed and increments
`authentication_failures`. If the master identity is revoked, clock beacons stop
being accepted until the replacement public key is deployed.

### MAVLink destination routing

Use the same MAVLink-system-to-radio-node map on the GCS, master, and slaves:

```yaml
routing:
  relay_via_master: true
  mavlink_system_to_node:
    1: 1  # MAV_SYS_ID 1 is attached to master radio node 1
    2: 2  # MAV_SYS_ID 2 is attached to slave radio node 2
    3: 3  # MAV_SYS_ID 3 is attached to slave radio node 3
```

The radio inspects addressing metadata in each complete MAVLink 1 or MAVLink 2
frame without decoding and re-encoding its payload. With `relay_via_master: true`,
a GCS command targeting MAVLink system 2 is sent as reliable unicast to the
master, then as reliable unicast from the master to radio node 2. The master does
not deliver that command to its own Cube, and unrelated slaves do not receive it.

Messages with `target_system: 0` remain broadcast. Messages that have no target
field, use an unknown target system, or cannot be inspected fall back to the
role's normal broadcast/master route. Broadcasts are not acknowledged. Setting
`default_destination` overrides destination-aware selection, so omit it when
using this map.

## Step 7: Test before flight

Run the hardware-independent tests on any computer:

```bash
pytest
```

The test suite includes a complete in-memory GCS/master/two-slave network. It
runs the production framing, modem, routing, ACK/retry, duplicate suppression,
frequency scheduler, and application-cycle logic while replacing only SoapySDR
and pymavlink endpoints with queues. The reusable simulation API is available as:

```python
from telemetry_radio.simulation import SimulatedChannel, SimulatedNetwork

channel = SimulatedChannel(
    packet_loss=0.05,
    noise_stddev=0.03,
    frequency_offset_hz=500,
    corruption_rate=0.01,
    random_delay_cycles=(0, 3),
    capture_ratio_db=10,
    max_transmissions_per_cycle=8,
)
network = SimulatedNetwork(configs, channel=channel)
network.step(3)  # initial tuning
network.inject("slave2", mavlink_wire_frame)
network.step(30)
assert mavlink_wire_frame in network.received("gcs")
network.close()
```

Here, `configs` is a dictionary mapping names such as `gcs`, `master`, and
`slave2` to `RadioConfig` objects. `VirtualMavlink` preserves injected byte
frames exactly, allowing tests to use captured or pymavlink-generated MAVLink
frames without a serial port or GCS application.

When used by `SimulatedNetwork`, the channel resolves transmissions in discrete
RF cycles. Bursts sent during the same cycle overlap. Per-link path gain can model
topology and hidden nodes:

```python
channel.set_path_gain(slave2_radio, slave3_radio, 0)  # mutually hidden
channel.set_path_gain(slave3_radio, slave2_radio, 0)
```

If one overlapping signal exceeds `capture_ratio_db`, the receiver captures it;
otherwise it receives the combined, normally CRC-invalid waveform. The
`channel.stats` dictionary reports transmissions, deliveries, collisions,
captures, corruption, impairment drops, delayed packets, and congestion drops. A
seeded channel produces repeatable test scenarios.

Simulation uses a controllable wall/monotonic clock, so long events do not need
real sleeps. `network.advance_time(301)` crosses a five-minute hop immediately.
Individual radios support deterministic fault injection:

```python
network.disconnect("slave2")
network.step(100)
network.reconnect("slave2")
network.nodes["master"].radio.drop_next()  # force the next transmission to fail
```

`clock_offsets={"slave2": -300}` passed to `SimulatedNetwork` starts that node
five minutes slow. Automated scenarios verify missed frequency boundaries,
master-beacon clock correction, CRC rejection of corrupted bursts, unicast retry
after a dropped ACK, duplicate suppression, and reacquisition after a temporary
disconnect.

Next, run all three roles in a controlled bench setup. Check that:

1. Each process reports the same scheduled frequency.
2. MAVLink telemetry reaches the GCS from the master and slave.
3. Commands from the GCS reach the intended autopilot.
4. Every node changes frequency at the same five-minute boundary.
5. A temporarily disconnected node scans and reacquires traffic after reconnecting.

## Synchronization and recovery

Every node must use the same POSIX UTC `epoch`, ordered frequency list, and
interval. It computes `floor((now - epoch) / interval)` independently; NTP or a
GPS-disciplined system clock is therefore required. Transmit is suppressed for
the configured guard time on either side of each boundary. After prolonged
silence, a node scans the predicted frequency followed by the remaining list,
accepting only frames that pass the packet CRC, then returns to the clock-derived
schedule.

## Current limitations

This repository is a reference implementation and is not yet flight-ready. The
BPSK receiver performs preamble-based timing acquisition and a basic squared-signal
carrier-offset estimate, but it is not a replacement for a field-tuned carrier and
timing loop. Listen-before-talk reduces collisions but cannot eliminate hidden-node
collisions. Production deployment should add or verify:

- Field-tuned carrier and symbol-timing tracking
- Hardware-specific RX/TX turnaround timing
- Stronger collision avoidance or scheduled time slots
- Encryption where confidentiality is required
- Regulatory limits, gain settings, bandwidth, and duty cycle

Clock beacons are signed, replay-filtered, and freshness-checked, but important
traffic sent as broadcast still cannot benefit from link-layer retries; select a
unicast destination where reliable delivery is required.

The `Modem` and `SoapyRadio` interfaces keep these upgrades isolated from MAVLink
handling, packet framing, routing, and scheduling.

## Controlled RF testing

Do not connect transmitters directly to SDR receiver inputs or begin over-the-air
testing from the example frequencies and gains. Begin with a rated, terminated
50-ohm conducted path and suitable attenuation; radiated and flight-like tests
require separate authorization and approval.

## Development quality gates

GitLab CI runs every push through separate quality, test, security, and packaging
stages. Runtime tests execute on Python 3.10, 3.11, 3.12, and 3.13. The coverage
gate starts at 75% branch coverage; raise the threshold as CLI, hardware-adapter,
and error-path coverage improves, but do not lower it to accommodate a regression.

Install the same tools used by CI and run the local gates before pushing:

```bash
python -m pip install -e '.[dev]'
ruff format --check .
ruff check .
mypy telemetry_radio
pytest --cov=telemetry_radio --cov-report=term-missing --cov-report=xml
pip-audit .
python -m build
twine check dist/*
telemetry-radio-conformance
```

Ruff is the formatting and linting authority; avoid manually restyling code in a
way that conflicts with `ruff format`. Mypy checks project code against the Python
3.10 language baseline while treating optional hardware and third-party packages
as external interfaces. The dependency audit requires network access to retrieve
current vulnerability data. Package validation builds both an sdist and wheel,
checks their metadata, installs the wheel in CI, and reruns protocol conformance.
