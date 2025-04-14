# Database Interaction Patterns & Guidelines

**Version:** 1.0
**Date:** 2025-04-14
**Author:** Python Knowledge Integration Architect (PKIA)
**Status:** Implemented

## 1. Introduction

This document provides guidelines and best practices for interacting with the different data stores used by the `wiki2gaz-modern` Python components:
1.  **MongoDB (`wikidata_entities_v1` collection):** The primary data store for processed entities.
2.  **Wikimapper SQLite DB (`index_*.db`):** Read-only database for Wikipedia Title -> QID mapping.
3.  **Temporary Stats SQLite DB (`temp_wiki_stats_lookup.db`):** Optional, temporary database used during enrichment if stats are generated as JSONL.

Adhering to these patterns ensures efficient, robust, and maintainable database interactions.

## 2. MongoDB (`wikidata_entities_v1` collection)

*   **Connection Management:**
    *   **Always** use the helper functions in `utils/mongo_helpers.py`:
        *   `get_mongo_client()`: Returns a cached, connection-checked client instance.
        *   `get_database()`: Returns the configured database handle, using the cached client.
        *   `get_entity_collection()`: Returns the handle for the main entity collection.
    *   These functions handle configuration loading, URI parsing, connection pooling (implicitly via the driver), and basic connection checking.
*   **Writes (Ingestion/Updates):**
    *   **Use `bulk_write`:** For any operation involving multiple documents (ingestion, enrichment, corrections, embeddings), gather operations into a list and execute using `collection.bulk_write(operations, ordered=False)`. `ordered=False` allows processing to continue even if some operations fail within the batch.
    *   **Ingestion (`ingest_wikidata_mongo.py`):** Use `pymongo.ReplaceOne({"_id": qid}, replacement_doc, upsert=True)`. This efficiently inserts new documents or completely replaces existing ones based on the Wikidata QID (`_id`), ensuring the latest data from the dump overwrites previous state.
    *   **Enrichment/Corrections (`enrich_mongo_*.py`, `apply_*.py`, `fix_*.py`, `generate_*.py`):** Use `pymongo.UpdateOne({"_id": qid}, {"$set": {field_to_update: value}})` within `bulk_write`. This targets specific fields for updates, avoiding overwriting the entire document.
    *   **Error Handling:** Wrap `bulk_write` calls in `try...except pymongo.errors.BulkWriteError as bwe:`. Log `bwe.details` to understand which operations failed and why (e.g., schema validation errors). Handle other potential exceptions like `ConnectionFailure`.
*   **Queries:**
    *   **Projections:** Always specify the fields you need using the projection argument (`{field1: 1, field2: 1}`). Avoid retrieving entire large documents if only a few fields are required, especially when iterating.
    *   **Filtering:** Utilize indexed fields whenever possible for efficient querying. Key indexed fields include:
        *   `_id` / `wikidata_id` (Primary Key)
        *   `coordinates` (Geospatial Index)
        *   `corrected_class`
        *   `wiki_links.normalized_enwiki_title`
        *   `admin_hierarchy.qid` (Multikey Index)
        *   `ontology_flag_resolved`
        *   `english_label` (Case-sensitive index)
        *   (Check `scripts/setup_mongodb.py` for the full list)
    *   **Large Result Sets:** When iterating over many documents (e.g., in enrichment/correction scripts querying candidates), use `no_cursor_timeout=True` on the `find()` call to prevent the cursor from timing out on the server side. Ensure the script has robust error handling around the iteration loop. `find().batch_size()` can sometimes help control memory usage on the client side.
    *   **Batch Lookups:** For fetching multiple specific documents by ID (e.g., P131 prefetching), use the `$in` operator: `collection.find({"_id": {"$in": list_of_qids}}, projection=...)`.
*   **Schema Validation:** Be aware that insertions/updates must conform to the `$jsonSchema` defined in `config/mongo_schema_v1.json` and applied by `scripts/setup_mongodb.py`. Schema validation errors will appear in `BulkWriteError` details.

## 3. Wikimapper SQLite DB (`index_*.db`)

*   **Access:** **Always** use the `WikimapperLookup` class from `utils/mapping.py`.
*   **Usage Pattern:** Instantiate and use it as a context manager:
    ```python
    from utils.mapping import WikimapperLookup
    # config = get_config() # Assumes config is loaded
    try:
        with WikimapperLookup(config) as mapper:
            normalized_title = "Some_Normalized_Title"
            qid = mapper.get_qid(normalized_title)
            if qid:
                # ... process QID ...
            else:
                # ... handle not found ...
    except (FileNotFoundError, ConfigurationError, sqlite3.Error) as e:
        # Handle initialization or query errors
        logger.error(f"Wikimapper access failed: {e}")
    ```
*   **Connection:** The class handles opening a read-only connection and closing it automatically when the `with` block exits.
*   **Input:** The `get_qid` method expects a title that has already been canonically normalized using `utils.helpers.normalize_wikipedia_title`.
*   **Concurrency:** The class includes a `threading.Lock` for safe use across multiple threads if the *same instance* is shared.

## 4. Temporary Stats SQLite DB (`temp_wiki_stats_lookup.db`)

*   **Context:** Created and used *only* within `scripts/enrich_mongo_with_wiki.py` if the stats output format from `process_wiki_stats.py` is configured as `"json"`.
*   **Creation:** Handled by the `create_temp_stats_db` function within the enrichment script. It reads JSON Lines files (`qid_inlink_counts.jsonl`, `qid_mentions.jsonl`) and populates an indexed SQLite DB.
*   **Access:** Use the `StatsLookup` class (defined *inside* `scripts/enrich_mongo_with_wiki.py`).
*   **Usage Pattern:** Similar to `WikimapperLookup`, use as a context manager:
    ```python
    # Within enrich_mongo_with_wiki.py
    # temp_db_path = ... # Path to the created DB
    try:
        with StatsLookup(temp_db_path) as lookup:
            count = lookup.get_count("Q123")
            mentions = lookup.get_top_mentions("Q123", limit=10)
            # ... process stats ...
    except (FileNotFoundError, sqlite3.Error) as e:
        logger.error(f"Stats DB lookup failed: {e}")
    ```
*   **Connection:** The class manages the read-only connection and closing.
*   **Lifecycle:** The temporary DB file is automatically deleted upon script completion unless the `--keep-temp-db` flag is used for debugging.

By adhering to these patterns, particularly using the provided helper classes/functions and favouring batch operations, the pipeline can interact with its data stores efficiently and reliably.
