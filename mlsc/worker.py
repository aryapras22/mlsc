"""Celery application and foundation scheduling-signal consumer only."""

from __future__ import annotations

from celery import Celery

app = Celery("mlsc")
app.conf.task_default_queue = "io"

