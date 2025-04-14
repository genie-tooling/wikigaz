#!/usr/bin/env python3
import argparse
import json
import logging
import os
import subprocess
import sys
import time
import shlex  # For safer command joining/splitting
from typing import Dict, Any, List, Set, Optional, Tuple
from datetime import datetime

# graphlib is standard in Python 3.9+
try:
    from graphlib import TopologicalSorter, CycleError
except ImportError:
    print("ERROR: graphlib module not found. Requires Python 3.9+.", file=sys.stderr)
    sys.exit(1)


# Make utils discoverable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Attempt to import utils, handle potential early errors
try:
    from utils.config import (
        load_config,
        get_config,
        pydash_get,
        resolve_value,
        get_base_dir,
        ConfigurationError,
    )
    from utils.logging_config import setup_logging
except ImportError as e:
    # Use basic print for critical early errors as logging might not be set up
    print(f"ERROR: Failed to import utility modules: {e}", file=sys.stderr)
    print(
        "Ensure utils/*.py exist and the script is run correctly relative to the project structure.",
        file=sys.stderr,
    )
    sys.exit(1)
except Exception as e:
    print(
        f"ERROR: An unexpected error occurred during initial imports: {e}",
        file=sys.stderr,
    )
    sys.exit(1)

# Logger setup will happen later in main() after config load
logger = logging.getLogger("PipelineRunner")


# Custom Exception for Step Execution Failure
class StepExecutionError(Exception):
    """Custom exception for errors during pipeline step execution."""

    def __init__(self, step_name: str, message: str, return_code: Optional[int] = None):
        self.step_name = step_name
        self.message = message
        self.return_code = return_code
        super().__init__(
            f"Step '{step_name}' failed: {message}"
            + (f" (Return Code: {return_code})" if return_code is not None else "")
        )


def _build_dependency_graph(steps: List[Dict[str, Any]]) -> Dict[str, Set[str]]:
    """Creates a graph representation for TopologicalSorter."""
    graph: Dict[str, Set[str]] = {}
    step_names: Set[str] = set()

    for i, step in enumerate(steps):
        step_name = step.get("name")
        if not step_name:
            raise ConfigurationError(f"Step at index {i} is missing a required 'name'.")
        if step_name in step_names:
            raise ConfigurationError(
                f"Duplicate step name found: '{step_name}'. Step names must be unique."
            )
        step_names.add(step_name)
        # graphlib expects dependencies: {node: {dep1, dep2}}
        graph[step_name] = set(step.get("depends_on", []))

    # Validate that all dependencies listed actually exist as step names
    all_deps: Set[str] = set()
    for deps in graph.values():
        all_deps.update(deps)

    missing_step_deps = all_deps - step_names
    if missing_step_deps:
        # Find which step(s) referred to the missing dependency
        referencing_steps = []
        for name, deps in graph.items():
            if not deps.isdisjoint(missing_step_deps):
                referencing_steps.append(name)
        raise ConfigurationError(
            f"Step(s) '{', '.join(referencing_steps)}' depend(s) on non-existent step(s): '{', '.join(missing_step_deps)}'"
        )

    return graph


def _get_execution_order(steps: List[Dict[str, Any]]) -> List[str]:
    """Determines the execution order of steps using topological sort."""
    try:
        graph = _build_dependency_graph(steps)
        ts = TopologicalSorter(graph)
        # Static order provides the execution sequence respecting dependencies
        sorted_steps = list(ts.static_order())
        logger.info(f"Determined step execution order: {', '.join(sorted_steps)}")
        return sorted_steps
    except CycleError as e:
        # e.args[1] contains the list of nodes in the cycle
        cycle_str = " -> ".join(e.args[1]) + f" -> {e.args[1][0]}"
        raise ConfigurationError(
            f"Cyclic dependency detected in pipeline steps: {cycle_str}"
        ) from e
    except Exception as e:
        logger.critical(f"Failed to determine step execution order: {e}", exc_info=True)
        raise


