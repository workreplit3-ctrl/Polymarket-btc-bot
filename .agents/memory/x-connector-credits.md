---
name: X connector credits
description: Runtime behavior when the connected X API is reachable but its read credits are exhausted.
---

The connected X app-only API can return HTTP 402 with `credits depleted` even when the Replit connection and internal authentication are healthy. Treat this as a temporary data-source outage, not an authentication failure or a reason to reauthorize.

**Why:** A live integration check reached the X proxy successfully and returned 402; retrying authorization would not restore read access.

**How to apply:** Keep X as an optional confirmation factor. On 402, use the neutral fallback, keep real-entry safety gates unchanged, and avoid logging or surfacing raw provider credentials.