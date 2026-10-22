"""Name to class. Adding a sink is one module, one line here, a `contracts/<name>/` folder, a fake, the failure tests and
at least one mutant."""

from __future__ import annotations

from .base import Sink
from .make import MakeSink
from .slack import SlackSink

SINK_CLASSES: dict[str, type[Sink]] = {
    "slack": SlackSink,
    "make": MakeSink,
}


def names() -> list[str]:
    return sorted(SINK_CLASSES)


def build_registry() -> dict[str, Sink]:
    return {name: cls() for name, cls in SINK_CLASSES.items()}


def defaults(name: str) -> dict[str, float]:
    cls = SINK_CLASSES[name]
    return {
        "timeout_s": cls.default_timeout_s,
        "min_interval_s": cls.default_min_interval_s,
        "pause_s": cls.default_pause_s,
        "max_age_s": cls.default_max_age_s,
    }
