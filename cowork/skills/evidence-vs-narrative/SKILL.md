---
name: evidence-vs-narrative
description: |
  Compares documented incident narrative with measured RTI evidence. Use when
  the user asks whether ticket timing or claims match actual telemetry.
license: MIT
metadata:
  author: Microsoft
  version: "1.0"
---

# Evidence versus narrative

## Workflow

1. Identify the incident, asset, narrative claim, and telemetry time window.
2. Call `noc_investigate` asking for both Foundry IQ ticket/runbook narrative and RTI Eventhouse evidence.
3. Present the ticket's stated time and the telemetry's earliest observed event side by side.
4. Calculate the difference only from returned timestamps and explain suppression or recording lag when supported.
5. When sources disagree, evidence wins for what physically happened while the ticket remains the record of what was documented.

For the reference scenario, use: `For the SYD-MEL fibre cut, what does the ticket say the time-to-detect was, versus what the actual telemetry shows?`

Do not silently reconcile conflicting timestamps or invent a cause for the discrepancy.