def run_step(step: Dict[str, Any], config: Dict[str, Any], base_dir: str) -> None:
    """
    Executes a single pipeline step after resolving parameters and checking conditions.
    Raises StepExecutionError on failure.

    Args:
        step: Dictionary representing the step configuration.
        config: The loaded global configuration dictionary.
        base_dir: The project's root directory.

    Raises:
        StepExecutionError: If the step setup fails or the command execution fails.
    """
    step_name = step.get("name", "Unnamed Step")
    script_path_rel = step.get("script")
    command_val = step.get("command")  # Can be string or list
    parameters = step.get("parameters", {})
    run_if_missing = step.get("run_if_missing")  # Relative or absolute path

    # --- Resolve values from config ---
    try:
        # Use the refined resolve_value from utils.config
        resolved_params = resolve_value(parameters, config)
        resolved_run_if_missing = (
            resolve_value(run_if_missing, config) if run_if_missing else None
        )
        resolved_script_path_rel = (
            resolve_value(script_path_rel, config) if script_path_rel else None
        )
        resolved_command_val = (
            resolve_value(command_val, config) if command_val else None
        )
    except Exception as e:
        raise StepExecutionError(
            step_name, f"Failed to resolve parameters/paths: {e}"
        ) from e

    # --- Check run_if_missing condition ---
    if resolved_run_if_missing:
        if not isinstance(resolved_run_if_missing, str):
            raise StepExecutionError(
                step_name,
                "Invalid 'run_if_missing' value. Must resolve to a string path.",
            )

        check_path = resolved_run_if_missing
        if not os.path.isabs(check_path):
            check_path = os.path.join(base_dir, check_path)

        if os.path.exists(check_path):
            logger.info(
                f"Skipping step '{step_name}' because target '{check_path}' already exists (run_if_missing)."
            )
            return  # Step is considered successful as condition met

    # --- Determine Command and Environment ---
    full_cmd: Optional[List[str]] = None
    script_path_abs: Optional[str] = None
    process_env = os.environ.copy()  # Start with current environment

    if resolved_script_path_rel:
        script_path_abs = resolved_script_path_rel
        if not os.path.isabs(resolved_script_path_rel):
            script_path_abs = os.path.join(base_dir, resolved_script_path_rel)

        if not os.path.exists(script_path_abs):
            raise StepExecutionError(step_name, f"Script not found: {script_path_abs}")

        if script_path_abs.endswith(".py"):
            cmd_list = [sys.executable, script_path_abs]
            if isinstance(resolved_params, dict):
                for key, value in resolved_params.items():
                    arg_key = f"--{key.replace('_', '-')}"
                    if isinstance(value, bool):
                        if value:
                            cmd_list.append(arg_key)
                        # else: omit flag if False
                    elif value is not None:
                        cmd_list.append(arg_key)
                        cmd_list.append(str(value))
            else:
                logger.warning(
                    f"Parameters for Python script '{step_name}' are not a dictionary. Passing ignored."
                )
            full_cmd = cmd_list

        elif script_path_abs.endswith(".sh"):
            # Prepare environment variables for shell script
            if isinstance(resolved_params, dict):
                logger.debug(
                    f"Passing parameters as environment variables for shell script '{step_name}':"
                )
                for key, value in resolved_params.items():
                    env_key = f"STEP_PARAM_{key.upper()}"
                    env_value = str(value if value is not None else "")
                    process_env[env_key] = env_value
                    # Be careful logging sensitive values
                    logger.debug(
                        f"  {env_key}=<value_set>"
                    )  # Avoid logging value directly
            cmd_list = ["/bin/bash", script_path_abs]
            full_cmd = cmd_list
        else:
            raise StepExecutionError(
                step_name, f"Unsupported script type: {script_path_abs}"
            )

    elif resolved_command_val:
        if isinstance(resolved_command_val, str):
            try:
                full_cmd = shlex.split(resolved_command_val)
            except ValueError as e:
                raise StepExecutionError(
                    step_name, f"Error splitting command string: {e}"
                )
        elif isinstance(resolved_command_val, list):
            full_cmd = [
                str(item) for item in resolved_command_val
            ]  # Ensure all elements are strings
        else:
            raise StepExecutionError(
                step_name, "Invalid 'command' format. Must resolve to string or list."
            )
    else:
        raise StepExecutionError(step_name, "No 'script' or 'command' defined.")

    if not full_cmd:  # Should be caught above, but safeguard
        raise StepExecutionError(step_name, "Could not determine command to run.")

    # --- Execute Command ---
    logger.info(f"--- Running Step: {step_name} ---")
    logger.info(f"Executing: {shlex.join(full_cmd)}")
    if (
        process_env != os.environ
    ):  # Log if environment differs significantly (e.g., for shell scripts)
        logger.debug(
            "Running with modified environment (parameters passed as STEP_PARAM_...)."
        )

    start_time = time.monotonic()
    try:
        process = subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
            check=False,  # Manually check return code
            encoding="utf-8",
            errors="replace",
            env=process_env,
            cwd=base_dir,
        )
        end_time = time.monotonic()
        duration = end_time - start_time
        logger.info(
            f"Step '{step_name}' finished in {duration:.2f} seconds with return code {process.returncode}."
        )

        # Log stdout/stderr (Log stderr more prominently on failure)
        log_level_stderr = logging.ERROR if process.returncode != 0 else logging.WARNING
        if process.stdout and process.stdout.strip():
            logger.debug(f"Step '{step_name}' STDOUT:\n{process.stdout.strip()}")
        if process.stderr and process.stderr.strip():
            logger.log(
                log_level_stderr,
                f"Step '{step_name}' STDERR:\n{process.stderr.strip()}",
            )

        # Check return code and raise error if failed
        if process.returncode != 0:
            raise StepExecutionError(
                step_name, f"Process failed", return_code=process.returncode
            )

        logger.info(f"Step '{step_name}' completed successfully.")

    except FileNotFoundError:
        raise StepExecutionError(
            step_name, f"Command or script not found: {full_cmd[0]}"
        )
    except OSError as e:
        raise StepExecutionError(
            step_name, f"OS error running command '{full_cmd[0]}': {e}"
        ) from e
    except StepExecutionError:  # Re-raise failures from the process itself
        raise
    except Exception as e:
        # Catch unexpected errors during subprocess execution
        end_time = time.monotonic()
        duration = end_time - start_time
        logger.error(
            f"Step '{step_name}' encountered an unexpected error after {duration:.2f} seconds.",
            exc_info=True,
        )
        raise StepExecutionError(step_name, f"Unexpected error: {e}") from e


