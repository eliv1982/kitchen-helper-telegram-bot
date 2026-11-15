"""The migration revisions the test fixtures name (HH-DB-01 CI-08).

A fixture that builds a database must state the revision it is about. Before HH-DB-01
the helpers called `upgrade_database(engine)` and silently got "head"; the historical
tests already passed their revision, and the head fixtures now say so explicitly.
tests/deploy_support.make_head_database is the single SQLite "head" fixture; this module
lets the application fixtures name the same revision without importing the deploy kit.
"""

HEAD_REVISION = "0014_user_measure_overrides"
