---
name: Paper-run pause safety
description: Operational constraint when collecting paper results while real trading remains paused.
---

The shared orchestrator pause flag stops the strategy loop before execution, including paper trades. A paper validation run that must leave the real pause enabled needs an isolated harness and must preserve and restore the flag rather than deleting it permanently.

**Why:** Removing the flag without a guaranteed restore can accidentally leave real trading unpaused after a test or interrupted run.

**How to apply:** Keep production mode set to paper, run validation against an isolated database/log, guard any temporary flag move with a shell trap/finally block, and verify the flag is present after the run.