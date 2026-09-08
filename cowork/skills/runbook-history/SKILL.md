---
name: runbook-history
description: |
  Retrieves internal NOC runbooks and prior incident history. Use when the user
  asks for the standard procedure, historical tickets, or narrative context.
license: MIT
metadata:
  author: Microsoft
  version: "1.0"
---

# Runbook and incident history

## Workflow

1. Extract the fault type, technology, asset, and any incident identifier.
2. Call `noc_investigate` with a narrow request explicitly asking the Foundry IQ knowledge specialist for runbook and prior-ticket evidence only.
3. Return the applicable runbook step or section and clearly identified prior incidents.
4. Separate documented narrative from current operational evidence.

For the reference scenario, use: `What's our standard runbook for a fibre cut on a DWDM link, and has anything like INC-2025-08-14-0042 happened before?`

Do not claim current telemetry, current topology state, or actions performed. Those belong to RTI and Fabric IQ workflows.
