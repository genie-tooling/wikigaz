#!/usr/bin/env python3
import logging
import argparse
import sys
import os
import time
from typing import Dict, Any, List, Optional, Iterator, Set, Tuple

# Make utils discoverable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from utils.config import load_config, get_config, pydash_get, ConfigurationError
from utils.logging_config import setup_logging
from utils.mongo_helpers import (
    get_entity_collection,
    get_minimal_item_data_for_hierarchy,
)  # Import specific fetcher
from pymongo.collection import Collection
from pymongo.errors import (
    ConnectionFailure,
    BulkWriteError,
    ConfigurationError as MongoConfigError,
)
from pymongo import UpdateOne  # Import UpdateOne

logger = logging.getLogger(__name__)

# --- Configuration Defaults (can be overridden in config.yaml) ---
DEFAULT_MAJOR_ADMIN_TYPES: Set[str] = {
    "Q515",
    "Q15284",
    "Q6256",
    "Q3624078",
    "Q10864048",
}  # City, Muni, Country, State, AdminDiv
DEFAULT_SUB_PART_TYPES: Set[str] = {
    "Q707918",
    "Q123705",
    "Q34876",
    "Q15634554",
}  # Borough, N'hood, AdminRegion, CDP

# Cache for fetched P31 types to reduce DB lookups within a processing batch
# Maps QID -> Set[P31_QID]
_p31_cache: Dict[str, Set[str]] = {}
_cache_hits = 0
_cache_misses = 0


def prefetch_p31_types(qids_to_fetch: Set[str], collection: Collection) -> None:
    """
    Prefetches P31 types for a set of QIDs and populates the _p31_cache.

    Args:
        qids_to_fetch: Set of QIDs whose P31 types are needed for the current batch.
        collection: The MongoDB collection to query.
    """
    global _p31_cache, _cache_hits, _cache_misses
    fetch_start_time = time.time()

    # Only fetch QIDs not already in the cache for this batch
    qids_needed = list(qids_to_fetch - _p31_cache.keys())
    if not qids_needed:
        logger.debug(
            "P31 prefetch skipped: All required QIDs already in cache for this batch."
        )
        return

    logger.debug(f"Prefetching P31 types for {len(qids_needed)} QIDs...")
    query = {"_id": {"$in": qids_needed}}
    # Project only the necessary fields: _id and instance_of
    projection = {"_id": 1, "instance_of": 1}
    fetched_count = 0
    try:
        cursor = collection.find(query, projection)
        for doc in cursor:
            fetched_count += 1
            qid = doc["_id"]
            # Ensure instance_of is a list of strings, handle potential older/missing data
            types = set(
                str(t) for t in doc.get("instance_of", []) if isinstance(t, str)
            )
            _p31_cache[qid] = types
            _cache_misses += (
                1  # Count as a miss initially, hit logic is in apply_ontology_fix
            )

        # Mark QIDs that were requested but not found in DB as fetched (with empty types)
        # This prevents repeated lookups for non-existent QIDs within the same batch.
        fetched_qids = (
            _p31_cache.keys() & qids_needed
        )  # Use intersection to find which of needed were actually added
        missing_qids = set(qids_needed) - fetched_qids
        for qid in missing_qids:
            _p31_cache[qid] = set()
            # Reduce log noise for potentially many missing items
            # logger.warning(f"P31 type data not found in DB for QID: {qid}")

        fetch_duration = time.time() - fetch_start_time
        logger.debug(
            f"Prefetched P31 data for {fetched_count}/{len(qids_needed)} QIDs in {fetch_duration:.2f}s."
        )
        if missing_qids:
            logger.warning(
                f"P31 data was missing for {len(missing_qids)} QIDs during prefetch."
            )

    except Exception as e:
        logger.error(f"Error during P31 prefetching: {e}", exc_info=True)
        # Mark all needed QIDs as empty to prevent retries in this batch on error
        for qid in qids_needed:
            if (
                qid not in _p31_cache
            ):  # Avoid overwriting successfully fetched ones if error is partial
                _p31_cache[qid] = set()


