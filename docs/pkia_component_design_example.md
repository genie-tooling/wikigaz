# Component Design: Wikidata Helpers (`utils/wikidata_helpers.py`)

**Version:** 1.0
**Date:** 2025-04-14
**Author:** Python Knowledge Integration Architect (PKIA)
**Status:** Implemented

## 1. Responsibilities

This utility module encapsulates the core logic for parsing and extracting specific pieces of information from a raw Wikidata JSON item dictionary, transforming it into the structure required by the target MongoDB schema (`config/mongo_schema_v1.json`). Its main responsibilities include:

*   Filtering raw Wikidata items based on configured criteria (`filter_wikidata_item`).
*   Extracting basic fields: labels, descriptions, aliases (handling language variants).
*   Extracting coordinates (P625) as GeoJSON Point objects.
*   Extracting simple claims values, primarily used for P31 (instance of).
*   Extracting complex properties involving qualifiers and specific data types:
    *   P17 (country) with P580/P582 (start/end time) qualifiers.
    *   P1082 (population) with P585 (point in time) qualifier.
    *   P2046 (area) with unit checking/conversion (currently only sqkm).
    *   P571 (inception) / P576 (dissolved) dates, extracting the year.
*   Extracting the administrative hierarchy by traversing P131 links upwards, utilizing an efficient **batch prefetching strategy** for performance.
*   Orchestrating the overall data extraction for a single item via `extract_entity_data`.
*   Managing the internal batch prefetching cache (`_batch_item_cache`) for P131 lookups during the processing of a single ingestion batch.
*   Providing internal helper functions for parsing Wikidata time strings (`_parse_wikidata_time`, `_extract_year_from_time_str`).

## 2. Key Classes/Modules/Functions

*   **Filtering:**
    *   `filter_wikidata_item(item, config)`: Checks `type`, `sitelinks.enwiki`, `claims.P625` based on `config`.
*   **Basic Extraction:**
    *   `extract_aliases(item)`: Groups aliases by base language, returns `Dict[str, List[str]]`.
    *   `extract_coordinates(item)`: Extracts P625, returns GeoJSON `dict` or `None`.
*   **Claim Extraction:**
    *   `extract_simple_claim_values(item, pid)`: Extracts simple QID values for a given PID (e.g., P31), returns `List[str]`.
    *   `extract_complex_property(item, pid, config)`: Handles specific PIDs (P17, P1082, P2046, P571, P576) considering qualifiers and types, returns specific structures or values.
*   **Hierarchy Extraction:**
    *   `extract_admin_hierarchy(item, get_item_data_func, max_depth)`: Core P131 traversal logic. Relies on `get_item_data_func` for fetching parent data.
    *   `_get_item_data_batch_fetcher(qids, collection)`: Internal function called by `ingest_wikidata_mongo.py` to populate the cache for a batch. Fetches minimal data from MongoDB for given QIDs.
    *   `_get_item_data_from_cache(qid)`: Internal function passed to `extract_admin_hierarchy` to retrieve data from the `_batch_item_cache`.
*   **Orchestration:**
    *   `extract_entity_data(item, config, get_item_data_func)`: Top-level function called by `ingest_wikidata_mongo.py`. It calls all other relevant extractors and assembles the final document dictionary matching the target schema structure (excluding fields added by later pipeline steps like `corrected_class` or `wiki_links`).
*   **Internal Helpers:**
    *   `_parse_wikidata_time(time_str)`: Parses Wikidata time strings into `datetime` objects, handling `YYYY-00-00` cases.
    *   `_extract_year_from_time_str(time_str)`: Robustly extracts the year integer from Wikidata time strings.

## 3. Data Flow & Dependencies

*   **Input:** Raw Wikidata item `dict`, global `config` `dict`, a `GetItemDataFunc` (typically `_get_item_data_from_cache`) for hierarchy traversal.
*   **Output:** A transformed `dict` representing the entity, partially conforming to `mongo_schema_v1.json` (fields added later like `wiki_links` will be initially `None`).
*   **Dependencies:** `utils.config`, `utils.helpers` (for `normalize_wikipedia_title` called within `extract_entity_data`), `pydash`, `datetime`, `re`. Uses `pymongo.collection.Collection` type hint, relies on `ingest_wikidata_mongo.py` to populate the batch cache via `_get_item_data_batch_fetcher`.

## 4. Key Algorithms & Design Choices

*   **Safety:** Extensive use of `pydash.get` and `.get()` for safe access to nested dictionary keys, preventing `KeyError` exceptions on missing data. Type checking (`isinstance`) is used before assuming data structures.
*   **Batch Prefetching (Hierarchy):**
    *   `ingest_wikidata_mongo.py` identifies all potential P131 target QIDs within a batch of raw items.
    *   `_get_item_data_batch_fetcher` fetches minimal data (`_id`, `english_label`, `claims.P131`) for these QIDs *once* per batch from MongoDB using `$in`.
    *   Results (including `None` for missing QIDs) are stored in the module-level `_batch_item_cache` (cleared for each batch).
    *   `extract_admin_hierarchy` uses `_get_item_data_from_cache`, avoiding direct DB calls during traversal.
    *   This significantly reduces database load compared to recursive single lookups.
*   **Complex Property Handling:** Dedicated logic within `extract_complex_property` for each supported PID (P17, P1082, P2046, P571, P576) to correctly parse amounts, units, dates, and relevant qualifiers (P580, P582, P585). Uses internal helpers `_parse_wikidata_time` and `_extract_year_from_time_str`.
*   **Type Preservation:** Helpers attempt to return appropriate types (e.g., `int` for year, `float` for area, `datetime` for point-in-time where possible).
*   **Modularity:** Functions are generally focused (e.g., `extract_coordinates` only handles P625). `extract_entity_data` orchestrates the calls.

## 5. Error Handling

*   Functions generally return `None` or empty collections (`[]`, `{}`) on error or if data is not found/parsable (e.g., `extract_coordinates` returns `None`, `extract_aliases` returns `{}`).
*   Warnings are logged for non-critical issues (e.g., failed date parse, unhandled units, missing hierarchy data).
*   This allows `extract_entity_data` to proceed and assemble a partial document even if some fields cannot be extracted. Critical errors (e.g., during cache fetching) might log errors but avoid crashing the entire batch if possible.

## 6. Assumptions

*   Relies on `ingest_wikidata_mongo.py` to manage the batch cache lifecycle (`clear`, `_get_item_data_batch_fetcher`).
*   Assumes the structure of Wikidata JSON dump items and claims matches common patterns.
*   Assumes `english_label` exists in MongoDB documents fetched for hierarchy labels.
*   Date/Time parsing handles common Wikidata formats but may miss edge cases.
*   Unit conversion is minimal (only sqkm for area).
