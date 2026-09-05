# Backend Module Boundaries

## Platform Layer

The platform layer is shared by all contract backend modules. It owns:

- project and workspace management
- unified task lifecycle
- logs, progress, and cancellation state
- controlled subprocess execution
- API versioning
- backend module registration
- future artifact/report persistence

Contract backend modules must not implement separate task, log, progress, or cancellation systems.
`app/platform/services` contains only the shared runtime base: workspace, task lifecycle, task
storage, logs, and controlled process execution. The current composition root in
`app/platform/api/deps.py` assembles module services with those platform services.

## Contract Backend Modules

The contract backend modules are:

- `code_generation`: code completion, repair, and refactoring backend integration
- `co_debug`: resource-coordinated debugging and optimization
- `vulnerability`: semantic code vulnerability detection
- `risk_warning`: rule-based code risk detection and warning

Current implementation focus is `co_debug`.

## Module 2: `co_debug`

The `co_debug` module owns:

- Makefile hidden dependency analysis and repair
- partitioned compilation adaptation
- GDB/GDB-MI debug session control
- FIFO baseline scheduling
- resource-aware multi-core scheduling
- acceptance metrics for build success rate and scheduling improvement

Its services and module-specific response/plan schemas live under
`app/modules/co_debug/services` and `app/modules/co_debug/schemas`. The `co_debug.scheduler`
submodule is called as ordinary Python functions by the platform integration point. The platform
remains responsible for starting Make/GCC/GDB as controlled subprocesses through `ProcessRunner`.
No generic task-handler or plugin abstraction is introduced until another module needs one.
