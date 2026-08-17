"""Shared pytest configuration and fixtures."""

from __future__ import annotations

import os

# Force test environment before any app imports.
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("LOG_JSON", "false")
