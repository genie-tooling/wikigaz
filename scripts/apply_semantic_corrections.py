#!/usr/bin/env python3
import logging
import argparse
import sys
import yaml
import os
import time
from typing import Dict, Any, List, Optional, Iterator, Set, Tuple  # Added Set, Tuple
from pymongo.collection import Collection
from pymongo.errors import ConnectionFailure, ConfigurationError, BulkWriteError
from pymongo import UpdateOne  # Import UpdateOne

# Make utils discoverable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from utils.config import load_config, get_config, pydash_get, ConfigurationError
from utils.logging_config import setup_logging
from utils.mongo_helpers import get_entity_collection

logger = logging.getLogger(__name__)

# Cache for loaded rules to avoid repeated file I/O
_class_rules: Optional[Dict[str, Any]] = None


def load_class_mapping_rules(config: Dict[str, Any]) -> Dict[str, Any]:
    """Loads the class assignment rules from the YAML file specified in config."""
    global _class_rules
    if _class_rules is not None:
        return _class_rules

    # Default path can be set here or rely purely on config
    default_rules_path = "config/class_mapping_rules.yaml"  # Relative to project root
    rules_file_rel = pydash_get(
        config, "semantic_correction.class_assignment_rules_file", default_rules_path
    )

    if not rules_file_rel:
        logger.warning(
            "No class assignment rules file specified or found in config. Cannot apply 'most_specific_known' strategy effectively."
        )
        _class_rules = {}  # Cache empty rules
        return _class_rules

    # Config loader should absolutize path, but check again
    rules_file_abs = rules_file_rel
    if not os.path.isabs(rules_file_abs):
        try:
            from utils.config import get_base_dir

            base_dir = get_base_dir()
            rules_file_abs = os.path.abspath(os.path.join(base_dir, rules_file_rel))
        except ImportError:
            rules_file_abs = os.path.abspath(rules_file_rel)  # Fallback

    if not os.path.exists(rules_file_abs):
        logger.error(f"Class assignment rules file not found: {rules_file_abs}")
        logger.error(
            "Please create this file or update 'semantic_correction.class_assignment_rules_file' in config."
        )
        _class_rules = {}
        # This is fatal for 'most_specific_known' strategy
        raise FileNotFoundError(f"Class rules file not found: {rules_file_abs}")

    try:
        with open(rules_file_abs, "r", encoding="utf-8") as f:
            rules = yaml.safe_load(f)
        _class_rules = rules or {}  # Handle empty file case
        logger.info(f"Loaded class assignment rules from {rules_file_abs}")

        # Basic validation
        if "preference_order" not in _class_rules and "hierarchy" not in _class_rules:
            logger.warning(
                f"Class rules file {rules_file_abs} seems empty or missing expected keys ('preference_order' or 'hierarchy'). 'most_specific_known' strategy may not work."
            )
        elif "preference_order" in _class_rules and not isinstance(
            _class_rules["preference_order"], list
        ):
            logger.error(
                f"Invalid format in {rules_file_abs}: 'preference_order' should be a list of QIDs."
            )
            _class_rules = {}  # Invalidate rules
            raise ConfigurationError(
                "Invalid format for 'preference_order' in class rules file."
            )
        # Add more validation for hierarchy structure if that strategy is implemented

        return _class_rules
    except yaml.YAMLError as e:
        logger.error(f"Error parsing class assignment rules file {rules_file_abs}: {e}")
        _class_rules = {}
        raise ConfigurationError(f"Error parsing class rules file: {e}")
    except Exception as e:
        logger.error(
            f"Unexpected error loading class rules file {rules_file_abs}: {e}",
            exc_info=True,
        )
        _class_rules = {}
        raise


def apply_class_assignment(doc: Dict[str, Any], rules: Dict[str, Any]) -> Optional[str]:
    """
    Applies the 'most_specific_known' class assignment strategy based on loaded rules.
    Currently implements the 'preference_order' list strategy.
    """
    instance_of_qids = doc.get("instance_of", [])
    if not isinstance(instance_of_qids, list) or not rules:
        logger.debug(
            f"Cannot assign class for {doc.get('_id')}: Invalid or missing 'instance_of' field, or no rules loaded."
        )
        return None

    # Strategy 1: Simple Preference List (Lower index = higher preference)
    if "preference_order" in rules:
        preference_order: List[str] = rules.get("preference_order", [])
        if not isinstance(preference_order, list):
            logger.warning(
                "preference_order in rules is not a list. Skipping class assignment."
            )
            return None

        best_match_qid: Optional[str] = None
        # Use infinity to ensure any found index is lower
        lowest_index = float("inf")

        for qid in instance_of_qids:
            if not isinstance(qid, str):  # Ensure QID is a string
                continue
            try:
                # Find index in preference list
                index = preference_order.index(qid)
                if index < lowest_index:
                    lowest_index = index
                    best_match_qid = qid
            except ValueError:
                continue  # QID not in preference list

        if best_match_qid:
            logger.debug(
                f"Class assigned for {doc.get('_id')}: {best_match_qid} (index {lowest_index}) from instances {instance_of_qids}"
            )
            return best_match_qid
        else:
            logger.debug(
                f"No preferred class found for {doc.get('_id')} among instances {instance_of_qids} based on rules."
            )
            return None

    # Strategy 2: Hierarchy (More complex - requires script changes)
    elif "hierarchy" in rules:
        logger.warning("Hierarchy-based class assignment not implemented yet.")
        return None  # Fallback

    else:
        logger.warning(
            "Class assignment rules file has unknown format. Cannot apply strategy."
        )
        return None


