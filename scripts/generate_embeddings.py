#!/usr/bin/env python3
import logging
import argparse
import sys
import os
import time
import numpy as np
from typing import Dict, Any, Optional, List, Iterator, Tuple

# Make utils discoverable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from utils.config import load_config, get_config, pydash_get, ConfigurationError
from utils.logging_config import setup_logging
from utils.mongo_helpers import get_entity_collection
from pymongo.collection import Collection
from pymongo import UpdateOne
from pymongo.errors import (
    ConnectionFailure,
    ConfigurationError as MongoConfigError,
    BulkWriteError,
)
from tqdm import tqdm
import bson  # For BSON binary format

# Try importing sentence-transformers, provide guidance if missing
try:
    from sentence_transformers import SentenceTransformer
except ImportError:
    logging.basicConfig(level=logging.INFO)  # Basic logging for early error
    logging.critical(
        "SentenceTransformers library not found. Please install it: poetry install"
    )
    sys.exit(1)

logger = logging.getLogger(__name__)


def load_embedding_model(config: Dict[str, Any]) -> Optional[SentenceTransformer]:
    """Loads the Sentence Transformer model specified in the config."""
    model_name = pydash_get(config, "embeddings.model_name")
    if not model_name:
        logger.error(
            "Embedding model name not specified in config (embeddings.model_name)."
        )
        return None
    try:
        logger.info(f"Loading Sentence Transformer model: {model_name}...")
        # Consider adding device configuration (e.g., 'cuda' if available)
        # model = SentenceTransformer(model_name, device='cuda')
        model = SentenceTransformer(model_name)
        logger.info("Embedding model loaded successfully.")
        return model
    except Exception as e:
        logger.error(
            f"Failed to load Sentence Transformer model '{model_name}': {e}",
            exc_info=True,
        )
        return None


def _construct_text_for_embedding(doc: Dict[str, Any], text_fields: List[str]) -> str:
    """Constructs the text input string from specified document fields."""
    combined_text_parts: List[str] = []
    doc_id = doc.get("_id", "UNKNOWN_ID")
    try:
        for field_path in text_fields:
            value = pydash_get(doc, field_path)
            # Handle lists (like aliases) and strings
            if isinstance(value, list):
                # Filter out non-strings or empty strings from list
                str_values = [str(v) for v in value if isinstance(v, str) and v]
                if str_values:
                    # Join list elements with space
                    combined_text_parts.append(" ".join(str_values))
            elif isinstance(value, str) and value:
                combined_text_parts.append(value)
            # Add handling for other types if needed (e.g., numbers?)
        # Join valid parts with a period and space. Use space if no valid parts found.
        combined_text = ". ".join(filter(None, combined_text_parts)) or " "
        # Truncate long texts if needed (models have input limits)
        # max_len = 512 # Example limit, check model documentation
        # if len(combined_text) > max_len:
        #    logger.debug(f"Truncating text for {doc_id} from {len(combined_text)} to {max_len} chars.")
        #    combined_text = combined_text[:max_len]
        return combined_text
    except Exception as e:
        logger.warning(f"Error constructing text for doc {doc_id}: {e}", exc_info=False)
        return " "  # Return default space on error


def _convert_to_bson_binary(embedding_np: np.ndarray) -> bson.Binary:
    """Converts a NumPy embedding array to BSON Binary format."""
    # Ensure float32, convert to bytes, wrap in BSON Binary subtype 0x00
    return bson.Binary(embedding_np.astype(np.float32).tobytes(), subtype=0x00)


