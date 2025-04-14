# Python Application Architecture

**Version:** 1.0
**Date:** 2025-04-14
**Author:** Python Knowledge Integration Architect (PKIA)
**Status:** Implemented

## 1. Introduction

This document provides the high-level architectural blueprint for the Python components of the `wiki2gaz-modern` system. It describes the main components (scripts and utilities), their responsibilities, and how they interact to achieve the overall goal of ingesting, processing, and storing integrated knowledge from Wikipedia and Wikidata into MongoDB.

## 2. Architectural Style

The system follows a **modular, script-based pipeline architecture**. Each major processing stage (e.g., Wikidata ingestion, Wikipedia stats calculation, enrichment, corrections) is encapsulated within a dedicated Python script located in the `scripts/` directory. These scripts are designed to be executed sequentially, often operating on data stored in shared locations (data dumps, MongoDB, intermediate files).

A central **pipeline runner script** (`scripts/run_pipeline.py`) orchestrates the execution of these individual scripts based on a declarative pipeline configuration (`config/pipeline_config.json`), managing dependencies and error handling.

Shared functionality, such as configuration loading, logging, database connections, data normalization, and specific Wikidata parsing logic, is extracted into **utility modules** within the `utils/` directory to promote code reuse and maintainability.

## 3. Core Components

*   **`scripts/` Directory:** Contains the executable pipeline stage scripts.
    *   `run_pipeline.py`: Orchestrates the entire pipeline execution, reading `pipeline_config.json`, resolving dependencies via topological sort (`graphlib`), executing steps via `subprocess`, handling errors, and managing parameter passing.
    *   `setup_mongodb.py`: Configures the target MongoDB collection by applying the JSON schema validation (`config/mongo_schema_v1.json`) and creating necessary indexes.
    *   `ingest_wikidata_mongo.py`: Streams the Wikidata JSON dump (`ijson`), filters items, extracts data using `utils/wikidata_helpers.py` (including P131 hierarchy with batch prefetching), and performs bulk writes (`ReplaceOne` upserts) to MongoDB.
    *   `build_wikimapper_index.sh`: Shell script wrapper invoking the external `wikimapper` CLI tool to build the SQLite mapping database from Wikipedia SQL dumps.
    *   `process_wiki_stats.py`: Streams the Wikipedia XML dump (`lxml.etree.iterparse`), extracts internal links, normalizes target titles, uses `utils/mapping.WikimapperLookup` to get QIDs, aggregates inlink counts and mention frequencies in memory, and saves results to SQLite or JSON Lines format.
    *   `enrich_mongo_with_wiki.py`: Reads aggregated stats (potentially converting JSONL to temporary SQLite DB via `StatsLookup`), queries MongoDB for entities needing enrichment, and updates them with Wikipedia signals (`wiki_links`).
    *   `apply_semantic_corrections.py`: Applies configured class assignment logic (`corrected_class`) based on rules (`config/class_mapping_rules.yaml`).
    *   `fix_ontology_mongo.py`: Applies specific P131 hierarchy corrections based on configured rules and P31 type prefetching.
    *   `generate_embeddings.py`: (Optional) Generates semantic vector embeddings using `sentence-transformers` for specified fields and stores them in MongoDB (as BSON Binary).
    *   `download_data.sh`: Helper shell script to download required Wikimedia dumps.
*   **`utils/` Directory:** Contains reusable helper modules.
    *   `config.py`: Loads YAML configuration (`config/config.yaml`) and `.env` files, resolves `${VAR}` and `${config...}` references, absolutizes paths.
    *   `logging_config.py`: Sets up configurable logging (console/file, text/JSON).
    *   `helpers.py`: Provides general utility functions, notably the canonical `normalize_wikipedia_title`.
    *   `mongo_helpers.py`: Manages MongoDB client/database connections and provides access to the entity collection. Includes helpers potentially used by `wikidata_helpers`.
    *   `wikidata_helpers.py`: Contains functions specific to parsing and extracting data from Wikidata JSON structures (coordinates, aliases, complex properties, P131 hierarchy traversal with batch prefetching).
    *   `mapping.py`: Provides the `WikimapperLookup` class to query the Wikimapper SQLite database.
*   **`config/` Directory:** Holds configuration files.
    *   `config.yaml`: Main configuration (paths, features, parameters).
    *   `.env` (not committed): Environment-specific overrides (e.g., `MONGO_URI`).
    *   `pipeline_config.json`: Defines pipeline steps, dependencies, enablement, and parameters for the runner.
    *   `mongo_schema_v1.json`: MongoDB `$jsonSchema` for validation.
    *   `class_mapping_rules.yaml`: Rules for semantic class assignment.
*   **`tests/` Directory:** Contains unit and integration tests.

## 4. Data Flow

The primary data flow follows the sequence defined in `config/pipeline_config.json` and orchestrated by `scripts/run_pipeline.py`. See the ASCII diagram in `README.md` for a visual representation. Key interactions involve:
1.  Downloading dumps (optional).
2.  Setting up MongoDB (schema, indexes).
3.  Building Wikimapper index (if Wikipedia processing enabled).
4.  Ingesting Wikidata JSON -> MongoDB.
5.  Processing Wikipedia XML -> Intermediate Stats (SQLite/JSONL).
6.  Enriching MongoDB with stats from intermediate storage.
7.  Applying semantic corrections to MongoDB documents.
8.  Applying ontology fixes to MongoDB documents.
9.  Generating embeddings (optional) into MongoDB documents.

## 5. Key Architectural Decisions

*   **Modularity:** Separation into scripts and utils.
*   **Streaming:** Prioritized for handling large dumps (`ijson`, `iterparse`).
*   **Configuration Driven:** Using YAML/JSON/.env for flexibility.
*   **Centralized Normalization:** Single function for Wikipedia titles.
*   **Optimized Lookups:** SQLite for Wikimapper/stats, batch prefetching for hierarchy.
*   **Explicit Orchestration:** Custom runner script with topological sort.
*   **Document Database:** MongoDB chosen for schema flexibility and RAG use case alignment.

See `docs/design_considerations.md` and `docs/pkia_decision_log.md` for more details.
