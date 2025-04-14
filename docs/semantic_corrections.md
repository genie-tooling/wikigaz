# Documentation: Semantic Corrections Logic

This document outlines the logic applied in `scripts/apply_semantic_corrections.py` and related design decisions based on the analysis of inconsistencies in the original (legacy) approach.

## 1. Class Assignment (`corrected_class` field)

**Goal:** Assign a single, semantically meaningful class QID to the `corrected_class` field in MongoDB, derived from the entity's `P31` (instance of) statements.

**Strategy (`config.semantic_correction.class_assignment_strategy` = `"most_specific_known"`):**
1.  **Retrieve P31 Values:** Get the list of QIDs from the entity's `instance_of` field (populated from P31 claims during ingestion).
2.  **Load Mapping Rules:** Load a predefined hierarchy or preference mapping from a configuration file (e.g., `config/class_mapping_rules.yaml` - *to be created/populated*). This file should define relationships or preferences (e.g., "Q515 (City) is preferred over Q17379834 (Administrative territorial entity)").
    *   *Format Idea (YAML):*
        ```yaml
        # Higher preference value means more preferred
        preference_order:
          - Q515    # City
          - Q7275   # County
          - Q34876  # Administrative region
          - Q17379834 # Administrative territorial entity
          # ... add more based on analysis
        # Or define hierarchical relationships if needed
        hierarchy:
          Q515: # City
            parent: Q17379834
          # ...
        ```
3.  **Apply Rules:**
    *   Iterate through the entity's P31 QIDs.
    *   Compare them against the preference list/hierarchy defined in the rules file.
    *   Select the QID that has the highest preference according to the rules.
    *   If multiple QIDs have the same highest preference level, the first one encountered (or a deterministic choice) can be taken.
4.  **Update Field:** Store the selected QID in the `corrected_class` field. If no P31 value matches any rule or preference, or if the entity has no P1 instances, leave the field `null`.

**Implementation:** This logic resides in `scripts/apply_semantic_corrections.py`.

## 2. Alias/Frequency Separation (`aliases` vs. `wiki_links`)

**Goal:** Avoid conflating Wikidata aliases with Wikipedia link anchor text frequencies.

**Decision:** Handled by Schema Design.
*   **Wikidata Aliases:** Extracted directly from the Wikidata dump (all languages) and stored in the `aliases` field in the MongoDB document (e.g., `aliases.en: ["USA", "United States of America"]`). This reflects names associated with the entity concept itself in Wikidata.
*   **Wikipedia Link Frequency/Mentions:** Calculated (if `wikipedia_processing.enabled` is true) by analyzing links *from* Wikipedia articles.
    *   The total incoming link count is stored in `wiki_links.inlink_count`.
    *   The most frequent anchor texts (mentions) used in those links are stored in `wiki_links.top_mentions`.
    This reflects how the concept is referred to *within* the context of Wikipedia's link graph.

**Implementation:** No specific correction script needed for this separation; it's handled by the separate ingestion (`ingest_wikidata_mongo.py`) and enrichment (`enrich_mongo_with_wiki.py`) processes populating distinct fields according to the `mongo_schema_v1.json`.

## 3. Probability Calculation (P(E|M))

**Goal:** Address misleading probability calculation from legacy system.

**Decision:** Remove P(E|M) calculation.
*   Analysis determined that the `P(E|M)` calculation (`probability_of_entity_given_mention`) was potentially misleading or not accurately calculated/used downstream.
*   The `config.semantic_correction.probability_calculation_enabled` flag is set to `false`.

**Implementation:** No logic for calculating P(E|M) will be implemented in the modernized pipeline. The focus is on providing clean entity data, aliases, and optional Wikipedia link counts/mentions separately.

## 4. Redirect Aggregation / Granularity Loss

**Goal:** Acknowledge and accept the level of aggregation.

**Decision:** QID-level aggregation is sufficient for the primary RAG use case.
*   The pipeline primarily operates at the Wikidata QID level. Information from different Wikipedia pages redirecting to the same core concept (linked via the same QID sitelink) is aggregated.
*   While this loses the nuance of which specific redirect was linked *from*, it simplifies the gazetteer structure for the defined RAG goal.

**Implementation:** This is an accepted design principle. The `enrich_mongo_with_wiki.py` script aggregates link counts and mentions based on the target QID derived via Wikimapper lookups.
