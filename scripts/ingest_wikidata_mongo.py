#!/usr/bin/env python3
import argparse
import bz2
import ijson  # type: ignore
import logging
import sys
import os
import time
from typing import (
    Dict,
    Any,
    List,
    Optional,
    Generator,
    Tuple,
    Callable,
)  # Added Callable
from pymongo.errors import BulkWriteError, ConnectionFailure, ConfigurationError
from pymongo.operations import ReplaceOne
from datetime import datetime, timezone  # Added timezone

# Make utils discoverable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from utils.config import load_config, get_config, pydash_get, get_base_dir
from utils.logging_config import setup_logging
from utils.mongo_helpers import (
    get_entity_collection,
    get_minimal_item_data_for_hierarchy,
)
from utils.wikidata_helpers import (
    filter_wikidata_item,
    extract_entity_data,
    GetItemDataFunc,
    # We need to define the actual data fetching function here or import it
)


logger = logging.getLogger(__name__)

# --- Batch Prefetching Implementation for Hierarchy Traversal ---
# Cache for item data fetched within a batch processing cycle
_batch_item_cache: Dict[str, Optional[Dict[str, Any]]] = {}


def _get_item_data_batch_fetcher(qids: List[str], collection: Collection) -> None:
    """
    Fetches data for multiple QIDs needed for hierarchy and caches it.
    This function populates the _batch_item_cache.
    """
    global _batch_item_cache
    qids_to_fetch = [qid for qid in qids if qid not in _batch_item_cache]
    if not qids_to_fetch:
        return

    logger.debug(f"Batch Prefetcher: Fetching data for {len(qids_to_fetch)} QIDs...")
    try:
        # Fetch necessary fields: labels, P131 claims
        projection = {
            "_id": 1,
            "labels.en.value": 1,  # Assuming English label needed
            "claims.P131.mainsnak.datavalue.value.id": 1,
        }
        cursor = collection.find({"_id": {"$in": qids_to_fetch}}, projection)
        found_qids = set()
        for doc in cursor:
            doc_id = doc["_id"]
            _batch_item_cache[doc_id] = doc  # Store fetched doc
            found_qids.add(doc_id)

        # Mark missing QIDs as None in cache to prevent re-fetching in this batch
        missing_qids = set(qids_to_fetch) - found_qids
        for qid in missing_qids:
            _batch_item_cache[qid] = None  # Explicitly mark as not found
        logger.debug(
            f"Batch Prefetcher: Fetched {len(found_qids)} docs, marked {len(missing_qids)} as missing."
        )

    except Exception as e:
        logger.error(
            f"Batch Prefetcher: Error fetching data for QIDs {qids_to_fetch}: {e}"
        )
        # Mark all requested as None on error to avoid retries in this batch
        for qid in qids_to_fetch:
            _batch_item_cache[qid] = None


def _get_item_data_from_cache(qid: str) -> Optional[Dict[str, Any]]:
    """
    Retrieves item data from the batch cache.
    This function will be passed to extract_admin_hierarchy.
    """
    global _batch_item_cache
    # Returns the cached dictionary or None if QID was fetched and not found/error occurred
    return _batch_item_cache.get(qid)


# --- End Batch Prefetching Implementation ---


def stream_wikidata_dump(dump_path: str) -> Generator[Dict[str, Any], None, None]:
    """
    Streams Wikidata items from a bz2 compressed JSON dump.

    Args:
        dump_path: Path to the bz2 compressed Wikidata JSON dump file.

    Yields:
        Dictionaries representing individual Wikidata items ('item').
    """
    logger.info(f"Starting to stream Wikidata dump: {dump_path}")
    processed_lines = 0
    start_time = time.monotonic()
    try:
        with bz2.open(dump_path, "rb") as f:
            # Use ijson.items to parse the stream item by item
            # Adjust prefix if needed (e.g., 'item' assumes root is array of objects)
            parser = ijson.items(f, "item")
            for item in parser:
                processed_lines += 1
                if processed_lines % 100000 == 0:
                    elapsed = time.monotonic() - start_time
                    rate = processed_lines / elapsed if elapsed > 0 else 0
                    logger.info(
                        f"Streamed {processed_lines:,} items ({rate:.2f} items/sec)..."
                    )

                # Ensure item is a dictionary before yielding
                if isinstance(item, dict):
                    yield item
                else:
                    logger.warning(
                        f"Skipping non-dictionary item found in stream at line approx {processed_lines}"
                    )

    except FileNotFoundError:
        logger.error(f"Wikidata dump file not found at: {dump_path}")
        raise
    except ImportError:
        logger.critical(
            "The 'ijson' library is required but not installed. Please install it (pip install ijson)."
        )
        raise
    except Exception as e:
        logger.error(
            f"Error streaming or parsing Wikidata dump {dump_path} around line {processed_lines}: {e}",
            exc_info=True,
        )
        raise  # Reraise to signal failure

    elapsed = time.monotonic() - start_time
    logger.info(
        f"Finished streaming Wikidata dump. Processed {processed_lines:,} items in {elapsed:.2f} seconds."
    )