def generate_and_store_embeddings(
    collection: Collection, model: SentenceTransformer, config: Dict[str, Any]
) -> Tuple[int, int]:
    """
    Generates embeddings for documents in MongoDB and stores them.
    Handles batching for fetching data and generating embeddings.

    Returns:
        Tuple (processed_count, total_error_count)
    """
    embeddings_enabled = pydash_get(config, "embeddings.enabled", False)
    if not embeddings_enabled:
        logger.info(
            "Skipping embedding generation as 'embeddings.enabled' is false in config."
        )
        return 0, 0

    text_fields: List[str] = pydash_get(config, "embeddings.text_fields_to_embed", [])
    if not text_fields or not isinstance(text_fields, list):
        logger.warning(
            "No valid text fields specified for embedding (embeddings.text_fields_to_embed is empty or not a list). Skipping."
        )
        return 0, 0

    storage_method = pydash_get(config, "embeddings.storage_method", "mongo_field")
    embedding_field = pydash_get(
        config, "embeddings.embedding_field_name", "embedding_vector"
    )
    batch_size = pydash_get(config, "embeddings.batch_size", 128)
    # Storing as BSON binary subtype 0x00 (generic binary)
    store_as_binary = True  # As per design
    binary_subtype = 0x00

    if storage_method != "mongo_field":
        logger.error(
            f"Storage method '{storage_method}' not implemented yet. Only 'mongo_field' is supported."
        )
        # Return zero errors as it's a config issue, not processing error
        return 0, 0

    # Query for documents that haven't been processed yet
    query = {
        embedding_field: {"$exists": False}
        # Optional: Add conditions to ensure source text fields exist?
        # "$or": [{field: {"$exists": True, "$ne": ""}} for field in text_fields] ? Complex...
    }

    # Project only necessary fields: _id and the text fields needed
    projection: Dict[str, int] = {"_id": 1}
    for field_path in text_fields:
        # Add projection for top-level fields if path contains dots
        top_level_field = field_path.split(".")[0]
        projection[top_level_field] = 1

    logger.info(
        f"Starting embedding generation for field '{embedding_field}'. Querying entities matching: {query}"
    )
    total_docs_to_process = 0
    try:
        total_docs_to_process = collection.count_documents(query)
        if total_docs_to_process == 0:
            logger.info("No documents found requiring embedding generation.")
            return 0, 0
        logger.info(
            f"Found approximately {total_docs_to_process:,} documents to generate embeddings for."
        )
    except Exception as e:
        logger.warning(
            f"Could not count documents for embedding generation: {e}. Proceeding anyway."
        )
        total_docs_to_process = -1  # Indicate unknown count

    # Fetch slightly larger batches from Mongo to feed the embedding batch size
    mongo_batch_factor = 2
    cursor = collection.find(query, projection, no_cursor_timeout=True).batch_size(
        batch_size * mongo_batch_factor
    )

    processed_count = 0  # Docs successfully embedded and written (or intended to write)
    write_errors = 0
    generation_errors = 0  # Errors during text construction or model.encode()
    start_time = time.time()

    doc_batch: List[Dict[str, Any]] = []

    try:  # Wrap cursor iteration
        with tqdm(
            total=total_docs_to_process if total_docs_to_process > 0 else None,
            desc="Generating Embeddings",
            unit="doc",
        ) as pbar:
            for doc in cursor:
                doc_batch.append(doc)

                if len(doc_batch) >= batch_size:
                    # --- Process Batch ---
                    texts_to_embed: List[str] = []
                    doc_ids_in_batch: List[str] = []
                    valid_indices_in_batch: List[
                        int
                    ] = []  # Tracks original index in doc_batch

                    for idx, batch_doc in enumerate(doc_batch):
                        doc_id = batch_doc.get("_id")
                        if not doc_id:
                            logger.warning("Document missing _id in batch, skipping.")
                            generation_errors += 1
                            continue
                        constructed_text = _construct_text_for_embedding(
                            batch_doc, text_fields
                        )
                        # Check if construction resulted in empty/default space only if needed
                        # if constructed_text == " ":
                        #    logger.debug(f"Skipping doc {doc_id} due to empty constructed text.")
                        #    generation_errors += 1 # Or just skip without error?
                        #    continue
                        texts_to_embed.append(constructed_text)
                        doc_ids_in_batch.append(doc_id)
                        valid_indices_in_batch.append(idx)

                    updates_batch: List[UpdateOne] = []
                    if texts_to_embed:
                        # Generate embeddings for the valid texts in the batch
                        try:
                            logger.debug(
                                f"Encoding batch of {len(texts_to_embed)} texts..."
                            )
                            embeddings_np: np.ndarray = model.encode(
                                texts_to_embed, show_progress_bar=False
                            )
                            logger.debug(
                                f"Encoding complete. Embedding shape: {embeddings_np.shape}"
                            )

                            # Prepare bulk updates only for successfully embedded docs
                            for i, embedding_np in enumerate(embeddings_np):
                                original_doc_id = doc_ids_in_batch[i]
                                try:
                                    embedding_value = _convert_to_bson_binary(
                                        embedding_np
                                    )
                                    updates_batch.append(
                                        UpdateOne(
                                            {"_id": original_doc_id},
                                            {
                                                "$set": {
                                                    embedding_field: embedding_value
                                                }
                                            },
                                        )
                                    )
                                except Exception as convert_err:
                                    logger.warning(
                                        f"Error converting embedding to BSON for doc {original_doc_id}: {convert_err}"
                                    )
                                    generation_errors += 1  # Failed conversion

                        except Exception as encode_err:
                            logger.error(
                                f"Error generating embeddings for batch (affecting {len(texts_to_embed)} docs): {encode_err}",
                                exc_info=True,
                            )
                            generation_errors += len(
                                texts_to_embed
                            )  # Count all docs in failed batch as errors

                    # --- Bulk Write ---
                    if updates_batch:
                        batch_write_errors_count = 0
                        try:
                            logger.debug(
                                f"Writing batch of {len(updates_batch)} embeddings..."
                            )
                            result = collection.bulk_write(updates_batch, ordered=False)
                            # <<< CORRECTED: Increment based on matched/upserted count >>>
                            successful_writes = (
                                result.matched_count + result.upserted_count
                            )
                            processed_count += successful_writes
                            # <<< END CORRECTION >>>
                            if result.bulk_api_result.get("writeErrors"):
                                batch_write_errors_count = len(
                                    result.bulk_api_result["writeErrors"]
                                )
                                logger.error(
                                    f"Bulk write errors storing embeddings: {batch_write_errors_count}"
                                )
                                write_errors += batch_write_errors_count

                        except BulkWriteError as bwe:
                            # Catch errors not reported in result but raising exception
                            batch_write_errors_count = len(
                                bwe.details.get("writeErrors", [])
                            )
                            logger.error(
                                f"Bulk write exception during embedding storage ({batch_write_errors_count} errors): {bwe.details}"
                            )
                            write_errors += batch_write_errors_count
                            # Estimate successful writes if possible
                            # <<< CORRECTED: Increment based on matched/upserted count >>>
                            ok_writes = (
                                bwe.details.get("nInserted", 0)
                                + bwe.details.get("nUpserted", 0)
                                + bwe.details.get("nMatched", 0)
                            )  # Approx successful
                            processed_count += ok_writes
                            # <<< END CORRECTION >>>
                        except Exception as e:
                            logger.error(
                                f"Unexpected error during bulk write: {e}",
                                exc_info=True,
                            )
                            write_errors += len(updates_batch)  # Assume all failed
                        finally:
                            updates_batch = []  # Clear for next batch

                    # Update progress bar by the number of docs *attempted* in the source batch
                    pbar.update(len(doc_batch))
                    # Clear the processed batch
                    doc_batch = []

            # --- Process Final Batch ---
            if doc_batch:
                logger.info(f"Processing final batch of {len(doc_batch)} documents...")
                # (Duplicate logic - consider refactoring into a helper function)
                texts_to_embed = []
                doc_ids_in_batch = []
                valid_indices_in_batch = []
                for idx, batch_doc in enumerate(doc_batch):
                    doc_id = batch_doc.get("_id")
                    if not doc_id:
                        continue
                    constructed_text = _construct_text_for_embedding(
                        batch_doc, text_fields
                    )
                    texts_to_embed.append(constructed_text)
                    doc_ids_in_batch.append(doc_id)
                    valid_indices_in_batch.append(idx)

                updates_batch = []
                if texts_to_embed:
                    try:
                        embeddings_np = model.encode(
                            texts_to_embed, show_progress_bar=False
                        )
                        for i, embedding_np in enumerate(embeddings_np):
                            original_doc_id = doc_ids_in_batch[i]
                            try:
                                embedding_value = _convert_to_bson_binary(embedding_np)
                                updates_batch.append(
                                    UpdateOne(
                                        {"_id": original_doc_id},
                                        {"$set": {embedding_field: embedding_value}},
                                    )
                                )
                            except Exception as convert_err:
                                logger.warning(
                                    f"Error converting embedding to BSON for doc {original_doc_id} (final batch): {convert_err}"
                                )
                                generation_errors += 1
                    except Exception as encode_err:
                        logger.error(
                            f"Error generating embeddings for final batch: {encode_err}",
                            exc_info=True,
                        )
                        generation_errors += len(texts_to_embed)

                if updates_batch:
                    batch_write_errors_count = 0
                    try:
                        result = collection.bulk_write(updates_batch, ordered=False)
                        # <<< CORRECTED: Increment based on matched/upserted count >>>
                        successful_writes = result.matched_count + result.upserted_count
                        processed_count += successful_writes
                        # <<< END CORRECTION >>>
                        if result.bulk_api_result.get("writeErrors"):
                            batch_write_errors_count = len(
                                result.bulk_api_result["writeErrors"]
                            )
                            logger.error(
                                f"Final bulk write errors storing embeddings: {batch_write_errors_count}"
                            )
                            write_errors += batch_write_errors_count
                    except BulkWriteError as bwe:
                        batch_write_errors_count = len(
                            bwe.details.get("writeErrors", [])
                        )
                        logger.error(
                            f"Final bulk write exception ({batch_write_errors_count} errors): {bwe.details}"
                        )
                        write_errors += batch_write_errors_count
                        # <<< CORRECTED: Increment based on matched/upserted count >>>
                        ok_writes = (
                            bwe.details.get("nInserted", 0)
                            + bwe.details.get("nUpserted", 0)
                            + bwe.details.get("nMatched", 0)
                        )  # Approx successful
                        processed_count += ok_writes
                        # <<< END CORRECTION >>>
                    except Exception as e:
                        logger.error(
                            f"Unexpected error during final bulk write: {e}",
                            exc_info=True,
                        )
                        write_errors += len(updates_batch)

                pbar.update(len(doc_batch))  # Update progress for the final batch

    except Exception as e:
        logger.error(f"Error during embedding generation loop: {e}", exc_info=True)
    finally:
        if cursor:
            try:
                cursor.close()
            except Exception as ce:
                logger.warning(f"Error closing MongoDB cursor: {ce}")
        end_time = time.time()
        duration = end_time - start_time
        total_errors = generation_errors + write_errors
        logger.info("-" * 50)
        logger.info(f"Embedding generation finished in {duration:.2f} seconds.")
        logger.info(
            f"Successfully stored embeddings for: {processed_count:,} documents."
        )
        if total_errors > 0:
            logger.warning(
                f"Encountered {total_errors} errors (Generation/Conversion: {generation_errors}, DB Write: {write_errors}). Check logs."
            )
        logger.info("-" * 50)

    return processed_count, total_errors


