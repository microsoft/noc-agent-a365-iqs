---
name: incident-telemetry
description: |
  Queries live Fabric RTI Eventhouse evidence for alert timelines, optical
  readings, per-sensor events, and suppression details.
license: MIT
metadata:
  author: Microsoft
  version: "1.0"
---

# Incident telemetry

## Workflow

1. Extract the exact asset and UTC time window.
2. Call `noc_investigate` with a narrow request explicitly asking the RTI incident-telemetry specialist for Eventhouse evidence, not ticket narrative.
3. Request one table at a time when needed: optical readings, network alerts, then incident events.
4. Return exact UTC timestamps, sensor IDs, measured dBm values, alert counts, and suppression state. Never smooth, infer, or rewrite measured values.

For live verification, use: `For LINK-SYD-MEL-FIBRE-01, what was the alert timeline and optical readings around 2025-08-14 03:22 UTC — when exactly did loss of light hit each sensor, and was anything suppressed?`

The expected fixture includes `SENS-SYD-MEL-F1-OPT-002` and `SENS-SYD-MEL-F1-OPT-003` dropping to approximately -30 dBm at `03:22:18`. Treat this only as a verification expectation; report the rows actually returned by Eventhouse.
