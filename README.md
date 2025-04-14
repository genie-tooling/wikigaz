# wiki2gaz-modern: Modernized Wikipedia/Wikidata Knowledge Integration Pipeline

**Version:** 1.1 (Post Phase 4 Implementation & Documentation Expansion)
**Date:** 2025-04-14

This project implements a modernized pipeline for ingesting Wikidata entities, enriching them with signals derived from Wikipedia (such as link counts and mention text), applying semantic corrections, optionally generating vector embeddings, and storing the final results in MongoDB. It is designed by the Knowledge Graph Architect & Modernization Strategist to create a rich gazetteer suitable for downstream tasks like Retrieval-Augmented Generation (RAG), addressing limitations found in previous systems and incorporating resilience to data evolution.

This pipeline prioritizes streaming processing for large data dumps, robust configuration management, code quality, maintainability, and comprehensive testing.
--------------

## Features

*   **Wikidata Ingestion:** Streams large Wikidata JSON dumps (using `ijson`) and extracts configurable properties, handling complex data types and structures (`scripts/ingest_wikidata_mongo.py`).
*   **Wikipedia Processing:** Streams large Wikipedia XML dumps (using `lxml`/`etree.iterparse`) to aggregate link statistics (counts and mention text frequency) efficiently (`scripts/process_wiki_stats.py`). **Note:** Uses in-memory aggregation; memory usage monitoring is advised (See `docs/design_considerations.md`).
*   **Wikimapper Integration:** Uses Wikimapper (via `utils/mapping.py`) for efficient Wikipedia title -> Wikidata QID lookup, crucial for linking Wikipedia stats to Wikidata entities. Index build automated via `scripts/build_wikimapper_index.sh`.
*   **Configurable Extraction:** Extracts specified Wikidata properties, including complex ones with qualifiers (e.g., P17 country history using P580/P582, P1082 population using P585, P2046 area with unit conversion, P571/P576 dates) via `utils/wikidata_helpers.extract_complex_property`.
*   **Hierarchy Traversal:** Implements P131 administrative hierarchy traversal with an efficient **Batch Prefetching Strategy** to minimize database lookups (`utils/wikidata_helpers.extract_admin_hierarchy`).
*   **Canonical Normalization:** Consistently normalizes Wikipedia titles according to specification using `utils/helpers.normalize_wikipedia_title` throughout the pipeline.
*   **Semantic Corrections:**
    *   Applies class assignment (`corrected_class` field) based on P31 values and configurable rules defined by the Data Architect in `config/class_mapping_rules.yaml` (`scripts/apply_semantic_corrections.py`).
    *   Includes logic to mitigate specific P131 hierarchy ontology flaws identified during analysis (`scripts/fix_ontology_mongo.py`, see `docs/ontology_flaw.md`).
*   **MongoDB Storage:** Stores processed entities in MongoDB with schema validation defined in `config/mongo_schema_v1.json`. Uses efficient batch writes (`bulk_write`) throughout. Schema setup automated via `scripts/setup_mongodb.py`.
*   **Embedding Generation (Optional):** Generates semantic vector embeddings using `sentence-transformers` (`all-MiniLM-L6-v2` default) based on entity labels, descriptions, and aliases. Stores embeddings efficiently as BSON Binary data in MongoDB (`scripts/generate_embeddings.py`). Enabled via `embeddings.enabled` flag in `config.yaml`.
*   **Configurable Pipeline:** Uses YAML (`config/config.yaml`) and `.env` for flexible configuration (supporting `${VAR:-default}` and `${config.path.key}` resolution). Pipeline flow, dependencies, and parameters are defined in `config/pipeline_config.json` and orchestrated by `scripts/run_pipeline.py` which supports dependency management (topological sort) and robust error handling.
*   **Code Quality:** Enforces type hinting (`mypy`), code formatting (`black`), and linting (`flake8`).
*   **Testing:** Includes unit (`tests/unit`) and integration (`tests/integration`) tests using `pytest`, with integration tests leveraging `mongomock` (basic) and recommending `testcontainers` for full validation (see `docs/pkia_testing_strategy.md`).
*   **Comprehensive Documentation:** Includes detailed design documents, data handling strategies, rationale, and operational guides in the `/docs` directory.

