"""Operator-only administrative commands (run as `python -m house_helper.admin`).

Nothing here is reachable from the web application or the Telegram bot, and neither
imports this package: it is a tool an administrator runs by hand against a database
they have chosen and migrated themselves. The boundary is pinned by
tests/test_admin_boundary.py.

Importing the package has no effect: no engine, no file, no environment access.
"""
