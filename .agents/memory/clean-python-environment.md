---
name: Clean Python verification
description: Replit Python environment isolation rules for trustworthy disposable dependency checks
---

A disposable Python environment is only clean when inherited `PYTHONPATH`, `PYTHONHOME`, user-package, and pip target settings are cleared before creating and using the venv.

**Why:** Replit can expose the project’s `.pythonlibs` through inherited environment settings, causing pip to report dependencies as already installed and masking missing requirements.

**How to apply:** For fresh-install verification, unset those variables, disable the user site, isolate pip configuration, and confirm imported dependency paths resolve inside the temporary venv before running the documented command.