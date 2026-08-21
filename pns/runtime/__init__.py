"""Session orchestration layer.

Keep this package initializer deliberately inert. Runtime submodules include
configuration loading and process-level supervisors, so eagerly re-exporting
their names here would make an unrelated import such as
``pns.runtime.scheduler`` initialize the reload boundary as a side effect.
Import public services from their explicit submodules instead.
"""

__all__ = []
