---
name: on-call-bridge
description: |
  Retrieves the caller's on-call roster and current incident-bridge context.
  Use for Teams, chat, meeting, mailbox, and responder coordination questions.
license: MIT
metadata:
  author: Microsoft
  version: "1.0"
---

# On-call and incident bridge

## Workflow

1. Identify the incident, team, bridge, and relevant time window.
2. Call `noc_investigate` with a narrow request explicitly asking the Work IQ specialist for the caller's own roster and collaboration context.
3. Report who is on call, current discussion, decisions, owners, and unresolved questions, with source boundaries.
4. If first-use OAuth consent is requested, ask the user to complete it and retry rather than substituting another source.

For the reference scenario, use: `Who's on-call right now, and what's being discussed on the current incident bridge?`

Do not expose another user's private context, invent attendance, or claim a message was sent.