def find_entities_for_correction(
    collection: Collection, config: Dict[str, Any]
) -> Iterator[Dict[str, Any]]:
    """Finds entities needing semantic correction based on chosen strategy."""
    class_strategy = pydash_get(config, "semantic_correction.class_assignment_strategy")

    # Query: Find entities that haven't had class assignment applied yet.
    # Also ensure instance_of field exists and is an array for the strategy to work.
    query = {
        "corrected_class": {"$exists": False},
        "instance_of": {"$exists": True, "$type": "array", "$ne": []},
    }

    # Projection: Fetch fields needed for the specific correction(s) being applied
    projection = {
        "_id": 1,
        "instance_of": 1,
    }  # corrected_class is implicitly needed by query

    logger.info(
        f"Querying MongoDB for entities needing semantic corrections (strategy: {class_strategy})..."
    )
    logger.debug(f"Using query: {query}, projection: {projection}")
    cursor = collection.find(query, projection, no_cursor_timeout=True)
    try:
        count = collection.count_documents(query, maxTimeMS=30000)
        logger.info(f"Found approximately {count:,} candidate entities for correction.")
    except Exception as e:
        logger.warning(f"Could not get exact count for correction candidates: {e}")
        logger.info("Proceeding with cursor iteration...")
    return cursor