def main():
    """Parses arguments and runs the pipeline."""
    parser = argparse.ArgumentParser(
        description="Run the wiki2gaz modernization pipeline defined in a JSON config.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--pipeline",
        default="config/pipeline_config.json",
        help="Path to the pipeline JSON configuration file.",
    )
    parser.add_argument(
        "--config",
        default="config/config.yaml",
        help="Path to the global YAML configuration file.",
    )
    parser.add_argument(
        "--start-at",
        default=None,
        help="Start execution from this step name (inclusive).",
    )
    parser.add_argument(
        "--stop-after",
        default=None,
        help="Stop execution after completing this step name (inclusive).",
    )
    parser.add_argument(
        "--run-steps",
        nargs="+",
        default=None,
        help="Only run specific step names (dependencies are NOT checked).",
    )
    parser.add_argument(
        "--list-steps",
        action="store_true",
        help="List the steps in execution order and exit.",
    )

    args = parser.parse_args()

    # Determine project base directory early
    project_base_dir: Optional[str] = None
    try:
        project_base_dir = get_base_dir()
    except Exception as e:
        print(
            f"ERROR: Failed to determine project base directory: {e}", file=sys.stderr
        )
        sys.exit(1)

    # --- Load Global Config and Setup Logging ---
    global_config: Optional[Dict[str, Any]] = None
    try:
        # Load config first to get logging settings
        global_config = load_config(config_path=args.config, base_dir=project_base_dir)
        # Setup logging using the loaded config
        setup_logging(global_config)
        # Now logger is configured
    except ConfigurationError as e:
        # Logging might not be fully set up, use print for critical config errors
        print(f"CRITICAL: Configuration Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        # Catch-all for other setup errors
        print(f"CRITICAL: Failed during initial setup: {e}", file=sys.stderr)
        # Optionally log to basic config if possible
        try:
            logging.exception("Critical setup failure")
        except Exception:
            pass
        sys.exit(1)

    # --- Load Pipeline Config ---
    pipeline_config: Optional[Dict[str, Any]] = None
    try:
        pipeline_config_path = args.pipeline
        if not os.path.isabs(pipeline_config_path):
            pipeline_config_path = os.path.join(project_base_dir, pipeline_config_path)

        if not os.path.exists(pipeline_config_path):
            raise FileNotFoundError(
                f"Pipeline configuration file not found: {pipeline_config_path}"
            )

        with open(pipeline_config_path, "r", encoding="utf-8") as f:
            pipeline_config = json.load(f)
        logger.info(f"Loaded pipeline configuration from {pipeline_config_path}")

    except FileNotFoundError as e:
        logger.critical(str(e))
        sys.exit(1)
    except json.JSONDecodeError as e:
        logger.critical(
            f"Error parsing pipeline configuration file {pipeline_config_path}: {e}"
        )
        sys.exit(1)
    except Exception as e:
        logger.critical(
            f"An unexpected error occurred loading pipeline config: {e}", exc_info=True
        )
        sys.exit(1)

    # --- Determine Execution Order and List Steps if Requested ---
    steps = pipeline_config.get("steps", [])
    if not isinstance(steps, list):
        logger.critical(
            "Pipeline configuration error: 'steps' key must contain a list."
        )
        sys.exit(1)

    steps_map = {
        step.get("name"): step for step in steps if step.get("name")
    }  # For quick lookup
    execution_order: List[str] = []
    try:
        execution_order = _get_execution_order(steps)
    except ConfigurationError as e:
        logger.critical(f"Pipeline configuration error: {e}")
        sys.exit(1)
    except Exception as e:  # Catch unexpected errors during sort/validation
        logger.critical(f"Failed to process step dependencies: {e}", exc_info=True)
        sys.exit(1)

    if args.list_steps:
        print("Pipeline Steps (Execution Order):")
        for i, name in enumerate(execution_order):
            step_config = steps_map.get(name, {})
            deps = step_config.get("depends_on", [])
            dep_str = f" (depends on: {', '.join(sorted(list(deps)))})" if deps else ""
            print(f"  {i+1}. {name}{dep_str}")
        sys.exit(0)

    # --- Pipeline Execution Logic ---
    completed_steps: Set[str] = set()
    start_index = 0
    stop_index = len(execution_order)  # Exclusive index

    # --- Apply Filters (Start/Stop/Specific) ---
    if args.start_at:
        if args.start_at not in steps_map:
            logger.error(f"Start step '{args.start_at}' not found in pipeline.")
            sys.exit(1)
        try:
            start_index = execution_order.index(args.start_at)
        except ValueError:
            logger.error(
                f"Start step '{args.start_at}' not reachable with current dependencies."
            )
            sys.exit(1)

    if args.stop_after:
        if args.stop_after not in steps_map:
            logger.error(f"Stop step '{args.stop_after}' not found in pipeline.")
            sys.exit(1)
        try:
            stop_index = execution_order.index(args.stop_after) + 1
        except ValueError:
            logger.error(
                f"Stop step '{args.stop_after}' not reachable with current dependencies."
            )
            sys.exit(1)

    run_only_specific_steps = set(args.run_steps) if args.run_steps else None

    pipeline_start_time = datetime.now()
    logger.info(
        f"=== Pipeline '{pipeline_config.get('pipeline_name', 'Unnamed')}' Started at {pipeline_start_time.strftime('%Y-%m-%d %H:%M:%S')} ==="
    )
    logger.info(f"Project base directory: {project_base_dir}")

    overall_success = True
    executed_steps_count = 0
    skipped_due_to_filter = 0
    skipped_due_to_disable = 0

    # --- Execute Steps in Order ---
    for i, step_name in enumerate(execution_order):
        step_config = steps_map.get(step_name)
        if not step_config:  # Should not happen if validation passed
            logger.error(
                f"Internal error: Step config not found for '{step_name}'. Skipping."
            )
            continue

        # --- Step Filtering (Start/Stop/Specific) ---
        if i < start_index:
            logger.info(
                f"Skipping step '{step_name}' (index {i}) due to --start-at '{args.start_at}'."
            )
            skipped_due_to_filter += 1
            completed_steps.add(
                step_name
            )  # Treat as completed for subsequent dependencies
            continue
        if i >= stop_index:
            logger.info(
                f"Skipping step '{step_name}' (index {i}) due to --stop-after '{args.stop_after}'."
            )
            skipped_due_to_filter += 1
            # Do not mark as completed if stopped after previous step
            continue
        if run_only_specific_steps and step_name not in run_only_specific_steps:
            logger.info(
                f"Skipping step '{step_name}' because it's not in --run-steps list."
            )
            skipped_due_to_filter += 1
            completed_steps.add(
                step_name
            )  # Mark as completed for potential dependencies within the filtered set
            continue

        # --- Evaluate 'enabled' flag ---
        enabled_value = step_config.get("enabled", True)
        try:
            resolved_enabled = resolve_value(enabled_value, global_config)
            if str(resolved_enabled).lower() not in ["true", "1", "yes"]:
                logger.info(
                    f"Skipping step '{step_name}' because it is configured as disabled ({enabled_value} -> {resolved_enabled})."
                )
                completed_steps.add(step_name)  # Mark as completed for dependencies
                skipped_due_to_disable += 1
                continue
        except Exception as e:
            logger.error(
                f"Error resolving 'enabled' flag for step '{step_name}': {e}. Stopping pipeline.",
                exc_info=True,
            )
            overall_success = False
            break

        # --- Check Dependencies (Crucial - run even if specific steps selected) ---
        # This ensures that even if running a subset, the required predecessors *within that subset* ran.
        # If --run-steps is used, this logic might prevent execution if a dependency wasn't included in the --run-steps list.
        # The user must provide a valid subset including dependencies when using --run-steps.
        dependencies = set(step_config.get("depends_on", []))
        missing_deps = dependencies - completed_steps
        if missing_deps:
            logger.error(
                f"Cannot run step '{step_name}'. Missing dependencies that should have completed: {', '.join(sorted(list(missing_deps)))}"
            )
            overall_success = False
            break  # Stop pipeline if dependency is missing

        # --- Run the step ---
        try:
            run_step(step_config, global_config, project_base_dir)
            completed_steps.add(step_name)  # Mark as completed only on success
            executed_steps_count += 1
        except StepExecutionError as e:
            logger.error(
                f"Pipeline execution halted due to failure in step '{e.step_name}'. Reason: {e.message}"
                + (f" (RC: {e.return_code})" if e.return_code is not None else "")
            )
            overall_success = False
            break  # Stop pipeline on failure
        except Exception as e:  # Catch unexpected errors from run_step caller
            logger.critical(
                f"Pipeline runner encountered an unexpected error while handling step '{step_name}': {e}",
                exc_info=True,
            )
            overall_success = False
            break

    # --- Pipeline Completion Summary ---
    pipeline_end_time = datetime.now()
    pipeline_duration = pipeline_end_time - pipeline_start_time
    logger.info(
        f"=== Pipeline Finished at {pipeline_end_time.strftime('%Y-%m-%d %H:%M:%S')} (Duration: {pipeline_duration}) ==="
    )
    logger.info(f"Steps executed: {executed_steps_count}")
    logger.info(f"Steps skipped (filter): {skipped_due_to_filter}")
    logger.info(f"Steps skipped (disabled): {skipped_due_to_disable}")

    if overall_success:
        logger.info("Pipeline completed successfully.")
        sys.exit(0)
    else:
        logger.error("Pipeline finished with errors.")
        sys.exit(1)


if __name__ == "__main__":
    main()
