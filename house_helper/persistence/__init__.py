"""Durable storage for the web application: users, sessions, conversations.

SQLAlchemy 2.x models, Alembic migrations and the engine/initialisation helpers
live here, and nowhere else: house_helper.core (the chat core) must not import
this package, SQLAlchemy or Alembic, and this package must not import the
Telegram transport or provider configuration (bot, config, api_client). Both
directions are pinned by tests/test_persistence_boundary.py.

SQLite is the development and initial deployment database; the schema is
written to be portable to PostgreSQL (no SQLite-only types or functions, named
constraints within PostgreSQL's 63-character identifier limit).
"""
