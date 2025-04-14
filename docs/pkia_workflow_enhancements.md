# PKIA Deliverable: Workflow Orchestration Enhancements Design

**Date:** 2025-04-14
**Author:** Python Knowledge Integration Architect (PKIA)
**Status:** Final Draft

## 1. Introduction

This document details the required enhancements for the pipeline runner script (`scripts/run_pipeline.py`) to ensure robust execution, dependency management, and error handling for the multi-step knowledge integration pipeline defined in `config/pipeline_config.json`.

## 2. Orchestration Tool

*   **Decision:** Continue using the custom Python script `scripts/run_pipeline.py` as the primary orchestrator for this phase.
*   **Rationale:** The current pipeline complexity does not yet mandate the overhead of introducing external workflow orchestrators like Airflow, Dagster, or Prefect. Enhancing the existing runner provides sufficient control for now. This decision can be revisited if operational requirements (e.g., complex scheduling, UI monitoring, dynamic workflows) increase significantly.

## 3. Dependency Management

*   **Requirement:** Steps must execute only after *all* their declared dependencies (in the `depends_on` list) have successfully completed within the current pipeline run.
*   **Implementation:**
    *   Modify `run_pipeline.py` to perform a **topological sort** of the steps based on the `depends_on` relationships defined in `config/pipeline_config.json` *before* starting execution.
    *   Use a standard graph library (like `graphlib` available in Python 3.9+) or implement a topological sort algorithm (e.g., Kahn's algorithm or DFS-based).
    *   The script should execute the steps in the topologically sorted order.
    *   The existing cycle detection (`_check_dependency_cycles`) should be retained or integrated into the topological sort implementation.
    *   The check for missing dependencies (`missing_deps = dependencies - completed_steps`) remains valid within the loop executing the sorted steps.

## 4. Error Handling Strategy

*   **Default Strategy:** **Fail Fast.** The pipeline run should stop immediately if any step fails (returns a non-zero exit code or encounters an unhandled exception during execution via `subprocess.run`).
*   **Implementation:**
    *   In `run_pipeline.py`, after each call to `run_step`, check the boolean return value.
    *   If `run_step` returns `False`, log a critical error indicating the pipeline halt due to the specific step failure and exit the runner script with a non-zero status code (e.g., `sys.exit(1)`).
    *   The `run_step` function should already capture `stderr` and `stdout` from the subprocess. Ensure that `stderr` from failed steps is logged prominently (e.g., at `ERROR` level).
    *   Wrap the `subprocess.run` call within `run_step` in a `try...except` block to catch potential exceptions during process execution itself (e.g., `FileNotFoundError`, `OSError`) and return `False` if such exceptions occur, logging the error appropriately.

## 5. Parameter Passing

*   **Goal:** Ensure parameters defined in `pipeline_config.json` are passed reliably and robustly to child scripts.
*   **Implementation:**
    *   **Python Scripts (`.py`):**
        *   Continue using command-line arguments (`--key value`) generated from the `parameters` dictionary in `pipeline_config.json`.
        *   The runner should ensure values are converted to strings (`str(value)`) before passing them.
        *   Handle boolean parameters correctly: If a parameter value resolves to `True`, pass the flag (e.g., `--force`); if `False`, omit the flag entirely.
        *   **Enhancement:** While full type validation in the runner is complex, consider adding basic checks within the *child scripts'* argument parsing logic (`argparse`) to define expected types (`type=int`, `type=float`, etc.) for better robustness.
    *   **Shell Scripts (`.sh`):**
        *   **Confirm Preferred Method:** Pass parameters as **environment variables** (e.g., `STEP_PARAM_MY_PARAM="value"`). This is generally more robust than relying on command-line argument parsing within shell scripts, especially for complex values.
        *   The `run_pipeline.py` script must construct the `env` dictionary for `subprocess.run`, adding parameters prefixed with `STEP_PARAM_` (uppercased key) and ensuring values are stringified. The receiving shell script must be written to read these environment variables.
        *   Document this expectation clearly for any shell scripts included in the pipeline.

## 6. Logging Enhancements

*   **Requirement:** Improve clarity in runner logs regarding step execution.
*   **Implementation:**
    *   Ensure clear log messages indicating the start and end of each step, including its success or failure status and duration.
    *   Log the fully resolved command being executed (use `shlex.join` for safe logging).
    *   Ensure stdout/stderr from child processes are captured and logged appropriately (potentially truncating long stdout but always showing full stderr on failure).

## 7. `run_if_missing` Condition

*   **Requirement:** Allow steps to be skipped if a target artifact already exists.
*   **Implementation:** The current implementation within `run_step` (checking `resolved_run_if_missing` path existence) is appropriate. Ensure the log message clearly indicates *why* the step was skipped. This check should happen *before* dependency checks *within the context of deciding whether to execute a specific step*, but the step is still considered "complete" for dependency purposes if skipped via this mechanism.

