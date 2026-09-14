---
name: Telegram polling exclusivity
description: Operational constraint for running the Telegram control bot.
---

Telegram long polling is exclusive per bot token. A second live process causes `Conflict: terminated by other getUpdates request`, even when the local process itself is healthy.

**Why:** The API workflow can be running correctly while another local, deployed, or manually started copy owns the polling connection.

**How to apply:** Before diagnosing bot code, inspect active processes and stop duplicate runners; do not rotate credentials or change polling logic for this conflict.