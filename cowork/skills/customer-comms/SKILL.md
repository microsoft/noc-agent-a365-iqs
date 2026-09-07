---
name: customer-comms
description: |
  Drafts grounded customer and stakeholder incident updates. Use when the user
  asks for an initial notification, status update, executive summary, or
  resolution communication about a network incident.
license: MIT
metadata:
  author: Microsoft
  version: "1.0"
---

# Customer communications

## Workflow

1. Call `noc_investigate` for confirmed impact, timing, mitigation status, SLA exposure, and the next update point.
2. Distinguish confirmed facts from estimates and omit unsupported root-cause claims.
3. Draft concise text with: status, customer impact, actions under way, workaround if known, and next update time.
4. Avoid internal-only topology, personal data, raw bridge chatter, and speculative blame.

The connector only investigates and drafts. It does not send messages or change incident state.
