"""Celery application and its task registration.

`imports` names every module that registers a task, so a worker started
without a command-line flag still registers everything — a task set that
depends on `--include` is one that silently shrinks the next time someone
starts a worker differently (on-demand-collection design.md, "Where the task
lives and how it gets its dependencies").
"""

from __future__ import annotations

from celery import Celery

app = Celery("mlsc")
app.conf.task_default_queue = "io"
app.conf.imports = ("mlsc.tasks.scheduled",)

