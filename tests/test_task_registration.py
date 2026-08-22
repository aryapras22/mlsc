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
    "mlsc.run_override",  # mlsc/tasks/celery_dispatcher.py, CeleryDispatcher.dispatch_override
    "mlsc.run_alert_delivery",  # mlsc/beat.py, MonitorAwareScheduler's STATIC_SCHEDULE
    "mlsc.run_weekly_discovery",  # mlsc/beat.py, MonitorAwareScheduler's STATIC_SCHEDULE
    "mlsc.run_monthly_refit",  # mlsc/beat.py, MonitorAwareScheduler's STATIC_SCHEDULE
    "mlsc.run_retention_sweep",  # mlsc/beat.py, MonitorAwareScheduler's STATIC_SCHEDULE
    "mlsc.run_stability_snapshot",  # mlsc/beat.py, MonitorAwareScheduler's STATIC_SCHEDULE
}


def test_every_enqueued_task_name_is_registered() -> None:
    app.loader.import_default_modules()

    assert _ENQUEUED_TASK_NAMES <= set(app.tasks)
