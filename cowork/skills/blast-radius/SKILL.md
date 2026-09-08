---
name: blast-radius
description: |
  Assesses network dependency and shared-infrastructure exposure. Use when the
  user asks which services, customers, SLA policies, routes, or redundant links
  are affected by a network fault.
license: MIT
metadata:
  author: Microsoft
  version: "1.0"
---

# Blast-radius assessment

## Workflow

1. Extract the exact link, router, amplifier, conduit, or site identifier.
2. Call `noc_investigate` with a narrow request explicitly asking the Fabric IQ topology specialist for dependent services, customers, SLA exposure, alternate paths, and shared-conduit risk.
3. Present confirmed dependencies first, then indirect or potential exposure.
4. Always check whether logical redundancy shares the same physical conduit; do not treat a second logical link as independent until this is verified.
5. State when topology has no matching entity instead of treating an empty result as a connection failure.

For the reference scenario, use: `If LINK-SYD-MEL-FIBRE-01 goes down completely, what's the blast radius, and is there any other link sharing the same physical conduit?`

Use topology evidence only. Do not substitute ticket narrative, mailbox content, or public web results. Prefer one asset or failure domain per call so the result fits Cowork's sub-30-second tool limit.
