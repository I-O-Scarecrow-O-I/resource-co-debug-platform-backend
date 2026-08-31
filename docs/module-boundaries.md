# Module Boundaries

## A. B/S Backend Foundation

Owns API, workspace, tasks, logs, process execution, cancellation, and frontend-facing state.

## B. Partitioned Debugging and Compilation Adaptation

Owns project parsing, Makefile declared dependencies, actual source dependencies, missing dependency
detection, repair plans, rebuild verification, and basic GDB control.

## C. Multi-Core Multi-Task Scheduling Optimization

Owns FIFO baseline planning, resource-aware planning, DAG-ready task selection, timing comparison,
and scheduler strategy output. It is called by module A as Python functions.

## D. Acceptance Test and Performance Verification

Owns fixed test cases, success-rate calculation, FIFO vs optimized timing records, improvement-rate
calculation, and reproducible test reports.
