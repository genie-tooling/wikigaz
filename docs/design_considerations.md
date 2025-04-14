# Design Considerations

**Version:** 1.1
**Date:** 2025-04-14

This document outlines key architectural and implementation constraints and decisions influencing the design of the `wiki2gaz-modern` pipeline.

## 1. Memory Constraints

*   **Constraint:** The target execution environment has significant but *limited* RAM. It cannot reliably load extremely large data structures (e.g., multi-gigabyte JSON files or dictionaries representing full Wikipedia statistics) entirely into memory.
*   **Implication:** All processing steps involving potentially large datasets (Wikipedia XML dump, Wikidata JSON dump, intermediate statistics) **must** prioritize streaming or iterative processing over in-memory loading.
*   **Design Choices & Rationale:**
    *   **Dump Parsing:** Use iterative parsers (`ijson` for Wikidata JSON, `lxml.etree.iterparse` or `xml.etree.ElementTree.iterparse` for Wikipedia XML). Explicit memory clearing (`elem.clear()`, parent manipulation) is crucial within the XML parsing loop. *This addresses the Memory Constraint by avoiding loading entire dump files.*
    *   **Intermediate Statistics (`process_wiki_stats.py`):**
        *   Aggregation logic (`aggregate_stats`) uses in-memory `defaultdict`/`Counter`. **PI Action Required:** Test memory usage on target data subset and report findings to PA. Alternatives (external sort/merge, DB aggregation) may be needed if RAM limits are exceeded. *This is a potential memory risk; verification is needed to ensure it meets the constraint.*
        *   When saving stats to `json` format, use **JSON Lines (.jsonl)**. *This addresses the Memory Constraint by avoiding the creation of huge single JSON files.*
        *   When saving stats to `sqlite` format, use batch inserts (`executemany`). *This optimizes write performance under potential memory pressure.*
    *   **Statistics Consumption (`enrich_mongo_with_wiki.py`):**
        *   Cannot load `.jsonl` stats files directly into memory for random access (QID lookups).
        *   If `stats_output_format` is `json`, the script first converts `.jsonl` files into a temporary, indexed **SQLite database**. The `StatsLookup` class uses this DB. *This addresses the Memory Constraint by providing efficient, low-memory key-based lookups instead of loading all stats.*
        *   If `stats_output_format` is `sqlite`, the script uses the pre-built SQLite DB directly via `StatsLookup`. *Leverages the efficient disk-based lookup.*
    *   **Data Structures:** Favor generators and iterators over loading full lists where practical (e.g., `stream_wikidata_dump`, `parse_wikipedia_dump`). *Reduces peak memory usage during processing.*
    *   **Hierarchy Traversal (`extract_admin_hierarchy`):** Implemented a **Batch Prefetching Strategy**. Fetches minimal data for all needed P131 targets in a batch *once* from MongoDB, caching the results (`_batch_item_cache`). The hierarchy traversal function (`extract_admin_hierarchy`) reads from this cache. *This addresses potential Performance constraints and reduces Memory pressure by minimizing repeated database calls compared to recursive single lookups.*

## 2. Normalization Consistency

*   **Requirement:** Consistent canonical normalization of Wikipedia titles is critical for joining data between Wikidata sitelinks, Wikipedia page titles, and internal Wikipedia links. Inconsistent normalization was a likely failure point in legacy systems.
*   **Design Choice & Rationale:** Implement a single canonical normalization function (`utils.helpers.normalize_wikipedia_title`) based on PA/DA specification v1.0 and ensure it is used *exclusively* wherever Wikipedia titles are processed or compared (Wikimapper lookup, stats aggregation, enrichment linking, storing `normalized_enwiki_title`). *This addresses the Reliability/Accuracy requirement by ensuring consistent keys for joining data across sources.* See `docs/source_data_deep_dive.md` for details on link processing.

## 3. Data Source Reliability & Complexity