def process_and_ingest_batch(
    items: List[Dict[str, Any]], config: Dict[str, Any]
) -> Tuple[int, int]:
    """
    Processes a batch of Wikidata items and ingests them into MongoDB.
    Includes batch prefetching for hierarchy data.

    Args:
        items: A list of raw Wikidata item dictionaries.
        config: The application configuration.

    Returns:
        A tuple (successful_inserts_or_updates, failed_or_filtered_items).
    """
    global _batch_item_cache  # Access the batch cache
    if not items:
        return 0, 0

    collection = get_entity_collection(config)
    operations: List[ReplaceOne] = []
    processed_count = 0  # Items that passed initial filter and started processing
    filtered_out_count = 0  # Items skipped by filter_wikidata_item
    failed_extraction_count = 0  # Items failing during extract_entity_data
    successful_write_count = 0
    start_time_batch = time.monotonic()

    # --- Step 1: Prefetch hierarchy data for the entire batch ---
    _batch_item_cache.clear()  # Clear cache for the new batch
    qids_needed_for_hierarchy: Set[str] = set()
    valid_items_for_processing: List[Dict[str, Any]] = []

    # Initial filter and collect QIDs needed for P131 lookups within the batch
    for raw_item in items:
        if filter_wikidata_item(raw_item, config):
            valid_items_for_processing.append(raw_item)
            # Collect direct P131 targets from the item itself
            initial_p131_claims = raw_item.get("claims", {}).get("P131", [])
            for claim in initial_p131_claims:
                target_qid = pydash_get(claim, "mainsnak.datavalue.value.id")
                if pydash_get(claim, "mainsnak.snaktype") == "value" and target_qid:
                    qids_needed_for_hierarchy.add(target_qid)
        else:
            filtered_out_count += 1

    # Perform the batch fetch using the *entity collection* itself
    # Note: This assumes P131 targets are also entities being ingested or already present
    # in the target collection with necessary fields (labels, claims.P131).
    # If P131 targets might point outside the main collection, this strategy needs adjustment.
    if qids_needed_for_hierarchy:
        _get_item_data_batch_fetcher(list(qids_needed_for_hierarchy), collection)
    # --- End Prefetch ---

    # --- Step 2: Process each valid item using the cached data ---
    for raw_item in valid_items_for_processing:
        item_id = raw_item.get("id")
        if not item_id:  # Should not happen if filter passed, but check again
            logger.warning("Skipping item with no ID after filtering.")
            filtered_out_count += 1  # Treat as filtered
            continue

        # Extract and Transform Data using the cache lookup function
        try:
            # Pass the cache retriever function to extract_entity_data -> extract_admin_hierarchy
            get_data_func: GetItemDataFunc = _get_item_data_from_cache
            processed_data = extract_entity_data(raw_item, config, get_data_func)
            processed_count += 1  # Item passed filter and extraction attempt started

            # Add processing timestamp
            processed_data["last_updated"] = datetime.now(timezone.utc)

            # Prepare Bulk Write Operation (ReplaceOne with upsert)
            operations.append(
                ReplaceOne(
                    filter={"_id": item_id}, replacement=processed_data, upsert=True
                )
            )
        except Exception as e:
            logger.error(
                f"Failed to extract/transform data for item {item_id}: {e}",
                exc_info=True,
            )
            failed_extraction_count += 1
            continue  # Skip adding this item to bulk write operations

    # --- Step 3: Execute Bulk Write ---
    failed_write_count = 0
    if operations:
        try:
            logger.debug(f"Executing bulk write with {len(operations)} operations...")
            bulk_result = collection.bulk_write(operations, ordered=False)
            # Count successful operations (new inserts + matched/updated existing)
            successful_write_count = (
                bulk_result.upserted_count + bulk_result.matched_count
            )
            logger.debug(
                f"Bulk write result: Matched={bulk_result.matched_count}, Modified={bulk_result.modified_count}, Upserted={bulk_result.upserted_count}, Errors={len(bulk_result.write_errors) if bulk_result.write_errors else 0}"
            )

            if bulk_result.write_errors:
                failed_write_count = len(bulk_result.write_errors)
                # Log individual errors if needed (can be verbose)
                for error in bulk_result.write_errors:
                    logger.error(
                        f"Bulk write error: Index={error.get('index')}, Code={error.get('code')}, Msg='{error.get('errmsg', 'N/A')}'"
                    )

        except BulkWriteError as bwe:
            logger.error(
                f"Bulk write operation failed entirely: {bwe.details}", exc_info=True
            )
            failed_write_count = len(
                operations
            )  # Assume all failed if exception raised here
        except Exception as e:
            logger.error(
                f"An unexpected error occurred during bulk write: {e}", exc_info=True
            )
            failed_write_count = len(operations)  # Assume all failed

    duration_batch = time.monotonic() - start_time_batch
    total_failed_or_filtered = (
        filtered_out_count + failed_extraction_count + failed_write_count
    )
    logger.debug(
        f"Batch finished in {duration_batch:.4f}s. Filtered: {filtered_out_count}, Extract Failed: {failed_extraction_count}, Write OK: {successful_write_count}, Write Failed: {failed_write_count}"
    )

    # Return successful writes vs total initial items attempted in batch
    return successful_write_count, total_failed_or_filtered