## Architecture Overview

The pipeline orchestrates several specialized Python scripts to process and integrate data from Wikidata and Wikipedia into MongoDB.

**Core Components (`scripts/`):**

*   `download_data.sh`: Downloads required Wikidata/Wikipedia dumps.
*   `build_wikimapper_index.sh`: Builds the SQLite DB for title->QID mapping (if Wiki processing enabled).
*   `setup_mongodb.py`: Applies schema validation and creates indexes in MongoDB.
*   `ingest_wikidata_mongo.py`: Streams Wikidata JSON, filters items, extracts data (incl. hierarchy), and inserts/updates MongoDB.
*   `process_wiki_stats.py`: Streams Wikipedia XML, extracts links, uses Wikimapper to map to QIDs, aggregates stats (inlinks, mentions), saves to SQLite/JSONL.
*   `enrich_mongo_with_wiki.py`: Loads stats from SQLite/JSONL, updates MongoDB documents with Wikipedia signals (`wiki_links`).
*   `apply_semantic_corrections.py`: Applies configured class assignment logic.
*   `fix_ontology_mongo.py`: Applies specific P131 hierarchy corrections.
*   `generate_embeddings.py`: (Optional) Generates and stores vector embeddings.
*   `run_pipeline.py`: Orchestrator script that reads `config/pipeline_config.json`, resolves dependencies, and executes the above steps in order.

**High-Level Data Flow:**

```
                     +-------------------------+      +--------------------------+
                     | Wikidata JSON Dump      |      | Wikipedia XML/SQL Dumps  |
                     | (latest-all.json.bz2)   |      | (pages-articles, SQLs)   |
                     +-------------------------+      +--------------------------+
                               |                                |         |
      (download_data.sh)       |                                |         | (download_data.sh)
                               V                                V         V
                     +-------------------------+      +--------------------------+
                     |   scripts/ingest_...    |----->| MongoDB Collection       |<---+----(scripts/setup_mongodb.py)
                     +-------------------------+      | (wikidata_entities_v1)   |    |
                               |                      +-------------^------------+    | (Apply Schema/Indexes)
 (Hierarchy Prefetch --------->+<--------------------------)         |                |
  Lookup from Mongo)                                                  |                |
                                                                      | (Stats Lookup) |
                               +--------------------------+           |                |
                               | scripts/build_wikimapper |<----------+                |
                               +--------------------------+                            |
                                          |                                            |
                     +--------------------------+           +--------------------------+
                     |   Wikimapper SQLite DB   |<----------| scripts/process_wiki...  |
                     | (index_enwiki-latest.db)|           +--------------------------+
                     +--------------------------+                      |
                               |                                       V
                               |                         +-----------------------------+
                               +------------------------>| Stats Output (SQLite/JSONL) |
                                                         +-------------^---------------+
                                                                       |
                      +----------------------------+                   |
                      | scripts/enrich_mongo...  |-------------------+
                      +------------^---------------+
                                   | (Update Mongo with wiki_links)
                                   V
                      +----------------------------+
                      | scripts/apply_semantic...  | --(Read Rules)--> [class_mapping_rules.yaml]
                      +------------^---------------+
                                   | (Update Mongo corrected_class)
                                   V
                      +----------------------------+
                      | scripts/fix_ontology...    | --(Read Rules)--> [config.yaml:ontology_fix]
                      +------------^---------------+
                                   | (Update Mongo admin_hierarchy, flag)
                                   V
                      +----------------------------+
                      | scripts/generate_embed...  | (Optional)
                      +------------^---------------+
                                   | (Update Mongo embedding_vector)
                                   V
                           [ Final MongoDB Data ]

```

