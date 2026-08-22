"""The real dispatcher: enqueues dispatch_run and run_override through Celery.

Kept in its own module so mlsc/application/runs.py and
mlsc/application/overrides.py never import Celery directly (design.md,
"Dependencies, injected"). One class satisfies both services' dispatcher
protocols structurally.
"""

from __future__ import annotations

import uuid

from mlsc.worker import app


class CeleryDispatcher:
    def dispatch_run(self, run_id: uuid.UUID) -> None:
        app.send_task("mlsc.dispatch_run", args=[str(run_id)], queue="io")

    def dispatch_override(self, job_id: uuid.UUID) -> None:
        app.send_task("mlsc.run_override", args=[str(job_id)], queue="io")
