"""A worker started without a command-line flag must still register every
task a producer enqueues by name — `mlsc.beat`'s Beat projection and
`CeleryDispatcher` are the only two producers, and both name their target
task as a bare string with no import behind it to catch a typo (on-demand-
collection design.md, "Failure strategy": "An unregistered task name —
crash, and prove it cannot recur")."""

from __future__ import annotations

from mlsc.worker import app

_ENQUEUED_TASK_NAMES = {
    "mlsc.run_monitor",  # mlsc/beat.py, MonitorAwareScheduler's projected entry
    "mlsc.dispatch_run",  # mlsc/tasks/celery_dispatcher.py, CeleryDispatcher.dispatch_run
}


def test_every_enqueued_task_name_is_registered() -> None:
    app.loader.import_default_modules()

    assert _ENQUEUED_TASK_NAMES <= set(app.tasks)