def _get_p31_from_cache_or_db(qid: str, collection: Collection) -> Set[str]:
    """
    Helper to get P31 types, prioritizing cache, falling back to DB lookup if necessary.
    (Used if prefetching wasn't exhaustive or failed for some QIDs).
    Updates the cache if a DB lookup occurs.
    """
    global _p31_cache, _cache_hits, _cache_misses
    if qid in _p31_cache:
        _cache_hits += 1
        return _p31_cache[qid]
    else:
        # Fallback: Query DB for this single QID (less efficient)
        _cache_misses += 1
        logger.warning(f"P31 cache miss for QID {qid}. Performing fallback DB lookup.")
        query = {"_id": qid}
        projection = {"_id": 1, "instance_of": 1}
        try:
            doc = collection.find_one(query, projection)
            if doc:
                types = set(
                    str(t) for t in doc.get("instance_of", []) if isinstance(t, str)
                )
                _p31_cache[qid] = types
                return types
            else:
                logger.warning(
                    f"P31 data not found in DB for QID {qid} during fallback lookup."
                )
                _p31_cache[qid] = set()  # Cache the miss
                return set()
        except Exception as e:
            logger.error(f"Error during fallback P31 DB lookup for {qid}: {e}")
            _p31_cache[qid] = set()  # Cache the miss/error
            return set()


