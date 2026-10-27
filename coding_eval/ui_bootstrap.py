"""Refresh presentation dependencies before importing their consumers."""

from importlib import import_module, reload
from pathlib import Path
from threading import RLock

_LOCK = RLock()


def load_ui_modules():
    """Keep worker resources/backend classes intact across concurrent reruns."""
    order = (
        "coding_eval.ui_queries", "app.product_style", "app.dataset_page", "app.model_page",
        "coding_eval.report_views", "app.report_charts", "app.report_page", "app.run_page",
    )
    modules, changed = {}, set()
    dependencies = {
        "app.report_charts": ("coding_eval.report_views",),
        "app.report_page": ("coding_eval.report_views", "app.report_charts"),
        "app.run_page": ("coding_eval.ui_queries",),
    }
    with _LOCK:
        for name in order:
            module = import_module(name)
            stamp = Path(module.__file__).stat().st_mtime_ns
            force = any(dep in changed for dep in dependencies.get(name, ()))
            if name == "coding_eval.report_views":
                force |= not hasattr(module, "difficulty_rows")
            if name == "app.run_page":
                force |= getattr(module, "PLAN_PREVIEW_REVISION", None) != "ready-snapshot-v1"
            if force or getattr(module, "_PRODUCT_LOADED_MTIME", None) != stamp:
                registry = getattr(module, "worker_registry", None)
                reload(module)
                if registry is not None:
                    module.worker_registry = registry
                module._PRODUCT_LOADED_MTIME = stamp
                changed.add(name)
            modules[name] = module
    return modules
