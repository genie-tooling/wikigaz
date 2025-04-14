#!/usr/bin/env python3
import argparse
import bz2
import ijson  # type: ignore[import]
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
    Set,  # Added Set
)
from pymongo.errors import BulkWriteError, ConnectionFailure, ConfigurationError
from pymongo.operations import ReplaceOne
from datetime import datetime, timezone  # Added timezone
from pymongo.collection import Collection
from pymongo.results import BulkWriteResult  # Added for type hint clarity

# Make utils discoverable
# Ensure this path is correct relative to where the script is run
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

try:
    from utils.config import (
        load_config,
        get_config,
        pydash_get,
        get_base_dir,
        ConfigurationError as ConfigUtilError,  # Alias utils' ConfigurationError
    )
    from utils.logging_config import setup_logging
    from utils.mongo_helpers import get_entity_collection
    from utils.wikidata_helpers import (
        filter_wikidata_item,
        extract_entity_data,
        GetItemDataFunc,
        _get_item_data_batch_fetcher,  # Import batch fetcher
        _get_item_data_from_cache,  # Import cache retriever
        _batch_item_cache,  # Import the actual cache dict
    )
except ImportError as e:
    # Basic logging/printing if utils cannot be imported
    print(f"ERROR: Failed to import utility modules: {e}", file=sys.stderr)
    print(
        "Ensure the script is run from the project root or PYTHONPATH is set correctly.",
        file=sys.stderr,
    )
    sys.exit(1)

logger = logging.getLogger(__name__)

