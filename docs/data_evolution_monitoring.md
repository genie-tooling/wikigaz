# Data Evolution Monitoring Strategy

**Version:** 1.0
**Date:** 2025-04-14
**Author:** Knowledge Graph Architect & Modernization Strategist

## 1. Introduction

Wikipedia and Wikidata are dynamic, constantly evolving knowledge bases. Changes in their schema, structure, content patterns, and dump formats pose a significant risk to the accuracy and robustness of the `wiki2gaz-modern` pipeline. This document outlines a strategy for monitoring relevant changes in these upstream data sources to proactively identify potential impacts and guide necessary maintenance or adaptation of the pipeline.

This complements the pipeline health monitoring strategy (focused on runtime performance and errors) by addressing the risks associated with the *data itself*.

## 2. Identified Risks of Data Evolution

*   **Wikidata Schema Changes:**
    *   Introduction of new properties (PIDs) relevant to our target entities.
    *   Deprecation or changing semantics of existing PIDs used by the pipeline.
    *   Changes in common data types for properties (e.g., a string becoming an item).
    *   Increased or changed usage of qualifiers, affecting complex property extraction.
    *   Evolution of the Wikidata ontology (new classes, changes to P279/P31 relationships).
*   **Wikipedia Structure/Content Changes:**
    *   Changes to common MediaWiki templates that might affect link extraction or content structure (though our current approach minimizes template reliance).
    *   Shifts in internal linking patterns or syntax.
    *   Changes in page naming conventions or redirect patterns.
*   **Dump Format Changes:**
    *   Modifications to the Wikimedia XML dump schema (`export-*.xsd`).
    *   Changes to the Wikidata JSON dump structure or serialization format.
    *   Changes to the format or content of the SQL dumps used by Wikimapper.

These changes can lead to:
*   Parsing errors during ingestion.
*   Incorrect data extraction (missing fields, wrong values, incorrect types).
*   Inaccurate semantic corrections or hierarchy generation.
*   Reduced effectiveness of mappings (e.g., Wikimapper becoming outdated).
*   Silent degradation of data quality if changes are subtle.

## 3. Monitoring Mechanisms

A multi-faceted approach is needed:

### 3.1. Automated Pipeline Monitoring (Error/Warning Focused)

*   **Mechanism:** Leverage the existing pipeline logging (`utils/logging_config.py`) and error handling (`scripts/run_pipeline.py`).
*   **Checks:**
    *   Monitor logs for spikes in parsing errors (XML, JSON).
    *   Track warnings related to failed data extraction (e.g., unparseable dates, unexpected types, missing units).
    *   Monitor warnings from `scripts/process_wiki_stats.py` regarding unmapped Wikipedia link targets (a potential indicator of Wikimapper staleness or normalization issues).
    *   Track failures in semantic correction or ontology fix steps, which might indicate unexpected P31/P131 patterns.
*   **Tools:** Existing logging infrastructure (file/console), potential aggregation using tools like Elasticsearch/Loki/Splunk if structured JSON logging is enabled. Setup alerts for critical error increases.

### 3.2. Periodic Manual/Semi-Automated Checks

*   **Mechanism:** Scheduled reviews and targeted analysis by the DA/PA roles.
*   **Checks & Frequency:**
    *   **Wikidata Property Review (Quarterly):** Review the usage documentation and discussion pages on Wikidata for the key PIDs extracted by the pipeline (P31, P625, P131, P17, P1082, P2046, P571, P576, P580, P582, P585). Check for deprecation notices, new recommended properties, or changes in common qualifier usage.
    *   **Wikidata Ontology Spot Check (Quarterly):** Review the P31 types used for common target entities (e.g., cities, countries) and the P131 hierarchy structure for a few representative examples to see if patterns targeted by `fix_ontology_mongo.py` are changing. Check `config/class_mapping_rules.yaml` against current ontology trends.
    *   **Wikimapper Staleness Check (Ad-hoc, e.g., before major runs):** Compare the date of the Wikimapper DB with the date of the Wikipedia XML dump being processed. If significantly different, assess the potential impact of unmapped links (using logs from `process_wiki_stats.py`). Consider rebuilding the Wikimapper index if the mismatch is large.
    *   **Dump Format Announcements (Continuous):** Monitor Wikimedia mailing lists (e.g., dumps-announce) or technical forums for announcements regarding dump format changes.
*   **Tools:** Wikidata website, SPARQL queries on Wikidata Query Service, pipeline logs, manual inspection of MongoDB data.

### 3.3. Schema Conformance Validation

*   **Mechanism:** Utilize the MongoDB `$jsonSchema` validation applied by `scripts/setup_mongodb.py`.
*   **Checks:** While primarily enforcing our target schema, validation errors during ingestion (`ingest_wikidata_mongo.py`) can sometimes indicate unexpected upstream data types or structures that violate our assumptions, even if the parsing didn't fail outright. Monitor BulkWriteError details for schema validation failures.
*   **Tools:** MongoDB logs, BulkWriteError details logged by the pipeline.

## 4. Impact Assessment Process

When a potential data evolution issue is detected (via monitoring or errors):

1.  **Triage:** The DA/PA assesses the severity. Is it a minor warning, a parsing failure, or a silent semantic shift?
2.  **Analysis:** Investigate the root cause. Examine the specific changes in the source data dump or schema documentation. Understand how the change affects the current pipeline logic (parsing, extraction, mapping, correction).
3.  **Quantify Impact:** Determine the scope of the impact. Does it affect a small subset of entities or core functionality? Does it break the pipeline or just degrade quality?
4.  **Propose Solution:** Define the necessary changes:
    *   Update parsing logic?
    *   Modify data extraction helpers (`wikidata_helpers.py`)?
    *   Adjust configuration (`config.yaml`, mapping rules)?
    *   Update the target MongoDB schema (`mongo_schema_v1.json`)?
    *   Require rebuilding Wikimapper index?
    *   No action needed (acceptable change)?
5.  **Document:** Record the issue, analysis, and proposed solution (e.g., in `docs/pkia_decision_log.md` or project issue tracker).

## 5. Update Process

1.  **Prioritize:** Schedule the implementation based on the severity and impact.
2.  **Implement:** Assign tasks to PI/DA/PA as appropriate. Update code, configuration, and schema.
3.  **Test:** Add new unit/integration tests specifically covering the data change scenario and the implemented fix. Ensure all tests pass.
4.  **Validate:** Perform validation (HD/DA) on a relevant data subset to confirm the fix works as expected and doesn't introduce regressions.
5.  **Deploy:** Roll out the updated pipeline.
6.  **Update Documentation:** Update relevant documents (`source_data_deep_dive.md`, helper docstrings, schema documentation, this document) to reflect the change and the new handling logic.

## 6. Conclusion

Proactive monitoring for data evolution is essential for maintaining the long-term value and accuracy of the `wiki2gaz-modern` pipeline. This strategy combines automated error tracking, periodic expert review, and schema validation to detect changes early and provides a process for assessing impact and implementing necessary adaptations.