Utility functions (`utils/`) provide support for configuration, logging, database interaction, data extraction/normalization, and mapping. The pipeline execution is configured via `config/config.yaml`, `.env`, and `config/pipeline_config.json`.

For detailed architecture and design documents, please refer to the `docs/` directory, particularly `docs/pkia_architecture.md` (Placeholder), `docs/design_considerations.md`, and `docs/data_modeling_rationale.md`.

## Prerequisites

*   **Python:** Version 3.9+ (required for `graphlib` used in runner, see `pyproject.toml`).
*   **Poetry:** For dependency management and running scripts (`pip install poetry`).
*   **Git:** For cloning the repository.
*   **MongoDB:** Access to a running MongoDB instance (v4.0+ recommended for `$jsonSchema` features). Connection URI needs to be configured via `.env`.
*   **Data Dumps:** Required data dumps must be downloaded to the configured data directory (`paths.data_dir`). Use the helper script:
    *   Wikidata JSON dump (e.g., `latest-all.json.bz2`)
    *   Wikipedia XML dump (e.g., `enwiki-latest-pages-articles-multistream.xml.bz2`)
    *   Wikipedia SQL dumps (required by Wikimapper if Wikipedia processing is enabled): `page.sql.gz`, `redirect.sql.gz`, `page_props.sql.gz`.
    *   See `scripts/download_data.sh`.
*   **System Tools:** `wget`, `sha1sum`, `bash`, `sqlite3` (usually standard on Linux/macOS) required by helper scripts like `download_data.sh` and `build_wikimapper_index.sh`.
*   **Wikimapper CLI (Conditional):** The `wikimapper` command-line tool needs to be installed (`pip install wikimapper`) if building the index using `scripts/build_wikimapper_index.sh` (required if `wikipedia_processing.enabled` is true).

## Setup Instructions

1.  **Clone the Repository:**
    ```bash
    git clone <repository-url>
    cd wiki2gaz-modern
    ```