# Note: The batch prefetching implementation using _batch_item_cache,
# _get_item_data_batch_fetcher, and _get_item_data_from_cache
# resides entirely within utils.wikidata_helpers.
# This script just calls the fetcher to populate the cache before processing a batch.


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
            # Assumes root is array of objects, each keyed 'item' might need adjustment
            # If the root is just the array, prefix should be 'item'
            # If root is {"items": [...]}, prefix might be "items.item"
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
        logger.critical(f"Wikidata dump file not found at: {dump_path}")
        raise
    except ImportError:
        logger.critical(
            "The 'ijson' library is required but not installed. Please install it (pip install ijson)."
        )
        raise
    except Exception as e:
        # Catch potential ijson parsing errors or bz2 errors
        logger.critical(
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
        A tuple (successful_writes, total_failed_or_filtered_this_batch).
    """
    global _batch_item_cache  # Access the batch cache defined in wikidata_helpers
    if not items:
        return 0, 0

    try:
        collection = get_entity_collection(config)
    except (ConfigUtilError, ConfigurationError, ConnectionFailure) as e:
        logger.error(f"Failed to get MongoDB collection for batch processing: {e}")
        # Indicate all items in this batch failed if connection is unavailable
        return 0, len(items)

    operations: List[ReplaceOne] = []
    processed_count = 0  # Items that passed initial filter and started processing
    filtered_out_count = 0  # Items skipped by filter_wikidata_item
    failed_extraction_count = 0  # Items failing during extract_entity_data
    start_time_batch = time.monotonic()

    # --- Step 1: Prefetch hierarchy data for the entire batch ---
    _batch_item_cache.clear()  # Clear cache for the new batch
    qids_needed_for_hierarchy: Set[str] = set()
    valid_items_for_processing: List[Dict[str, Any]] = []

    # Initial filter and collect QIDs needed for P131 lookups within the batch
    for raw_item in items:
        if filter_wikidata_item(raw_item, config):
            valid_items_for_processing.append(raw_item)
            # Collect direct P131 targets from the item itself for prefetching
            # Check if 'claims' and 'P131' exist and are structured as expected
            claims_p131 = raw_item.get("claims", {}).get("P131", [])
            if isinstance(claims_p131, list):
                for claim in claims_p131:
                    # Use safe access for potentially nested structure
                    target_qid = pydash_get(claim, "mainsnak.datavalue.value.id")
                    # Ensure it's a valid claim and we got a string QID
                    if (
                        pydash_get(claim, "mainsnak.snaktype") == "value"
                        and isinstance(target_qid, str)
                        and target_qid
                    ):
                        qids_needed_for_hierarchy.add(target_qid)
        else:
            filtered_out_count += 1

    # Perform the batch fetch using the *entity collection* itself
    if qids_needed_for_hierarchy:
        # Pass the list of QIDs and the collection handle
        _get_item_data_batch_fetcher(list(qids_needed_for_hierarchy), collection)
    # --- End Prefetch ---

    # --- Step 2: Process each valid item using the cached data ---
    for raw_item in valid_items_for_processing:
        item_id = raw_item.get("id")
        if not item_id:
            logger.warning("Skipping item with no ID after filtering.")
            filtered_out_count += 1 # Count as filtered since it can't be processed
            continue

        # Extract and Transform Data using the cache lookup function
        try:
            # Pass the cache retriever function from wikidata_helpers
            get_data_func: GetItemDataFunc = _get_item_data_from_cache
            processed_data = extract_entity_data(raw_item, config, get_data_func)
            processed_count += 1

            # Add processing timestamp
            processed_data["last_updated"] = datetime.now(timezone.utc)

            # Prepare Bulk Write Operation (ReplaceOne with upsert)
            # ReplaceOne completely replaces the document matching the filter, or inserts if not found.
            operations.append(
                ReplaceOne(
                    filter={"_id": item_id}, replacement=processed_data, upsert=True
                )
            )
        except Exception as e:
            # Catch errors during the complex extraction/transformation phase
            logger.error(
                f"Failed to extract/transform data for item {item_id}: {e}",
                exc_info=True, # Log full traceback for extraction errors
            )
            failed_extraction_count += 1
            continue # Skip adding this item to the bulk write operation

    # --- Step 3: Execute Bulk Write ---
    failed_write_count = 0
    successful_write_count = 0 # Initialize counter for successful writes
    if operations:
        bulk_result: Optional[BulkWriteResult] = None # Define for access in error handling
        try:
            logger.debug(f"Executing bulk write with {len(operations)} operations...")
            # ordered=False allows Mongo to process operations potentially in parallel
            # and continue even if some operations in the batch fail.
            bulk_result = collection.bulk_write(operations, ordered=False)

            # --- Correctly Check for Errors and Count Successes ---
            # Access errors safely using .get() on the raw result dict
            write_errors = bulk_result.bulk_api_result.get("writeErrors", [])
            failed_write_count = len(write_errors)

            if failed_write_count > 0:
                logger.error(
                   f"Bulk write completed with {failed_write_count} errors."
                )
                # Log details of *why* specific documents failed (e.g., schema validation)
                # Limit logged errors to avoid flooding?
                max_errors_to_log = 5
                for i, error in enumerate(write_errors):
                    if i >= max_errors_to_log:
                        logger.error(f"  (Plus {failed_write_count - max_errors_to_log} more errors...)")
                        break
                    failing_doc_id = error.get('op', {}).get('q', {}).get('_id', 'UNKNOWN_ID')
                    logger.error(
                        f"  - Write Error for Doc ID '{failing_doc_id}': Index={error.get('index')}, "
                        f"Code={error.get('code')}, Msg='{error.get('errmsg', 'N/A')}'"
                    )
                    # Optionally log the full error['errInfo'] for schema validation details if needed
                    # logger.error(f"    Error Info: {error.get('errInfo')}")


            # Calculate successful writes based on driver results
            # Matched means existing doc was replaced, Upserted means new doc was inserted.
            successful_write_count = bulk_result.upserted_count + bulk_result.matched_count

            # Log summary - Use calculated successful_write_count
            logger.debug(
                f"Bulk write result: Matched={bulk_result.matched_count}, Modified={bulk_result.modified_count}, "
                f"Upserted={bulk_result.upserted_count}, Successful (Matched+Upserted)={successful_write_count}, Errors={failed_write_count}"
            )

        except BulkWriteError as bwe:
            # Handles cases where the entire bulk write operation might fail at a lower level
            logger.error(f"Bulk write operation failed fundamentally: {bwe.details}", exc_info=True)
            # Try to get counts from details, but assume all failed if details are sparse
            failed_write_count = len(bwe.details.get("writeErrors", operations)) # Assume all failed if can't determine errors
            successful_write_count = bwe.details.get("nUpserted", 0) + bwe.details.get("nMatched", 0) # Best guess
        except AttributeError as ae:
             # Catch if bulk_result or bulk_api_result is unexpectedly None or missing keys
             logger.error(
                 f"AttributeError processing bulk write result: {ae}. Result object: {getattr(bulk_result, 'bulk_api_result', 'Result object missing')}",
                 exc_info=True
             )
             failed_write_count = len(operations) # Assume all failed if result parsing fails
             successful_write_count = 0
        except ConnectionFailure as ce:
            # Handle network/connection issues during the write
            logger.error(f"Connection failure during bulk write: {ce}", exc_info=True)
            failed_write_count = len(operations) # Assume all failed on connection error
            successful_write_count = 0
        except Exception as e:
            # Catch other unexpected errors during the write or result processing
            logger.error(
                f"An unexpected error occurred during bulk write or result processing: {e}", exc_info=True
            )
            failed_write_count = len(operations) # Assume all failed
            successful_write_count = 0

    # --- Calculation of total failed/filtered for the batch ---
    duration_batch = time.monotonic() - start_time_batch
    total_failed_or_filtered_this_batch = (
        filtered_out_count + failed_extraction_count + failed_write_count
    )

    # Log batch summary using calculated success/fail counts
    logger.debug(
        f"Batch finished in {duration_batch:.4f}s. Filtered: {filtered_out_count}, "
        f"Extract Failed: {failed_extraction_count}, Write OK: {successful_write_count}, "
        f"Write Failed: {failed_write_count}"
    )

    # --- Return calculated successful writes and total failures ---
    return successful_write_count, total_failed_or_filtered_this_batch


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
        setup_logging(config) # Setup logging based on loaded config

        # Apply overrides from command line
        # Use pydash_get for safe access with default
        batch_size = args.batch_size or int(pydash_get(
            config, "wikidata_ingestion.batch_size", 1000
        ))
        limit = args.limit

        # Resolve dump file path carefully
        dump_file_path_cfg = pydash_get(config, "wikidata_ingestion.dump_filename")
        dump_file_path_arg = args.dump_file
        dump_file_rel_path = dump_file_path_arg or dump_file_path_cfg

        if not dump_file_rel_path:
            logger.critical(
                "Wikidata dump file path not configured in config or via --dump-file."
            )
            raise ConfigUtilError("Wikidata dump file path not configured.")

        abs_dump_file_path = dump_file_rel_path
        if not os.path.isabs(abs_dump_file_path):
            # Config loader should have absolutized paths.data_dir
            data_dir = pydash_get(config, "paths.data_dir") # Expect absolute path
            if not data_dir:
                logger.critical("paths.data_dir not configured.")
                raise ConfigUtilError("paths.data_dir not configured.")
            if not os.path.isabs(data_dir): # Double check if config loader failed
                logger.warning(f"paths.data_dir '{data_dir}' was not absolute, resolving relative to base_dir.")
                data_dir = os.path.abspath(os.path.join(base_dir, data_dir))

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

        # --- Get Collection Handle Once (implicitly tests connection) ---
        _ = get_entity_collection(config) # Call once to ensure DB connection works upfront

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
                # Log progress using the returned counts
                # Use INFO level for periodic progress updates
                logger.info(
                    f"Streamed: {total_items_streamed:,} | "
                    f"Batch Writes OK: {success_count}, Failed/Filt: {fail_count} | "
                    f"Total Writes OK: {total_successful_writes:,}"
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
                f"Final Batch Result: Writes OK={success_count}, Failed/Filtered={fail_count}"
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
                "Ingestion completed with some failures or filtered items. Check logs for details (especially ERROR level)."
            )

    except (
        FileNotFoundError,
        ConfigUtilError, # Catch specific config error
        ConfigurationError, # Catch Mongo driver config error
        ConnectionFailure,
    ) as e:
        logger.critical(f"Ingestion script failed due to setup/connection error: {e}", exc_info=True)
        sys.exit(1)
    except Exception as e:
        logger.critical(
            f"An unexpected error occurred during Wikidata ingestion: {e}",
            exc_info=True,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