def main():
    """Main execution function for Wikidata ingestion."""
    parser = argparse.ArgumentParser(
        description="Ingest Wikidata JSON dump into MongoDB.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--config",
        default="config/config.yaml",
        help="Path to the global YAML configuration file.",
    )
    parser.add_argument(
        "--dump-file",
        default=None,
        help="Path to Wikidata JSON dump (overrides config).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help="Batch size for MongoDB bulk writes (overrides config).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit processing to the first N items from the dump (for testing).",
    )

    args = parser.parse_args()

    total_items_streamed = 0
    total_successful_writes = 0
    total_failed_or_filtered = 0
    ingestion_start_time = time.monotonic()

    config: Optional[Dict[str, Any]] = None
    try:
        # --- Load Config and Setup ---
        base_dir = get_base_dir()  # Get project root
        config = load_config(config_path=args.config, base_dir=base_dir)
        setup_logging(config)

        # Apply overrides from command line
        batch_size = args.batch_size or pydash_get(
            config, "wikidata_ingestion.batch_size", 1000
        )
        limit = args.limit

        # Resolve dump file path
        dump_file_path = args.dump_file or pydash_get(
            config, "wikidata_ingestion.dump_filename"
        )
        if not dump_file_path:
            logger.critical(
                "Wikidata dump file path not configured in config or via --dump-file."
            )
            raise ConfigurationError("Wikidata dump file path not configured.")

        abs_dump_file_path = dump_file_path
        if not os.path.isabs(abs_dump_file_path):
            # Config loader should absolutize paths.data_dir
            data_dir = pydash_get(config, "paths.data_dir", "data")
            abs_dump_file_path = os.path.abspath(
                os.path.join(data_dir, abs_dump_file_path)
            )

        if not os.path.exists(abs_dump_file_path):
            logger.critical(f"Wikidata dump file not found: {abs_dump_file_path}")
            raise FileNotFoundError(
                f"Wikidata dump file not found: {abs_dump_file_path}"
            )

        logger.info(f"--- Starting Wikidata Ingestion ---")
        logger.info(f"Source Dump: {abs_dump_file_path}")
        logger.info(f"Batch Size: {batch_size}")
        if limit:
            logger.info(f"Processing Limit: {limit:,} items")

        # --- Get Collection Handle Once ---
        # (process_and_ingest_batch will reuse the global client/db)
        _ = get_entity_collection(config)

        # --- Process Stream ---
        item_stream = stream_wikidata_dump(abs_dump_file_path)
        batch: List[Dict[str, Any]] = []

        for item in item_stream:
            total_items_streamed += 1
            batch.append(item)

            if len(batch) >= batch_size:
                success_count, fail_count = process_and_ingest_batch(batch, config)
                total_successful_writes += success_count
                total_failed_or_filtered += fail_count
                # Log progress based on items streamed vs successfully written
                logger.info(
                    f"Streamed: {total_items_streamed:,} | Current Batch OK: {success_count}, Failed/Filt: {fail_count} | Total OK Writes: {total_successful_writes:,}"
                )
                batch = []  # Reset batch

            if limit is not None and total_items_streamed >= limit:
                logger.info(f"Reached processing limit of {limit:,} items.")
                break

        # Process the final batch if any items remain
        if batch:
            logger.info(f"Processing final batch of {len(batch)} items...")
            success_count, fail_count = process_and_ingest_batch(batch, config)
            total_successful_writes += success_count
            total_failed_or_filtered += fail_count
            logger.info(
                f"Final Batch: {len(batch)} items -> {success_count} success / {fail_count} failed/filtered"
            )

        ingestion_end_time = time.monotonic()
        duration = ingestion_end_time - ingestion_start_time
        logger.info(f"--- Wikidata Ingestion Finished ---")
        logger.info(f"Total Items Streamed: {total_items_streamed:,}")
        logger.info(f"Total Successfully Ingested/Updated: {total_successful_writes:,}")
        logger.info(
            f"Total Filtered/Failed (Extraction or Write): {total_failed_or_filtered:,}"
        )
        logger.info(f"Total Duration: {duration:.2f} seconds")

        if total_failed_or_filtered > 0:
            logger.warning(
                "Ingestion completed with some failures or filtered items. Check logs for details."
            )
            # Decide on exit code based on severity? For now, exit 0 unless critical error occurred.

    except (
        FileNotFoundError,
        ConfigurationError,
        ConnectionFailure,
        MongoConfigError,
    ) as e:
        logger.critical(f"Ingestion script failed: {e}", exc_info=True)
        sys.exit(1)
    except Exception as e:
        logger.critical(
            f"An unexpected error occurred during Wikidata ingestion: {e}",
            exc_info=True,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
