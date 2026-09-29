---
title: Instructions for agents
description: What to tell an AI agent that uses InterLock so it behaves well.
sidebar:
  order: 4
---

An agent works better through InterLock when its instructions say how the
gateway behaves. A starting point for a system prompt or project instructions:

```text
Data access goes through InterLock. Use the interlock_* MCP tools.
- Call interlock_describe_access first to learn which sources, tables and
  columns you may use. Do not probe for others.
- A refusal says why. Report it; do not rephrase the query to get around it.
- Values such as [REDACTED:EMAIL] were redacted on purpose. Do not try to
  recover them.
- A write that returns an approval_id was queued for a person to review and
  was NOT executed. Report the approval id and do not retry.
```

Keep API keys out of prompts, repositories and logs. Give each agent deployment
its own identity so its requests are attributable and revocable.
