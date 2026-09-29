"""Procrastinate app reference for background task processing.

The actual Procrastinate `App` is constructed by `procrastinate.contrib.django`
once Django is ready. This module just re-exports it so tasks can do
`from config.procrastinate import app`.

That app is a `DjangoApp` (procrastinate >= 3.9), whose worker closes stale
Django DB connections before and after every task, so a connection that dies
between jobs (RDS restart, idle TCP timeout) is reopened instead of reused.
Register tasks with plain `@app.task`. See #225 and procrastinate#1577.
"""

from procrastinate.contrib.django import app

__all__ = ["app"]
