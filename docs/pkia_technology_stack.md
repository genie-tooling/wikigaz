# Technology Stack Specification (Python Focus)

**Version:** 1.0
**Date:** 2025-04-14
**Author:** Python Knowledge Integration Architect (PKIA)
**Status:** Implemented

## 1. Introduction

This document outlines the key Python libraries, frameworks, and tools selected for the implementation of the `wiki2gaz-modern` pipeline, along with the rationale for their choice. Dependency management is handled via Poetry, with versions specified in `pyproject.toml` and locked in `poetry.lock`.

## 2. Core Libraries & Justification

*   **Dependency Management:**
    *   **`poetry`**: Manages Python dependencies, virtual environments, and packaging. Ensures reproducible builds (`poetry.lock`) and provides a clean project structure. Chosen for its modern approach compared to traditional `pip`/`requirements.txt`.
*   **Runtime:**
    *   **Python 3.9+**: Base language version. Specifically chosen for standard library features like `graphlib` (used in the pipeline runner) and mature type hinting support.
*   **Configuration:**
    *   **`pyyaml`**: Standard library for parsing YAML configuration files (`config.yaml`, `class_mapping_rules.yaml`).
    *   **`python-dotenv`**: Loads environment variables from `.env` files, essential for managing secrets like `MONGO_URI` without committing them.
    *   **`pydash`**: Provides helper functions for safe nested dictionary access (`pydash.get`), simplifying configuration lookups.
*   **Database Interaction:**
    *   **`pymongo`**: The official, mature, and performant driver for interacting with MongoDB. Essential for ingestion, enrichment, and correction steps.
    *   **`sqlite3`** (Standard Library): Used for creating and querying the Wikimapper SQLite database (`utils/mapping.py`) and the temporary intermediate statistics database (`scripts/enrich_mongo_with_wiki.py`). Chosen for its simplicity, file-based nature, and ubiquity.
*   **Data Parsing:**
    *   **`ijson`**: An iterative JSON parser. Critical for processing multi-gigabyte/terabyte Wikidata JSON dumps efficiently without loading the entire file into memory.
    *   **`lxml`** (Preferred) / **`xml.etree.ElementTree`** (Fallback): Used for parsing large Wikipedia XML dumps. `lxml` is generally faster and more feature-rich; `iterparse` is used from either library for streaming to conserve memory. System dependencies like `libxml2` might be needed for `lxml`.
*   **Semantic Embeddings:**
    *   **`sentence-transformers`**: State-of-the-art library for easily downloading and using pre-trained sentence embedding models (like `all-MiniLM-L6-v2`). Simplifies the process of generating vectors.
    *   **`numpy`**: Foundational library for numerical operations, used implicitly by `sentence-transformers` and explicitly for handling embedding arrays before storage.
    *   **`bson`** (from `pymongo`): Used to convert NumPy embedding arrays into the BSON Binary format for efficient storage in MongoDB.
*   **Pipeline Orchestration:**
    *   **`graphlib`** (Standard Library, Python 3.9+): Used by `scripts/run_pipeline.py` for robust topological sorting of pipeline steps based on dependencies.
    *   **`subprocess`** (Standard Library): Used by the runner to execute individual pipeline scripts as separate processes.
    *   **`argparse`** (Standard Library): Used by individual scripts to parse command-line arguments passed by the runner or user.
*   **Utilities:**
    *   **`tqdm`**: Provides progress bars for long-running operations (e.g., dump processing), improving user experience.
    *   **`requests`**: Included for potential future use or download operations (e.g., fetching schema files, though currently uses `wget` in shell scripts).
    *   **`shlex`** (Standard Library): Used by the pipeline runner for safely joining/splitting command parts for execution and logging.

## 3. Development & Testing Stack

*   **Testing Framework:**
    *   **`pytest`**: The standard, powerful, and flexible testing framework for Python. Used for writing and running unit, integration, and potentially E2E tests.
    *   **`pytest-cov`**: `pytest` plugin for measuring test coverage.
*   **Mocking:**
    *   **`unittest.mock`** (Standard Library): Used extensively in unit tests to isolate components by patching dependencies (e.g., database calls, external libraries).
    *   **`mongomock`**: Provides an in-memory mock of MongoDB for basic integration tests, avoiding the need for a running MongoDB instance in simpler test scenarios.
    *   **`(Recommended)` `testcontainers`**: Library to manage real services (like MongoDB) in Docker containers during integration tests, providing higher fidelity testing than mocks.
*   **Code Quality:**
    *   **`black`**: Uncompromising code formatter ensures consistent style across the project.
    *   **`flake8`**: Linter checks for code style issues (PEP 8) and potential errors.
    *   **`mypy`**: Static type checker verifies type hints, catching potential type errors before runtime.

## 4. External Tools (Invoked by Scripts)

*   **`wikimapper`** (CLI): External tool installed separately (`pip install wikimapper`), invoked by `scripts/build_wikimapper_index.sh` to create the SQLite mapping database.
*   **`wget`** (System Tool): Used by `scripts/download_data.sh` for downloading Wikimedia dumps.
*   **`bash`**, **`sha1sum`**, **`sqlite3`** (System Tools): Used by helper shell scripts (`download_data.sh`, `build_wikimapper_index.sh`).

This stack prioritizes standard, well-maintained libraries suitable for data-intensive processing, emphasizing performance (streaming), maintainability (configuration, code quality tools), and robustness (testing).
