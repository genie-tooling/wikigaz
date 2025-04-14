# Documentation: Wikidata Ontology Flaw (P131 Hierarchy)

**Identified Flaw:** Inconsistent Granularity in Administrative Hierarchy (P131)

**Description:**
The `P131` (located in the administrative territorial entity) property is crucial for building geographic hierarchies. However, its usage can be inconsistent across Wikidata entities. A common observed pattern is that an entity (e.g., a specific neighbourhood Q1) might have a `P131` link pointing to an intermediate administrative entity (e.g., a borough Q10) which itself is primarily defined as `P31` (instance of) "part of a city" or similar concept, and *then* this intermediate entity Q10 has a `P131` link to the actual encompassing city (Q100).

For applications requiring a clean, direct hierarchy (e.g., City -> State -> Country), this intermediate level ("sub-part") directly under a major administrative type (like City) can be problematic or represent unnecessary granularity.

**Example:**
*   **Q1 (Neighbourhood):** `P131 -> Q10` (Borough)
*   **Q10 (Borough):** `P31 -> Q707918` (Borough); `P131 -> Q100` (City)
*   **Q100 (City):** `P31 -> Q515` (City); `P131 -> Q1000` (State/Region)

**Desired Outcome for Q1:** A flattened hierarchy like `[Q100 (City), Q1000 (State/Region)]`, effectively skipping the intermediate Q10 because its parent Q100 is a major type (City).

**Mitigation Strategy (Implemented in `scripts/fix_ontology_mongo.py`):**
1.  **Identify Candidates:** Query MongoDB for entities where `ontology_flag_resolved` is not `true` and `admin_hierarchy` exists and has at least 2 levels.
2.  **Batch Processing:** Process candidate entities in batches.
3.  **Prefetch P31 Types:** For each batch, collect all unique QIDs present in the hierarchies. Fetch their `P31` (instance of) values from MongoDB in a single query and cache them (`_p31_cache`). This avoids repeated DB lookups during hierarchy checking.
4.  **Apply Rule:** For each entity in the batch:
    *   Iterate through its `admin_hierarchy` array from index 0 up to the second-to-last level.
    *   For each level `i` (current entity `B` with QID `qid_B`) and the next level `i+1` (parent entity `C` with QID `qid_C`):
        *   Look up the `P31` types for `qid_B` and `qid_C` from the cache.
        *   Check if `qid_B`'s types intersect with the configured `sub_part_types`.
        *   Check if `qid_C`'s types intersect with the configured `major_admin_types`.
        *   **If both conditions are true**, the current level `i` (entity `B`) is skipped when building the `new_hierarchy`.
        *   Otherwise, the current level `i` is added to the `new_hierarchy`.
    *   The last level of the original hierarchy is always added to `new_hierarchy`.
5.  **Update MongoDB:**
    *   If the hierarchy was modified by skipping levels, update the document's `admin_hierarchy` with the `new_hierarchy` (recalculating the `level` field sequentially) and set `ontology_flag_resolved` to `true`.
    *   If the hierarchy was checked but no modifications were needed according to the rule, just set `ontology_flag_resolved` to `true`.
    *   Use `bulk_write` for efficiency.

**Configuration (`config.yaml`):**
*   `ontology_fix.major_admin_types`: List of QIDs defining major administrative types (e.g., City, Municipality, Country, State).
*   `ontology_fix.sub_part_types`: List of QIDs defining intermediate "sub-part" types (e.g., Borough, Neighbourhood, District).

**Note:** This mitigation focuses solely on the described P131 intermediate entity pattern. Other ontology issues might exist and require different strategies. The script uses a dedicated flag (`ontology_flag_resolved`) to track which entities have been processed by this specific logic.
