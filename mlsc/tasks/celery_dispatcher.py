"""The real TaskDispatcher: enqueues dispatch_run through Celery.

Kept in its own module so mlsc/application/runs.py never imports Celery
directly (design.md, "Dependencies, injected").
"""

from __future__ import annotations

import uuid

from mlsc.worker import app


class CeleryDispatcher:
    def dispatch_run(self, run_id: uuid.UUID) -> None:
        app.send_task("mlsc.dispatch_run", args=[str(run_id)], queue="io")