def process_correction_batch(
    collection: Collection, config: Dict[str, Any]
) -> Tuple[int, int, int]:
    """Processes a batch of documents for semantic corrections."""
    class_strategy = pydash_get(config, "semantic_correction.class_assignment_strategy")
    rules = {}
    apply_class_correction = False

    if class_strategy == "most_specific_known":
        try:
            rules = load_class_mapping_rules(config)
            if (
                rules and "preference_order" in rules
            ):  # Only apply if rules loaded and have expected key
                apply_class_correction = True
                logger.info("Applying 'most_specific_known' class assignment strategy.")
            else:
                logger.warning(
                    "Cannot apply 'most_specific_known' class strategy: Rules empty or invalid format (missing 'preference_order')."
                )
        except (FileNotFoundError, ConfigurationError) as e:
            logger.error(
                f"Cannot apply 'most_specific_known' class strategy due to rule loading error: {e}"
            )
            # Stop processing if rules are required but failed to load
            raise
    elif class_strategy:
        logger.warning(
            f"Semantic correction strategy '{class_strategy}' is configured but not implemented. No corrections applied."
        )
    else:
        logger.info(
            "No semantic correction strategy specified or 'most_specific_known' rules invalid. Skipping."
        )

    # Return early if no corrections are being applied
    if (
        not apply_class_correction
    ):  # Add checks for other correction types if implemented
        logger.info(
            "No semantic corrections enabled or configured correctly. Exiting processing."
        )
        return 0, 0, 0  # Processed, Updated, Errors

    batch_size = pydash_get(config, "mongodb.batch_size", 1000)
    updates: List[UpdateOne] = []
    processed_count = 0
    updated_count = 0
    error_count = 0
    start_time = time.time()
    last_log_time = start_time

    entity_iterator = find_entities_for_correction(collection, config)

    logger.info(f"Starting semantic correction batch processing...")

    for doc in entity_iterator:
        processed_count += 1
        doc_id = doc["_id"]
        update_ops = {}
        needs_update = False

        # --- Apply Class Assignment Correction ---
        if apply_class_correction:
            # Query ensures corrected_class doesn't exist, no need to check again
            corrected_class_qid = apply_class_assignment(doc, rules)
            # Update field, setting it to null if no class determined by rules
            update_ops["$set"] = update_ops.get("$set", {})
            update_ops["$set"]["corrected_class"] = corrected_class_qid
            needs_update = True
            if corrected_class_qid is not None:
                updated_count += 1
                logger.debug(
                    f"Setting corrected_class for {doc_id}: '{corrected_class_qid}'"
                )
            else:
                logger.debug(
                    f"Setting corrected_class for {doc_id}: null (no match found)"
                )

        # --- Apply Other Corrections (if any) ---
        # Add logic here if other corrections are implemented

        # --- Prepare update operation ---
        if needs_update:
            # Use UpdateOne for clarity, although filter/update structure matches ReplaceOne
            updates.append(
                UpdateOne(
                    filter={"_id": doc_id},
                    update=update_ops
                    # upsert=False # We are only updating existing documents based on the query
                )
            )

        # --- Perform bulk write periodically ---
        if len(updates) >= batch_size:
            write_errors = 0
            try:
                logger.debug(f"Writing batch of {len(updates)} semantic corrections...")
                result = collection.bulk_write(updates, ordered=False)
                logger.debug(
                    f"Successfully wrote batch (modified: {result.modified_count})."
                )
                # We track updated_count based on logic, not bulk_write result
            except BulkWriteError as bwe:
                write_errors = len(bwe.details.get("writeErrors", []))
                logger.error(
                    f"Bulk write error during corrections ({write_errors} errors): {bwe.details}",
                    exc_info=False,
                )
                error_count += write_errors
            except Exception as e:
                write_errors = len(updates)  # Assume all failed on unexpected error
                logger.error(f"Unexpected error during bulk write: {e}", exc_info=True)
                error_count += write_errors
            finally:
                updates = []  # Clear batch regardless of success/failure

            # Log progress
            current_time = time.time()
            if current_time - last_log_time > 30:
                elapsed = current_time - start_time
                rate = processed_count / elapsed if elapsed > 0 else 0
                logger.info(
                    f"Processed {processed_count:,} entities ({updated_count:,} updated with class) in {elapsed:.2f}s ({rate:.2f}/sec). Write Errors: {error_count}"
                )
                last_log_time = current_time

    # Write any remaining updates
    if updates:
        write_errors = 0
        try:
            logger.debug(f"Writing final batch of {len(updates)} corrections...")
            result = collection.bulk_write(updates, ordered=False)
            logger.debug(
                f"Successfully wrote final batch (modified: {result.modified_count})."
            )
        except BulkWriteError as bwe:
            write_errors = len(bwe.details.get("writeErrors", []))
            logger.error(
                f"Bulk write error during final corrections batch ({write_errors} errors): {bwe.details}",
                exc_info=False,
            )
            error_count += write_errors
        except Exception as e:
            write_errors = len(updates)
            logger.error(
                f"Unexpected error during final bulk write: {e}", exc_info=True
            )
            error_count += write_errors

    # Final summary
    elapsed = time.time() - start_time
    logger.info("-" * 50)
    logger.info("Semantic correction process finished.")
    logger.info(f"Total time: {elapsed:.2f} seconds")
    logger.info(f"Total entities processed (candidates): {processed_count:,}")
    logger.info(f"Entities updated with a class QID: {updated_count:,}")
    logger.info(f"Write errors encountered: {error_count:,}")
    logger.info("-" * 50)

    # Close cursor if necessary (though iteration should exhaust it)
    if isinstance(entity_iterator, Iterator):
        try:
            if hasattr(entity_iterator, "close"):
                entity_iterator.close()  # type: ignore
        except Exception:
            pass  # Ignore errors closing cursor

    return processed_count, updated_count, error_count


def main():
    parser = argparse.ArgumentParser(
        description="Apply semantic corrections to Wikidata entities in MongoDB."
    )
    parser.add_argument(
        "--config",
        default="config/config.yaml",
        help="Path to the global YAML configuration file.",
    )
    parser.add_argument(
        "--class-strategy",
        default=None,
        help="Override class assignment strategy from config.",
    )
    parser.add_argument(
        "--rules-file",
        default=None,
        help="Override path to class mapping rules YAML file.",
    )

    args = parser.parse_args()

    config = None
    try:
        config = load_config(config_path=args.config)
        setup_logging(config)

        # Apply overrides from args
        if args.class_strategy:
            from pydash import set_ as pydash_set

            pydash_set(
                config,
                "semantic_correction.class_assignment_strategy",
                args.class_strategy,
            )
            logger.info(f"Overriding class strategy to: {args.class_strategy}")
        if args.rules_file:
            from pydash import set_ as pydash_set

            pydash_set(
                config,
                "semantic_correction.class_assignment_rules_file",
                args.rules_file,
            )
            logger.info(f"Overriding rules file path to: {args.rules_file}")

        collection = get_entity_collection(config)
        process_correction_batch(collection, config)

    except (FileNotFoundError, ConfigurationError, ConnectionFailure) as e:
        logger.critical(
            f"Semantic correction script failed due to setup error: {e}", exc_info=True
        )
        sys.exit(1)
    except Exception as e:
        logger.critical(f"An unexpected error occurred: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
