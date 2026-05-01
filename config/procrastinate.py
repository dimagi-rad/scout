"""Procrastinate app reference for background task processing.

The actual Procrastinate `App` is constructed by `procrastinate.contrib.django`
once Django is ready. This module just re-exports it so tasks can do
`from config.procrastinate import app`.
"""

from procrastinate.contrib.django import app

__all__ = ["app"]
