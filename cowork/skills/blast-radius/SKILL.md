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
2. Call `noc_investigate` for dependent services, SLA exposure, alternate paths, and shared-conduit risk.
3. Present confirmed dependencies first, then indirect or potential exposure.
4. Explicitly identify logical redundancy that shares physical infrastructure.
5. State when topology has no matching entity instead of treating an empty result as a connection failure.

Prefer one asset or failure domain per call so the result fits Cowork's sub-30-second tool limit.
