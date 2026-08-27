"""The real dispatcher: enqueues dispatch_run, run_override and
run_theme_job through Celery.

Kept in its own module so mlsc/application/runs.py, mlsc/application/overrides.py
and mlsc/application/themes.py never import Celery directly (design.md,
"Dependencies, injected"). One class satisfies all three services' dispatcher
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

    def dispatch_theme_job(self, job_id: uuid.UUID) -> None:
        app.send_task("mlsc.run_theme_job", args=[str(job_id)], queue="io")