def apply_ontology_fix(
    doc: Dict[str, Any], collection: Collection, config: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """
    Applies the P131 hierarchy cleanup rule based on cached P31 types.

    Checks if an intermediate administrative level (sub-part) should be skipped
    if its parent is a major admin type. Assumes P31 types for relevant QIDs
    have been prefetched into _p31_cache.

    Args:
        doc: The MongoDB document potentially needing the fix.
        collection: The MongoDB collection (used for fallback P31 lookup if cache misses).
        config: The application configuration dictionary.

    Returns:
        An update operation dictionary {'$set': {...}} if changes are needed or
        if the document is resolved (even if unchanged), otherwise None if already resolved.
    """
    global _cache_hits, _cache_misses  # Allow modifying cache stats
    doc_id = doc.get("_id")
    if not doc_id:
        logger.warning("Skipping document without _id during ontology fix.")
        return None

    # Skip if already marked as resolved by a previous run
    if doc.get("ontology_flag_resolved", False):
        return None

    original_hierarchy = doc.get("admin_hierarchy")
    # Ensure hierarchy is a list and has at least two levels for the pattern to apply
    if not isinstance(original_hierarchy, list) or len(original_hierarchy) < 2:
        logger.debug(
            f"Marking {doc_id} as resolved: Hierarchy absent or too short ({len(original_hierarchy) if isinstance(original_hierarchy, list) else 'N/A'} levels)."
        )
        return {"$set": {"ontology_flag_resolved": True}}

    # Load rule QIDs from config or use defaults
    major_admin_types = set(
        pydash_get(config, "ontology_fix.major_admin_types", DEFAULT_MAJOR_ADMIN_TYPES)
    )
    sub_part_types = set(
        pydash_get(config, "ontology_fix.sub_part_types", DEFAULT_SUB_PART_TYPES)
    )

    if not major_admin_types or not sub_part_types:
        logger.warning(
            "Ontology fix rule QID lists (major_admin_types or sub_part_types) are empty in config. Skipping fix logic."
        )
        # Mark as resolved because the rules can't be applied anyway
        return {"$set": {"ontology_flag_resolved": True}}

    modified = False
    new_hierarchy: List[Dict[str, Any]] = []

    # --- Apply Rule Logic ---
    # Iterate up to the second-to-last element to always have a parent to check
    for i in range(len(original_hierarchy)):
        current_level_data = original_hierarchy[i]
        current_qid = current_level_data.get("qid")

        # Basic validation of hierarchy entry structure
        if (
            not isinstance(current_level_data, dict)
            or not current_qid
            or not isinstance(current_qid, str)
        ):
            logger.warning(
                f"Invalid hierarchy entry at index {i} for doc {doc_id}: {current_level_data}. Keeping entry."
            )
            new_hierarchy.append(
                current_level_data
            )  # Keep invalid entries? Or skip? Keeping for now.
            continue

        # Check if this is the last level
        if i + 1 >= len(original_hierarchy):
            new_hierarchy.append(current_level_data)  # Always keep the last level
            break

        parent_level_data = original_hierarchy[i + 1]
        parent_qid = parent_level_data.get("qid")

        if (
            not isinstance(parent_level_data, dict)
            or not parent_qid
            or not isinstance(parent_qid, str)
        ):
            logger.warning(
                f"Invalid parent hierarchy entry at index {i+1} for doc {doc_id}: {parent_level_data}. Keeping current entry '{current_qid}'."
            )
            new_hierarchy.append(
                current_level_data
            )  # Keep current if parent is invalid
            continue

        # Get P31 types from cache (or fallback DB lookup)
        # Use the helper function that handles cache logic
        current_p31 = _get_p31_from_cache_or_db(current_qid, collection)
        parent_p31 = _get_p31_from_cache_or_db(parent_qid, collection)

        # --- Apply the Specific Rule ---
        # Check if current QID is an instance of any "sub-part" type AND
        # check if the parent QID is an instance of any "major admin" type.
        is_sub_part = bool(current_p31.intersection(sub_part_types))
        is_major_parent = bool(parent_p31.intersection(major_admin_types))

        if is_sub_part and is_major_parent:
            # Pattern matched: Skip the current level (the sub-part)
            modified = True
            logger.info(
                f"Ontology Fix Applied for {doc_id}: Skipping intermediate level {i} (QID: {current_qid}, Label: '{current_level_data.get('label')}') because parent {parent_qid} is a major admin type."
            )
            # Don't append current_level_data to new_hierarchy
        else:
            # Pattern not matched, keep the current level
            new_hierarchy.append(current_level_data)

    # --- Construct Final Update Operation ---
    if modified:
        # Re-calculate levels based on position in the new list
        for idx, level_data in enumerate(new_hierarchy):
            if isinstance(level_data, dict):  # Double-check type before assigning
                level_data["level"] = idx
        logger.debug(f"Final modified hierarchy for {doc_id}: {new_hierarchy}")
        return {
            "$set": {"admin_hierarchy": new_hierarchy, "ontology_flag_resolved": True}
        }
    else:
        # No modification needed based on the rule, but mark as checked and resolved
        logger.debug(
            f"Marking {doc_id} as resolved: Checked hierarchy, no ontology fix pattern found."
        )
        return {"$set": {"ontology_flag_resolved": True}}


def process_ontology_batch(
    collection: Collection, config: Dict[str, Any]
) -> Tuple[int, int, int]:
    """Processes documents for ontology flaw fixing using batching and P31 prefetching."""
    global _p31_cache, _cache_hits, _cache_misses  # Access global cache and stats

    # Query: Find documents not yet resolved, that have a hierarchy with at least 2 levels
    query = {
        "ontology_flag_resolved": {"$ne": True},
        "admin_hierarchy": {
            "$exists": True,
            "$type": "array",
            "$not": {"$size": 0},
            "$not": {"$size": 1},
        },
    }
    # Fetch the whole document as hierarchy and P31 lookups are needed
    projection = None  # Fetch full document

    batch_size = pydash_get(
        config, "mongodb.batch_size", 500
    )  # Keep smaller for this potentially complex task
    processed_count = 0
    fixed_count = 0  # Count where hierarchy was actually changed
    resolved_count = 0  # Count where flag was set (fixed + checked_ok)
    error_count = 0
    start_time = time.time()
    last_log_time = start_time

    logger.info("Starting ontology fix batch processing...")
    logger.debug(f"Using query: {query}")

    try:
        total_docs_to_process = collection.count_documents(query, maxTimeMS=30000)
        logger.info(
            f"Found approximately {total_docs_to_process:,} entities potentially needing ontology fix check."
        )
    except Exception as e:
        logger.warning(f"Could not get exact count for ontology fix candidates: {e}")
        total_docs_to_process = "?"  # Indicate unknown count

    cursor = collection.find(query, projection, no_cursor_timeout=True)
    batch_docs: List[Dict[str, Any]] = []
    batch_qids_needed_for_p31: Set[str] = set()

    try:  # Wrap cursor iteration in try/finally
        for doc in cursor:
            batch_docs.append(doc)
            # Collect all unique QIDs in this hierarchy for P31 prefetching
            hierarchy = doc.get("admin_hierarchy")
            if isinstance(hierarchy, list):
                for level in hierarchy:
                    if isinstance(level, dict):
                        qid = level.get("qid")
                        if qid and isinstance(qid, str):  # Ensure QID is valid string
                            batch_qids_needed_for_p31.add(qid)

            # Process when batch is full
            if len(batch_docs) >= batch_size:
                # Reset batch cache stats
                _p31_cache.clear()
                _cache_hits = 0
                _cache_misses = 0
                logger.info(
                    f"Processing batch of {len(batch_docs)} documents (Total processed: {processed_count:,})..."
                )
                batch_start_time = time.time()

                # Prefetch P31 data for QIDs needed in this batch
                prefetch_p31_types(batch_qids_needed_for_p31, collection)

                batch_updates: List[UpdateOne] = []
                for batch_doc in batch_docs:
                    processed_count += 1
                    update_op = apply_ontology_fix(batch_doc, collection, config)
                    if (
                        update_op
                    ):  # Will contain $set if fix applied or resolution marked
                        batch_updates.append(
                            UpdateOne({"_id": batch_doc["_id"]}, update_op)
                        )
                        resolved_count += 1
                        # Count only if hierarchy was actually changed
                        if "admin_hierarchy" in update_op.get("$set", {}):
                            fixed_count += 1

                # Write updates for this batch
                write_errors_batch = 0
                if batch_updates:
                    try:
                        logger.debug(
                            f"Writing {len(batch_updates)} ontology fixes/resolutions for batch..."
                        )
                        result = collection.bulk_write(batch_updates, ordered=False)
                        logger.debug(
                            f"Ontology fix bulk write result: modified={result.modified_count}, upserted={result.upserted_count}"
                        )
                    except BulkWriteError as bwe:
                        write_errors_batch = len(bwe.details.get("writeErrors", []))
                        logger.error(
                            f"Bulk write error during ontology fix batch ({write_errors_batch} errors): {bwe.details}",
                            exc_info=False,
                        )
                        error_count += write_errors_batch
                    except Exception as e:
                        write_errors_batch = len(batch_updates)  # Assume all failed
                        logger.error(
                            f"Unexpected error during ontology fix bulk write: {e}",
                            exc_info=True,
                        )
                        error_count += write_errors_batch

                batch_duration = time.time() - batch_start_time
                logger.info(
                    f"Finished processing batch ({len(batch_docs)} docs) in {batch_duration:.2f}s. Cache Hits: {_cache_hits}, Misses: {_cache_misses}, Write Errors: {write_errors_batch}."
                )

                # Clear batch containers for next iteration
                batch_docs = []
                batch_qids_needed_for_p31 = set()

                # Log overall progress
                current_time = time.time()
                if current_time - last_log_time > 60:
                    elapsed = current_time - start_time
                    rate = processed_count / elapsed if elapsed > 0 else 0
                    logger.info(
                        f"Overall progress: {processed_count:,}/{total_docs_to_process} entities ({fixed_count:,} fixed, {resolved_count:,} resolved) in {elapsed:.2f}s ({rate:.2f}/sec). Total Errors: {error_count}"
                    )
                    last_log_time = current_time

        # Process the final partial batch
        if batch_docs:
            logger.info(f"Processing final batch of {len(batch_docs)} documents...")
            batch_start_time = time.time()
            _p31_cache.clear()
            _cache_hits = 0
            _cache_misses = 0
            prefetch_p31_types(batch_qids_needed_for_p31, collection)

            batch_updates = []
            for batch_doc in batch_docs:
                processed_count += 1
                update_op = apply_ontology_fix(batch_doc, collection, config)
                if update_op:
                    batch_updates.append(
                        UpdateOne({"_id": batch_doc["_id"]}, update_op)
                    )
                    resolved_count += 1
                    if "admin_hierarchy" in update_op.get("$set", {}):
                        fixed_count += 1

            write_errors_batch = 0
            if batch_updates:
                try:
                    logger.debug(
                        f"Writing {len(batch_updates)} ontology fixes/resolutions for final batch..."
                    )
                    result = collection.bulk_write(batch_updates, ordered=False)
                    logger.debug(
                        f"Ontology fix final bulk write result: modified={result.modified_count}, upserted={result.upserted_count}"
                    )
                except BulkWriteError as bwe:
                    write_errors_batch = len(bwe.details.get("writeErrors", []))
                    logger.error(
                        f"Bulk write error during final ontology fix batch ({write_errors_batch} errors): {bwe.details}",
                        exc_info=False,
                    )
                    error_count += write_errors_batch
                except Exception as e:
                    write_errors_batch = len(batch_updates)
                    logger.error(
                        f"Unexpected error during final ontology fix bulk write: {e}",
                        exc_info=True,
                    )
                    error_count += write_errors_batch

            batch_duration = time.time() - batch_start_time
            logger.info(
                f"Finished processing final batch ({len(batch_docs)} docs) in {batch_duration:.2f}s. Cache Hits: {_cache_hits}, Misses: {_cache_misses}, Write Errors: {write_errors_batch}."
            )

    finally:
        # Ensure cursor is closed
        if isinstance(cursor, Iterator) and hasattr(cursor, "close"):
            try:
                cursor.close()
            except Exception as cursor_e:
                logger.warning(f"Error closing MongoDB cursor: {cursor_e}")

    # Final summary
    elapsed = time.time() - start_time
    logger.info("-" * 50)
    logger.info("Ontology fix process finished.")
    logger.info(f"Total time: {elapsed:.2f} seconds")
    logger.info(
        f"Total entities processed (candidates checked): {processed_count:,} (out of ~{total_docs_to_process})"
    )
    logger.info(f"Entities with hierarchy potentially fixed: {fixed_count:,}")
    logger.info(f"Total entities marked as resolved: {resolved_count:,}")
    logger.info(f"Write errors encountered: {error_count:,}")
    logger.info("-" * 50)
    return processed_count, fixed_count, error_count


def main():
    parser = argparse.ArgumentParser(
        description="Apply specific ontology fixes (e.g., P131 hierarchy) to Wikidata entities."
    )
    parser.add_argument(
        "--config",
        default="config/config.yaml",
        help="Path to the global YAML configuration file.",
    )
    # Add arguments for rule QID lists if moved out of defaults/config

    args = parser.parse_args()

    config: Optional[Dict[str, Any]] = None
    try:
        config = load_config(config_path=args.config)
        setup_logging(config)

        # Check if fix should run based on config (pipeline runner handles 'enabled' field)
        run_fix_explicitly = (
            pydash_get(config, "wikidata_ingestion.apply_ontology_fix_on_ingest", False)
            == False
        )
        if not run_fix_explicitly:
            logger.info(
                "Skipping ontology fix script: Config indicates fix applied during ingest ('apply_ontology_fix_on_ingest': true)."
            )
            sys.exit(0)

        collection = get_entity_collection(config)
        process_ontology_batch(collection, config)

    except (
        FileNotFoundError,
        ConfigurationError,
        ConnectionFailure,
        MongoConfigError,
    ) as e:
        logger.critical(f"Ontology fix script failed: {e}", exc_info=True)
        sys.exit(1)
    except Exception as e:
        logger.critical(
            f"An unexpected error occurred during ontology fix: {e}", exc_info=True
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
