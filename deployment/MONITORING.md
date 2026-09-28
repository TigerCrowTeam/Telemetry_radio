# Operational Health and Alert Thresholds

The application exposes Prometheus text at `/metrics` and health at `/healthz`.
Scrape every node separately and attach stable `instance`, `site`, and `role` labels
in the Prometheus target configuration. Load the example rules from
`deployment/prometheus/telemetry-radio-alerts.yml` and validate them with
`promtool check rules` before deployment.

These are conservative initial thresholds, not flight-qualified limits. Establish
normal and degraded baselines during the controlled RF tests, retain the raw data,
and revise thresholds by SDR model, topology, traffic rate, and mission profile.

| Condition | Warning | Critical | Initial response |
| --- | --- | --- | --- |
| Target availability | — | Down for 2 minutes | Check process, host, scrape path, and power |
| Synchronization | More than 2 losses in 15 minutes | Unsynchronized for 30 seconds | Check master, clocks, schedule, and RF path |
| Retry ratio | Above 20% for 5 minutes with at least 10 transmissions | Set after RF baseline | Check interference, collisions, peers, and link margin |
| Authentication failures | Any in 5 minutes | More than 5 in 5 minutes | Verify key deployment; preserve logs and investigate traffic |
| Replay rejection | Any in 5 minutes | Set after security testing | Check restarts, state persistence, sessions, and hostile traffic |
| FEC corrections | Trend and baseline; no fixed count yet | Set after impairment testing | Compare against signal, retries, and uncorrectable frames |
| Uncorrectable FEC | More than 5 in 10 minutes | Set after impairment testing | Check RF path, frequency error, interference, and gain |
| Queue utilization | Above 75% for 5 minutes | Above 95% for 1 minute | Reduce input, inspect retries/rate limiting, restore capacity |
| Queue drops | Any in 5 minutes | Repeated loss of command-priority traffic | Identify priority and source; preserve queue/load evidence |
| Signal power | Below `1e-6` for 2 minutes while nonzero | Set from measured link margin | Check antenna/cable/gain; correlate with errors and retries |
| Transmit errors | — | Any in 5 minutes | Inspect SDR, USB, driver, power, and stream state |

`estimated_signal_power` is mean squared complex sample magnitude, not calibrated
dBm. Zero commonly means an idle read or timeout and is excluded from the starter
low-signal rule. Measure the healthy distribution and packet-error transition for
each device before assigning operational warning and critical values.

Counter alerts use `increase()` so process restarts do not look like negative
events. A restart can still hide a short event if metrics are scraped infrequently;
use a 15-second scrape interval initially and retain journald as the diagnostic
record. Alert routing should page for `critical`, create an operator-visible warning
for `warning`, and inhibit dependent link-quality alerts while the target is down.

Before flight-like use, review every threshold against results from backlog items
9–11. Record the approved values, responsible operator, date, software version,
hardware model, and rationale. Changes to thresholds should be reviewed like code.
