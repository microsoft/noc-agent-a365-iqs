---
name: public-advisory
description: |
  Searches public telecom vendor advisories and carrier status reports. Use
  for external NOC-relevant developments, outages, and equipment notices.
license: MIT
metadata:
  author: Microsoft
  version: "1.0"
---

# Public advisory search

## Workflow

1. Extract the vendor, equipment family, corridor, carrier, geography, and time window.
2. Call `noc_investigate` with a narrow request explicitly asking the Web IQ specialist for public sources only.
3. Return source, publication time, affected product or geography, and relevance to the incident.
4. Distinguish confirmed advisories from search-result correlation.

For the reference scenario, use: `Is there any public vendor advisory or carrier status-page report about DWDM equipment issues on the Sydney–Melbourne corridor this week?`

Decline unrelated general-news requests. Do not use private tickets, topology, Teams, email, or Eventhouse evidence in this workflow.
