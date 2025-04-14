# Python Coding Standards and Style Guide

**Version:** 1.0
**Date:** 2025-04-14
**Author:** Python Knowledge Integration Architect (PKIA)
**Status:** Enforced

## 1. Introduction

This document defines the coding standards and style guidelines for the Python codebase of the `wiki2gaz-modern` project. The goal is to ensure code is consistent, readable, maintainable, and high-quality. Adherence to these standards is **mandatory** for all contributions.

## 2. Code Formatting (`black`)

*   **Tool:** `black` is the mandatory code formatter.
*   **Configuration:** The default `black` configuration is used (line length 88). Configuration is specified in `pyproject.toml` (`[tool.black]`).
*   **Execution:** Run `poetry run black .` from the project root before committing any code changes.
*   **CI Check:** The CI pipeline includes a step to verify code formatting using `black --check .`. Builds will fail if code is not formatted correctly.

## 3. Linting (`flake8`)

*   **Tool:** `flake8` is mandatory for identifying style issues (PEP 8), programming errors (like unused variables), and complexity issues.
*   **Configuration:** Uses `flake8` defaults initially. Any project-specific deviations or ignored rules must be justified and added to a `.flake8` file or `pyproject.toml` and documented here.
    *   *(Currently using defaults)*
*   **Execution:** Run `poetry run flake8 .` from the project root before committing. Address all reported issues.
*   **CI Check:** The CI pipeline includes a step to run `flake8`. Builds will fail if linting errors are present.

## 4. Type Hinting (`mypy`)

*   **Requirement:** Comprehensive type hints using the `typing` module (PEP 484) are **mandatory** for all new code, including function/method signatures (arguments and return types) and variables where the type is not immediately obvious. Aim to add type hints during refactoring of existing code.
*   **Tool:** `mypy` is used for static type checking to catch type errors before runtime.
*   **Configuration:** Settings are defined in `pyproject.toml` (`[tool.mypy]`). Key settings include:
    *   `python_version = "3.9"`
    *   `warn_return_any = true`
    *   `warn_unused_configs = true`
    *   `ignore_missing_imports = true` (Initially set to `true` for practicality with external libraries, but strive to add stubs or more specific ignores where possible).
    *   *(Consider adding `disallow_untyped_defs = true` later for stricter enforcement)*
*   **Execution:** Run `poetry run mypy scripts/ utils/` (or `.`) regularly during development. Address all reported type errors.
*   **CI Check:** The CI pipeline includes a step to run `mypy`. Builds **must** pass `mypy` checks without errors.

## 5. Naming Conventions (PEP 8)

*   **Variables/Functions/Methods/Modules:** Use `snake_case` (lowercase words separated by underscores). Example: `processed_count`, `normalize_title`, `mongo_helpers.py`.
*   **Constants:** Use `UPPER_SNAKE_CASE`. Example: `MAX_RETRIES = 3`, `P_INSTANCE_OF = "P31"`. Define constants at the module level.
*   **Classes:** Use `PascalCase` (or `CapWords`). Example: `WikimapperLookup`, `StepExecutionError`.
*   **Private/Internal:** Prefix internal helper functions or attributes within modules/classes with a single underscore `_`. Example: `_parse_wikidata_time`. Use double underscore `__` (name mangling) only when strongly needed to avoid subclass collisions.
*   **Clarity:** Choose descriptive names that clearly indicate the purpose of the variable, function, or class. Avoid overly generic names like `data`, `value`, `func` unless the scope is extremely limited and context is clear.

## 6. Documentation

*   **Docstrings (Google Style):**
    *   **Requirement:** Mandatory for all modules, public classes, functions, and methods.
    *   **Format:** Use Google Style Docstrings. This format is readable and parsable by tools like Sphinx.
    *   **Content:**
        *   A concise summary line explaining *what* the unit does.
        *   A more detailed explanation of the *purpose/why* and any complex logic.
        *   `Args:` section detailing each parameter, its type, and description.
        *   `Returns:` section describing the return value and its type.
        *   `Raises:` section documenting any exceptions the unit might explicitly raise.
    *   **Example:** See `docs/pkia_coding_standards.md` in the PKIA deliverables or `utils/helpers.py` for examples.
*   **Comments (`#`):**
    *   Use inline comments sparingly to explain non-obvious logic, algorithmic choices, or workarounds (`# TODO:`, `# FIXME:`).
    *   Do **not** use comments to restate what the code clearly does. Good naming and structure should make the code self-explanatory.
*   **README/Docs:** Keep `README.md` and other `/docs` files up-to-date with significant changes to architecture, setup, or usage.

## 7. Modularity & Structure

*   **Single Responsibility:** Functions/methods should ideally do one thing well. Classes should represent a coherent concept.
*   **Cohesion:** Group related functions/classes into modules (e.g., all MongoDB helpers in `utils/mongo_helpers.py`).
*   **File/Function Length:** Avoid excessively long files or functions. Refactor large units into smaller, more manageable pieces.
*   **Imports:** Use absolute imports (`from utils import helpers`) where possible within the project. Avoid wildcard imports (`from module import *`). Keep imports organized at the top of the file (standard library, third-party, local application).

## 8. Error Handling

*   **Specific Exceptions:** Catch specific exception types (`FileNotFoundError`, `KeyError`, `TypeError`, `pymongo.errors.BulkWriteError`, custom exceptions like `ConfigurationError`) rather than generic `Exception`.
*   **Logging:** Log errors using the `logging` module. Provide context (`logger.error(f"Failed processing item {item_id}: {e}", exc_info=True)`).
*   **Graceful Handling:** For non-critical errors (e.g., failure to parse a single date qualifier), log a warning and continue processing if possible, often returning a default value (`None`, `[]`).
*   **Fail Fast:** For critical errors (invalid config, database connection failure, unrecoverable parsing errors), log a critical error and allow the script/pipeline to terminate with a non-zero exit code.

## 9. Code Review

*   **Process:** All changes submitted via Pull Requests. Require at least one review/approval.
*   **Focus:** Reviewers check for correctness, adherence to standards, test coverage, potential bugs, performance implications, security considerations, and clarity.

By following these standards, we aim to build a robust, maintainable, and understandable knowledge integration pipeline.
