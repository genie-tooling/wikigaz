# Legacy System Analysis & Modernization Gap Document

**Version:** 1.0
**Date:** 2025-04-14
**Author:** Knowledge Graph Architect & Modernization Strategist

## 1. Introduction

This document analyzes the characteristics and shortcomings of the hypothetical 5-year-old legacy system that `wiki2gaz-modern` aims to replace. It identifies key gaps, particularly those arising from the evolution of Wikipedia and Wikidata data sources, and explicitly maps how the modernized architecture addresses these issues. This provides context and justification for the design choices in the new pipeline.

## 2. Legacy System Profile (Hypothetical)

The previous system, developed approximately 5 years ago, likely exhibited some of the following characteristics:

*   **Data Processing:** Batch-oriented processing, potentially loading large portions of dumps into memory or using less efficient parsing techniques. Limited use of streaming.
*   **Data Formats:** Possibly relied on older or less structured dump formats if available at the time. Parsing logic might have been tightly coupled to specific XML structures or template patterns common 5 years ago.
*   **Wikidata Integration:** May have focused primarily on labels, descriptions, and basic properties (P31, coordinates), potentially lacking robust handling for complex qualifiers, ranks, or newer properties. Hierarchy traversal might have been simplistic or inefficient (e.g., recursive single lookups).
*   **Wikipedia Integration:** May have used simpler link extraction (e.g., basic regex without robust normalization), potentially leading to inaccurate link counts or mention association. Could have struggled with the scale of Wikipedia processing without streaming.
*   **Normalization:** Title normalization might have been inconsistent or incomplete (e.g., only handling underscores, not full URL decoding or Unicode normalization), leading to joining issues between Wikipedia and Wikidata.
*   **Schema/Storage:** Potentially stored data in a relational database (requiring complex EAV models or losing structure) or an earlier version of MongoDB with less mature schema validation capabilities. Schema might not have cleanly separated Wikidata aliases from Wikipedia mentions.
*   **Configuration:** Less flexible configuration, possibly hardcoding paths, properties, or logic.
*   **Resilience:** Limited resilience to changes in upstream data formats or schemas. Failures might have been harder to debug or required significant code changes to adapt.

## 3. Identified Gaps & Problems Caused by Data Evolution

Based on the legacy profile, several key problems likely emerged due to the dynamic nature of Wikipedia and Wikidata:

*   **Gap 1: Brittle Wikipedia Parsing:** Changes in MediaWiki templates, formatting conventions, or internal link syntax over 5 years likely broke parsing logic tied to specific legacy structures, leading to inaccurate link counts or missed data.
*   **Gap 2: Outdated Wikidata Schema Assumptions:** New properties relevant to entities might be ignored. Changes in property usage (e.g., increased use of qualifiers like P585 on population) could lead to incorrect data extraction if the old system didn't handle them. Ontology shifts (new classes, changing P131 patterns) could lead to inaccurate typing or hierarchy representation.
*   **Gap 3: Inconsistent Title Normalization:** Differences in how titles were represented (underscores vs. spaces, URL encoding variations, Unicode forms) likely caused failures when trying to join Wikipedia link data (based on titles) with Wikidata entities (via sitelinks). This is a major source of data loss/inaccuracy in knowledge graph integration.
*   **Gap 4: Scalability Bottlenecks:** Batch processing or in-memory loading approaches likely hit performance or memory limits as Wikipedia and Wikidata dumps grew significantly over 5 years.
*   **Gap 5: Inaccurate Semantic Representation:** Simplistic class assignment (e.g., taking the first P31) or conflating aliases with link mentions could lead to a less accurate or useful gazetteer for downstream tasks. Handling of redirects might have lost important semantic distinctions.
*   **Gap 6: Maintenance Difficulty:** Lack of robust configuration, tight coupling, and insufficient logging/monitoring likely made adapting the legacy system to data changes or new requirements increasingly difficult and error-prone.

## 4. How `wiki2gaz-modern` Addresses the Gaps

The architecture and design of `wiki2gaz-modern` directly target these legacy gaps:

*   **Addressing Gap 1 (Wiki Parsing):**
    *   Uses streaming XML parsing (`lxml`/`etree.iterparse`) with careful memory management (`elem.clear()`) to handle large dumps reliably (`scripts/process_wiki_stats.py`).
    *   Focuses on extracting standard MediaWiki links (`[[...]]`) rather than relying on complex, brittle template parsing.
*   **Addressing Gap 2 (Wikidata Schema):**
    *   Uses streaming JSON parsing (`ijson`) for large Wikidata dumps (`scripts/ingest_wikidata_mongo.py`).
    *   Employs configurable property extraction (`config.yaml`), allowing easy adaptation to relevant properties.
    *   Includes dedicated logic for handling common complex properties and their qualifiers (`utils/wikidata_helpers.extract_complex_property`).
    *   Implements targeted fixes for known ontology issues (e.g., P131 hierarchy flaw in `scripts/fix_ontology_mongo.py`).
    *   Uses safe data access patterns (`pydash.get`, `.get()`) to handle missing fields gracefully.
*   **Addressing Gap 3 (Normalization):**
    *   Defines and consistently applies a **canonical Wikipedia title normalization function** (`utils.helpers.normalize_wikipedia_title`) across all relevant stages (Wikidata sitelink processing, Wikipedia link extraction, Wikimapper lookups). This ensures reliable joins.
*   **Addressing Gap 4 (Scalability):**
    *   **Streaming First:** Prioritizes streaming for both Wikidata JSON and Wikipedia XML dumps.
    *   **Efficient Lookups:** Uses indexed SQLite databases for Wikimapper and intermediate Wikipedia statistics, avoiding loading huge datasets into memory for lookups.
    *   **Batch Operations:** Leverages MongoDB `bulk_write` and SQLite `executemany` for efficient database interactions.
    *   **Batch Prefetching:** Optimizes P131 hierarchy traversal by prefetching data for needed QIDs within a batch.
*   **Addressing Gap 5 (Semantics):**
    *   Implements configurable, rule-based semantic class assignment (`scripts/apply_semantic_corrections.py`).
    *   Clearly separates Wikidata aliases (`aliases` field) from Wikipedia link mentions (`wiki_links.top_mentions` field) in the schema.
    *   Acknowledges QID-level aggregation for redirects as a conscious design choice suitable for the RAG use case (see `docs/semantic_corrections.md`).
*   **Addressing Gap 6 (Maintenance):**
    *   **Configuration Driven:** Pipeline flow, parameters, feature flags, paths, and credentials are configurable via YAML/JSON/`.env` files (`config/`, `utils/config.py`).
    *   **Modular Design:** Separates concerns into distinct scripts and utility modules.
    *   **Robust Orchestration:** Uses a pipeline runner (`scripts/run_pipeline.py`) with dependency management (topological sort) and clear error handling.
    *   **Code Quality & Testing:** Enforces standards (typing, linting, formatting) and incorporates unit/integration testing (`tests/`).
    *   **Comprehensive Logging:** Configurable logging (JSON/text, file/console) provides better visibility (`utils/logging_config.py`).

## 5. Conclusion

The `wiki2gaz-modern` pipeline represents a significant architectural improvement over the hypothetical legacy system. By embracing streaming, robust normalization, configurable processing, targeted semantic corrections, and modern development practices, it is designed to be more **accurate**, **resilient** to data evolution, **scalable** to handle large data volumes, and **maintainable** over the long term. This directly addresses the key pain points and data drift issues inherent in processing dynamic knowledge sources like Wikipedia and Wikidata.
