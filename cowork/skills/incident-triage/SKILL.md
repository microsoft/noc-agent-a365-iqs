---
name: incident-triage
description: |
  Triages network incidents using grounded NOC evidence. Use when the user asks
  what happened, what is affected, how severe an alert is, or what to do next.
license: MIT
metadata:
  author: Microsoft
  version: "1.0"
---

# Incident triage

## Workflow

1. Identify the incident, affected device, link, service, location, and time window from the user's request.
2. Call `noc_investigate` with a narrow task asking for telemetry evidence, current blast radius, and the relevant runbook step.
3. Separate observed evidence from specialist inference. Name the specialist grounding each fact.
4. Report severity, affected services, likely cause, immediate safe actions, unknowns, and the next evidence to collect.

The tool is read-only. Never claim that a remediation, notification, or configuration change was executed.

If the request is broad, split it into focused calls. Cowork tool calls must finish in less than 30 seconds, while a full five-specialist investigation can take longer.