*   **Challenge:** Wikidata and Wikipedia dumps can have inconsistencies, missing data, complex structures (qualifiers, ranks), or evolve over time, breaking assumptions made by simpler or older pipelines.
*   **Design Choices & Rationale:**
    *   Use safe dictionary access (`pydash.get` or `.get()`) during parsing and data extraction (`utils/wikidata_helpers.py`). *Improves Robustness by preventing errors on missing keys.*
    *   Implement specific extractors for complex properties (P17, P1082, P2046, P571, P576) handling relevant qualifiers as specified (`extract_complex_property`). *Improves Accuracy by correctly interpreting contextual data.*
    *   Implement robust error handling and logging for parsing errors, missing links, or failed lookups. Log warnings for recoverable issues. *Improves Maintainability and Debugging.*
    *   Document specific data quality issues encountered (e.g., `docs/ontology_flaw.md`) and implement targeted mitigation strategies (`scripts/fix_ontology_mongo.py`). *Directly addresses known Reliability issues.*
    *   Record dump versions used for traceability (manual or via config). *Improves Reproducibility and Debugging.*
    *   Refer to `docs/source_data_deep_dive.md` for detailed assumptions about source data structures and `docs/data_evolution_monitoring.md` for strategy on handling changes.

## 4. Configuration Management

*   **Requirement:** Pipeline behaviour (paths, credentials, feature flags, parameters) must be configurable without code changes for flexibility and maintainability.
*   **Design Choice & Rationale:** Use a primary YAML configuration file (`config/config.yaml`) combined with environment variables (via `.env` and `python-dotenv`) for sensitive data or overrides. Implement robust config loading and value resolution (`utils/config.py`) including environment variable substitution (`${VAR:-default}`), config references (`${config.path.key}`), and path absolutization. *Provides Flexibility and addresses Maintainability by separating configuration from code.*

## 5. Modularity and Testability

*   **Goal:** Ensure the pipeline components are reasonably modular and testable, improving maintainability and reliability.
*   **Design Choice & Rationale:**
    *   Separate concerns into distinct scripts (ingestion, stats processing, enrichment, correction). *Improves Modularity.*
    *   Utilize utility modules (`utils/`) for shared functionality (config, logging, helpers, DB access, mapping). *Promotes Code Reuse and Modularity.*
    *   Design functions with clear inputs/outputs and type hints. *Enhances Readability and Testability.*
    *   Employ mocking (`unittest.mock`, `mongomock`) for unit tests and recommend `testcontainers` for integration tests. *Enables comprehensive Testing.* (See `docs/pkia_testing_strategy.md`).

## 6. Performance

*   **Goal:** Process large data dumps efficiently within resource constraints (Memory, Time).
*   **Design Choices & Rationale:**
    *   Streaming parsers (see Memory Constraints). *Addresses Memory and Time constraints.*
    *   Batch database operations (`bulk_write` for MongoDB, `executemany` for SQLite). *Improves database interaction Time efficiency.*
    *   Efficient lookup mechanisms (indexed SQLite databases for Wikimapper and intermediate stats). *Reduces lookup Time and Memory usage compared to in-memory alternatives.*
    *   Batch prefetching for P131 hierarchy lookups. *Reduces database query Time significantly during hierarchy building.*
    *   Careful selection of Python libraries (e.g., `lxml` preferred over `xml.etree`, `ijson` for JSON streaming). *Optimizes CPU and Memory usage during parsing.*
    *   (Future) Potential for parallelization using `multiprocessing` or `concurrent.futures` for CPU-bound tasks if identified as bottlenecks.

## 7. Pipeline Orchestration

*   **Requirement:** Execute pipeline steps reliably in the correct order, respecting dependencies, and handling errors gracefully.
*   **Design Choice (Initial):** A custom Python runner script (`scripts/run_pipeline.py`) reads a JSON configuration (`config/pipeline_config.json`). *Provides sufficient control without external dependencies for current complexity.*
*   **Refinement (Phase 3/4):** Enhanced the runner with:
    *   Topological sort (`graphlib`) for robust dependency management.
    *   Clear "Fail Fast" error handling strategy.
    *   Standardized parameter passing (args for Python, environment variables for shell).
    *   Improved logging and conditional step execution (`enabled`, `run_if_missing`).
    *   *Rationale:* These enhancements address Reliability and Maintainability of the workflow execution based on architectural guidance (`docs/pkia_workflow_enhancements.md`).
