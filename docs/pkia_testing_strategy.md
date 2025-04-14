# PKIA Deliverable: Final Testing Strategy (Python)

**Date:** 2025-04-14
**Author:** Python Knowledge Integration Architect (PKIA)
**Status:** Final Draft

## 1. Introduction

This document defines the comprehensive testing strategy for the Python components of the `wiki2gaz-modern` knowledge integration pipeline. The goal is to ensure code correctness, robustness, maintainability, and confidence in the pipeline's output through a multi-layered testing approach.

## 2. Testing Philosophy

*   **Test Pyramid:** Adhere to the testing pyramid principle, emphasizing a strong foundation of unit tests, a healthy layer of integration tests, and a smaller number of focused end-to-end tests.
*   **Automation:** All tests should be fully automated and executable with a single command (e.g., `poetry run pytest`).
*   **CI/CD Integration:** All tests must pass in the Continuous Integration (CI) pipeline before code is merged or deployed.
*   **Testability:** Code should be written with testability in mind (e.g., dependency injection, clear interfaces, avoiding side effects where possible).

## 3. Testing Levels & Scope

### 3.1. Unit Tests (`tests/unit`)

*   **Goal:** Verify the correctness of individual, isolated units of code (functions, methods, classes) independent of external dependencies like databases or file systems.
*   **Scope:**
    *   Utility functions (`utils/helpers.py`, `utils/config.py`, `utils/logging_config.py`).
    *   Core logic within data extraction functions (`utils/wikidata_helpers.py` - e.g., `extract_coordinates`, `extract_aliases`, individual `extract_complex_property` logic, hierarchy traversal logic with mocked data fetching).
    *   Parsing logic helpers (if any exist outside main scripts).
    *   Logic within specific pipeline script functions if easily isolated (e.g., `apply_class_assignment` in `apply_semantic_corrections.py`).
*   **Techniques:**
    *   Use `pytest` as the test runner.
    *   Use `unittest.mock` (`@patch`, `MagicMock`) extensively to isolate the unit under test from its dependencies (e.g., mock database calls, file I/O, external API calls, helper functions).
    *   Focus on testing various inputs, outputs, edge cases, and potential error conditions for each unit.
    *   Use parameterized tests (`@pytest.mark.parametrize`) for testing multiple scenarios efficiently.
*   **Coverage Target:** Aim for high unit test coverage (e.g., **> 80%**) for critical utility modules and data transformation logic. Use `pytest-cov` to measure coverage.

### 3.2. Integration Tests (`tests/integration`)

*   **Goal:** Verify the interaction and data flow between different components of the system, including interactions with external services (mocked or containerized).
*   **Scope:**
    *   **Script <> Helper Interaction:** Test pipeline scripts' interaction with utility modules (e.g., `ingest_wikidata_mongo.py` calling `utils/mongo_helpers.py`, `process_wiki_stats.py` calling `utils/mapping.py`).
    *   **Database Interaction:** Test interactions with MongoDB and SQLite (Wikimapper DB, temporary stats DB).
    *   **Pipeline Runner <> Scripts:** Test the `run_pipeline.py` script's ability to correctly invoke child scripts and handle basic success/failure reporting.
    *   **Data Flow Between Steps:** Verify that the output of one pipeline step (e.g., `process_wiki_stats.py` creating a SQLite DB) can be correctly consumed by a subsequent step (`enrich_mongo_with_wiki.py`).
*   **Techniques:**
    *   Use `pytest` as the test runner.
    *   **Database Mocking/Containerization:**
        *   Use `mongomock` for *basic* MongoDB interaction tests (e.g., checking if `bulk_write` was called, simple document inserts/finds).
        *   **Strongly Recommended:** Utilize **`testcontainers`** (or similar) to spin up real MongoDB and potentially other required services (like a vector DB if used later) in Docker containers for more realistic integration testing, especially for verifying schema validation, index usage, and complex queries.
        *   Use temporary SQLite files for testing Wikimapper and stats DB interactions.
    *   Focus on testing the contracts and communication between components.
    *   Use small, controlled data fixtures (`tests/fixtures/`) representing realistic inputs/outputs for components.
*   **Coverage Target:** Focus on covering key interaction points and data handoffs rather than line coverage.

### 3.3. End-to-End (E2E) Tests (`tests/e2e` - Potentially run via pipeline runner)

*   **Goal:** Verify that the entire pipeline, as defined in `config/pipeline_config.json`, can execute successfully from start to finish on a small, controlled dataset and produce the expected final state.
*   **Scope:**
    *   Execute the main pipeline runner (`scripts/run_pipeline.py`) targeting a specific, minimal test configuration.
    *   Use tiny, curated input data files (Wikidata JSON snippet, Wikipedia XML snippet, corresponding Wikimapper DB subset) stored in `tests/fixtures/e2e_data/`.
    *   The test should prepare the environment (e.g., potentially clear/set up a test MongoDB database using `testcontainers` if not using `mongomock` globally), run the pipeline, and then validate the final state of the MongoDB collection against expected results.
*   **Techniques:**
    *   Use `pytest` to orchestrate the test setup, execution, and teardown.
    *   Requires careful management of test data and environment configuration.
    *   Focus on verifying the *correctness* of the final integrated output for a few key entities, not performance or exhaustive data validation.
*   **Execution:** These tests might be slower and more resource-intensive. They should run in CI but could potentially be run less frequently than unit/integration tests if necessary.

## 4. Test Data Management (`tests/fixtures/`)

*   Store small, representative test data files under `tests/fixtures/`.
    *   Examples: `wikidata_sample.jsonl`, `wikipedia_snippet.xml`, `sample_mapping.db`, `sample_stats.db`, `class_rules_sample.yaml`.
*   Avoid committing large data files to the repository.
*   Test functions should load required fixtures relative to the test file location or a known fixtures directory.

## 5. Test Execution and CI/CD

*   A single command (`poetry run pytest`) should execute all unit and integration tests.
*   E2E tests might have a separate command or tag (`pytest -m e2e`) if needed.
*   The CI/CD pipeline **must** execute all applicable tests on every commit/pull request.
*   Builds/deployments **must** be blocked if any tests fail.
*   Test coverage reports should be generated and potentially checked against thresholds in CI.

## 6. Continuous Improvement

*   Test suites should evolve alongside the codebase. New features require new tests. Bug fixes should include regression tests.
*   Regularly review test coverage and effectiveness. Identify and address gaps in testing.
*   Refactor tests for clarity and maintainability as needed.