def main():
    parser = argparse.ArgumentParser(
        description="Generate and store embeddings for Wikidata entities in MongoDB."
    )
    parser.add_argument(
        "--config",
        default="config/config.yaml",
        help="Path to the global YAML configuration file.",
    )
    parser.add_argument(
        "--model-name", default=None, help="Override embedding model name from config."
    )
    parser.add_argument(
        "--batch-size", type=int, default=None, help="Override embedding batch size."
    )
    # Add argument to force processing even if field exists?
    # parser.add_argument("--force", action="store_true", help="Force regeneration of embeddings even if field exists.")

    args = parser.parse_args()
    config: Optional[Dict[str, Any]] = None

    try:
        config = load_config(config_path=args.config)
        setup_logging(config)

        # Apply overrides
        if args.model_name:
            config["embeddings"]["model_name"] = args.model_name
            logger.info(f"Overriding model name to: {args.model_name}")
        if args.batch_size:
            config["embeddings"]["batch_size"] = args.batch_size
            logger.info(f"Overriding batch size to: {args.batch_size}")

        if not pydash_get(config, "embeddings.enabled", False):
            logger.info(
                "Embedding generation is disabled in the configuration ('embeddings.enabled': false). Exiting."
            )
            sys.exit(0)

        # --- Load Model ---
        model = load_embedding_model(config)
        if model is None:
            sys.exit(1)

        # --- Get Collection ---
        collection = get_entity_collection(config)

        # --- Generate and Store ---
        processed, errors = generate_and_store_embeddings(collection, model, config)

        if errors > 0:
            logger.error("Embedding generation completed with errors.")
            sys.exit(1)  # Exit with error code if errors occurred
        else:
            logger.info("Embedding generation completed successfully.")
            sys.exit(0)

    except (
        FileNotFoundError,
        ConfigurationError,
        ConnectionFailure,
        MongoConfigError,
    ) as e:
        # Log critical setup/connection errors before exiting
        # Check if logger was successfully configured
        if logging.getLogger().hasHandlers():
            logger.critical(f"Embedding script failed during setup: {e}", exc_info=True)
        else:
            print(
                f"CRITICAL: Embedding script failed during setup: {e}", file=sys.stderr
            )
        sys.exit(1)
    except Exception as e:
        if logging.getLogger().hasHandlers():
            logger.critical(f"An unexpected error occurred: {e}", exc_info=True)
        else:
            print(f"CRITICAL: An unexpected error occurred: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