2.  **Install Poetry:**
    Follow the official Poetry installation guide: [https://python-poetry.org/docs/#installation](https://python-poetry.org/docs/#installation)

3.  **Install Dependencies:**
    This will install all required Python packages defined in `pyproject.toml` into a virtual environment managed by Poetry.
    ```bash
    poetry install
    ```
    *(Note: This installs runtime and development dependencies like `pytest` and `black`)*.

4.  **Configure Environment (`.env`):**
    *   Create a `.env` file in the project root directory (where `pyproject.toml` resides).
    *   Add your MongoDB connection string. **This is essential.**
        ```dotenv
        # .env (Example)
        MONGO_URI=mongodb://myuser:mypassword@my_mongo_host:27017/wiki2gaz_db_modern_v1?authSource=admin
        ```
        (Adjust the URI according to your MongoDB setup).
    *   *Optional:* Override other configuration defaults via environment variables if needed (e.g., `WIKIDATA_DUMP_FILENAME`, `WIKIPEDIA_DUMP_FILENAME`). See `config/config.yaml` for `${VAR}` references.

5.  **Download Data Dumps:**
    *   Review `config/config.yaml` (`paths.data_dir`, `wikidata_ingestion.dump_filename`, `wikipedia_processing.dump_filename`) to ensure they point to the desired dump versions. You can override filenames using environment variables (e.g., `export WIKIDATA_DUMP_FILENAME=...`).
    *   Run the download script:
        ```bash
        bash scripts/download_data.sh
        ```
    *   This will download required dumps to the directory specified by `paths.data_dir` (default: `./data/`). **Verify downloads and checksums** reported by the script. This can take significant time and disk space.

6.  **Build Wikimapper Index (Conditional):**
    *   This step is **only required if `wikipedia_processing.enabled` is `true`** in `config/config.yaml`.
    *   Ensure the `wikimapper` command is installed (`pip install wikimapper`).
    *   Run the build script:
        ```bash
        bash scripts/build_wikimapper_index.sh
        ```
    *   This creates the SQLite database specified by `paths.wikimapper_db` (default: `./data/index_enwiki-latest.db`) using the downloaded SQL dumps. This can take a **very long time** and significant disk space.

## Configuration

The pipeline behavior is primarily controlled by configuration files:

*   **`config/config.yaml`:** Main configuration file. Defines paths, MongoDB connection details (can be overridden by `.env`), feature flags (`enabled` sections), processing parameters (batch sizes, properties to extract), embedding settings, ontology fix rules, etc. Supports variable substitution:
    *   `${VAR}` or `${VAR:-default}` for environment variables.
    *   `${config.section.key}` for references to other config values.
    *   Relative paths under `paths` and `logging` are automatically made absolute relative to the project root during loading.
*   **`.env`:** Used for sensitive information (like `MONGO_URI`) and environment-specific overrides. Loaded automatically by `utils/config.py`.
*   **`config/pipeline_config.json`:** Defines the sequence, dependencies (`depends_on`), enablement (`enabled`), and parameters for each step executed by the pipeline runner (`scripts/run_pipeline.py`). References values from `config.yaml` using `${config.section.key}` syntax.
*   **`config/class_mapping_rules.yaml`:** (**Requires DA Input/Review**) Defines the preference order or hierarchy for the `most_specific_known` class assignment strategy used in `scripts/apply_semantic_corrections.py`. **Must be reviewed and potentially populated by the Data Architect based on target entity types.**
*   **`config/mongo_schema_v1.json`:** Defines the target MongoDB `$jsonSchema` used for validation by `scripts/setup_mongodb.py`.

## Usage Guide

The primary way to run the pipeline is using the orchestrator script `scripts/run_pipeline.py`. This script loads the configuration, loads the pipeline definition (`config/pipeline_config.json`), determines the correct execution order via topological sort based on `depends_on` fields, checks if steps are enabled, and executes them sequentially using `subprocess.run`, passing parameters appropriately.

**Running the Full Pipeline:**

Execute the runner script using Poetry from the project root directory:

```bash
poetry run python scripts/run_pipeline.py
```

This command will:
1.  Load configuration from `config/config.yaml` and `.env`.
2.  Load the pipeline definition from `config/pipeline_config.json`.
3.  Determine the execution order based on dependencies.
4.  Execute each enabled step in the calculated order.
5.  Log output to the console and potentially a file (configured in `config.yaml`).
6.  **Stop immediately if any step fails.**

**Common Runner Options:**

*   **Specify Config Files:**
    ```bash
    poetry run python scripts/run_pipeline.py --config my_custom_config.yaml --pipeline my_custom_pipeline.json
    ```
*   **List Steps:** See the steps and their execution order without running them:
    ```bash
    poetry run python scripts/run_pipeline.py --list-steps
    ```
*   **Run Specific Steps:** Execute only one or more named steps. **Caution:** Dependencies are *not* automatically run or checked beyond the specified set; ensure the required state exists (e.g., MongoDB setup, Wikimapper index built) before running dependent steps individually.
    ```bash
    poetry run python scripts/run_pipeline.py --run-steps "Ingest Wikidata" "Apply Semantic Corrections"
    ```
*   **Start/Stop Control:** Run a portion of the pipeline based on the determined execution order:
    ```bash
    # Start from 'Process Wikipedia Stats' and run all subsequent steps
    poetry run python scripts/run_pipeline.py --start-at "Process Wikipedia Stats"

    # Run from the beginning up to and including 'Enrich MongoDB with Wiki'
    poetry run python scripts/run_pipeline.py --stop-after "Enrich MongoDB with Wiki"
    ```

**Generating Embeddings:**

To generate embeddings:
1.  Ensure `embeddings.enabled: true` is set in `config/config.yaml`.
2.  Verify embedding model (`embeddings.model_name`), text fields (`embeddings.text_fields_to_embed`), and batch size (`embeddings.batch_size`) are configured as desired. See `docs/pkia_embedding_strategy.md`.
3.  Run the pipeline normally using `scripts/run_pipeline.py`. The "Generate Embeddings" step will execute if enabled and its dependencies (`Fix Ontology Issues`, `Enrich MongoDB with Wiki`) are met.

**Running Individual Scripts:**

While possible, running individual scripts directly (e.g., `poetry run python scripts/ingest_wikidata_mongo.py`) is generally **not recommended** for a full pipeline run as it bypasses the orchestration logic (dependency checks, standard parameter passing, error handling flow). However, it can be useful for debugging specific steps:

```bash
# Example: Run Wikidata ingestion directly, limiting to 10k items for testing
poetry run python scripts/ingest_wikidata_mongo.py --limit 10000

# Example: Run semantic corrections directly (reads config/config.yaml)
poetry run python scripts/apply_semantic_corrections.py

# Example: Run ontology fix directly
poetry run python scripts/fix_ontology_mongo.py

# Example: Run enrichment directly (ensure stats exist in configured format/location)
poetry run python scripts/enrich_mongo_with_wiki.py
```

*(Note: Individual scripts may have their own command-line arguments; use the `--help` flag for details, e.g., `poetry run python scripts/ingest_wikidata_mongo.py --help`)*.

## Testing

Run the automated tests (unit and integration) using `pytest`:

```bash
poetry run pytest
```

This command discovers and executes tests located in the `tests/` directory. Unit tests (`tests/unit`) use mocking extensively. Integration tests (`tests/integration`) verify interactions between components, potentially using `mongomock` or requiring further setup if using `testcontainers`. Refer to `docs/pkia_testing_strategy.md`.

## Output

*   **Primary Output:** Enriched documents stored in the MongoDB collection specified by `mongodb.collection_entities` in `config/config.yaml` (default: `wikidata_entities_v1`). The structure conforms to the schema definition, documented in `docs/mongo_schema_v1_documentation.md`.
*   **Intermediate Files:** Depending on configuration (`wikipedia_processing.stats_output_format`), intermediate files might be generated in the directory specified by `paths.processing_dir` (default: `./processing_output/`). For example:
    *   If format is `sqlite`: `wiki_stats.db` (used by enrichment).
    *   If format is `json`: `qid_inlink_counts.jsonl` and `qid_mentions.jsonl` (converted to temporary SQLite DB by enrichment).
*   **Logs:** Log files are stored in the location specified by `logging.log_file_path` (default: `./logs/pipeline.log`), formatted as text or JSON based on `logging.format`.

## Documentation

Detailed design documents, architectural considerations, and specific strategy documents can be found in the `/docs` directory. Key documents include:

*   `docs/pkia_deliverables.md`: Overview of architectural deliverables.
*   `docs/pkia_architecture.md`: (Placeholder) High-level Python application architecture.
*   `docs/design_considerations.md`: Core constraints and design choices (Memory, Normalization, Performance, etc.).
*   `docs/data_modeling_rationale.md`: Justification for the MongoDB document model choice.
*   `docs/mongo_schema_v1_documentation.md`: Detailed explanation of the MongoDB schema.
*   `docs/source_data_deep_dive.md`: Details on Wikidata/Wikipedia dump structures and processing assumptions.
*   `docs/legacy_analysis_and_gap.md`: Analysis of hypothetical legacy system issues and how this pipeline addresses them.
*   `docs/data_evolution_monitoring.md`: Strategy for monitoring changes in source data.
*   `docs/semantic_corrections.md`: Logic for semantic corrections.
*   `docs/ontology_flaw.md`: Explanation of the P131 hierarchy fix.
*   `docs/pkia_embedding_strategy.md`: Embedding generation details.
*   `docs/pkia_workflow_enhancements.md`: Runner script design.
*   `docs/pkia_testing_strategy.md`: Testing approach.
*   _(Other PKIA deliverables like Technology Stack, Component Design, DB Interaction, Coding Standards, Deployment Guide, Decision Log reside in `/docs` as well - see `pkia_deliverables.md`)._

## License

See the LICENSE file for details.

## Contributing

See CONTRIBUTING.md for details.
