#!/usr/bin/env python3
import logging
import json
import argparse
import sys
import os
from typing import (
    Dict,
    Any,
    List,
    Tuple,
    Optional,
    cast,
    Union,
    Set,
)  # Added Union, Set

# Make utils discoverable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from utils.config import (
    load_config,
    get_config,
    pydash_get,
    get_base_dir,
    ConfigurationError,
)  # Added ConfigurationError
from utils.logging_config import setup_logging
from utils.mongo_helpers import get_mongo_client, get_database, get_entity_collection
from pymongo.errors import (
    CollectionInvalid,
    OperationFailure,
    ConnectionFailure,
    ConfigurationError as MongoConfigError,
)  # Renamed pymongo's CE
from pymongo.collection import Collection
from pymongo.database import Database
import pymongo  # For index direction constants

logger = logging.getLogger(__name__)


def load_schema(schema_file_path: str) -> Dict[str, Any]:
    """
    Loads the JSON schema from the specified file.

    Args:
        schema_file_path: Absolute path to the JSON schema file.

    Returns:
        The dictionary representing the $jsonSchema structure.

    Raises:
        FileNotFoundError: If the schema file does not exist.
        json.JSONDecodeError: If the file is not valid JSON.
        ValueError: If the JSON does not seem to represent a valid schema structure.
    """
    if not os.path.exists(schema_file_path):
        logger.error(f"Schema file not found: {schema_file_path}")
        raise FileNotFoundError(f"Schema file not found: {schema_file_path}")

    try:
        with open(schema_file_path, "r", encoding="utf-8") as f:
            schema_doc = json.load(f)

        # Extract the actual schema object if wrapped under "$jsonSchema"
        if isinstance(schema_doc, dict) and "$jsonSchema" in schema_doc:
            schema = schema_doc["$jsonSchema"]
            if not isinstance(schema, dict):
                raise ValueError(
                    "'$jsonSchema' key does not contain a dictionary object."
                )
            logger.info(
                f"Successfully loaded MongoDB schema (extracted $jsonSchema) from {schema_file_path}"
            )
            return cast(Dict[str, Any], schema)
        elif isinstance(schema_doc, dict) and "bsonType" in schema_doc:
            # Assume the file *is* the $jsonSchema content directly
            logger.warning(
                f"Schema file {schema_file_path} does not contain top-level '$jsonSchema' key. Assuming file content is the schema itself."
            )
            schema = schema_doc
            return cast(Dict[str, Any], schema)
        else:
            raise ValueError(
                "Loaded JSON does not appear to be a valid schema (missing '$jsonSchema' or 'bsonType')."
            )

    except json.JSONDecodeError as e:
        logger.error(f"Error parsing JSON schema file {schema_file_path}: {e}")
        raise
    except ValueError as e:
        logger.error(f"Invalid schema structure in {schema_file_path}: {e}")
        raise
    except Exception as e:
        logger.error(
            f"Unexpected error loading schema file {schema_file_path}: {e}",
            exc_info=True,
        )
        raise


def apply_schema_validation(db: Database, collection_name: str, schema: Dict[str, Any]):
    """
    Applies or updates the $jsonSchema validation rule to the specified collection.

    Creates the collection if it doesn't exist. Uses 'collMod' to apply validation.

    Args:
        db: The pymongo Database object.
        collection_name: The name of the collection to modify.
        schema: The $jsonSchema dictionary to apply.
    """
    try:
        logger.info(
            f"Attempting to apply schema validation to {db.name}.{collection_name}..."
        )
        # Use collMod to apply/update the validator.
        # MongoDB >= 3.6 creates the collection if it doesn't exist when collMod is run.
        # Corrected: Use dictionary format for db.command for collMod
        db.command(
            {
                "collMod": collection_name,
                "validator": {"$jsonSchema": schema},
                "validationLevel": "strict",
                "validationAction": "error",
            }
        )
        logger.info(
            f"Successfully applied/updated JSON schema validation for collection '{collection_name}'."
        )
    except OperationFailure as e:
        # Check if error is specifically "NamespaceNotFound" - should be handled by collMod implicitly creating it.
        # If this error still occurs, it might indicate a deeper issue.
        if "NamespaceNotFound" in str(e.details):
            logger.error(
                f"OperationFailure 'NamespaceNotFound' occurred unexpectedly during collMod for '{collection_name}'. This might indicate a MongoDB version issue or permission problem. Details: {e.details}",
                exc_info=True,
            )
            # Attempt explicit creation as a fallback? Risky if collMod should work.
            # try:
            #     db.create_collection(collection_name, validator={'$jsonSchema': schema}, validationLevel='strict', validationAction='error')
            #     logger.info(f"Explicitly created collection '{collection_name}' after NamespaceNotFound error.")
            # except Exception as create_e:
            #     logger.error(f"Failed explicit collection creation after collMod error: {create_e}", exc_info=True)
            #     raise e # Re-raise original error
            raise  # Re-raise original error for now
        else:
            # Log other OperationFailures (e.g., invalid schema, permissions)
            logger.error(
                f"Failed to apply schema validation to collection '{collection_name}': {e.details}",
                exc_info=True,
            )
            raise  # Reraise other failures as they might be critical
    except Exception as e:
        logger.error(
            f"An unexpected error occurred during schema application for '{collection_name}': {e}",
            exc_info=True,
        )
        raise  # Reraise unexpected errors


def create_indexes(collection: Collection, schema: Dict[str, Any]):
    """
    Creates indexes based on the schema structure and common query patterns.

    Ensures essential indexes exist for performance. Skips existing indexes.

    Args:
        collection: The pymongo Collection object.
        schema: The $jsonSchema dictionary (used to infer potential index fields).
    """
    # Use a set to avoid duplicates if inferred multiple ways
    indexes_to_ensure: Set[Tuple[str, Union[int, str]]] = set()
    # Primary ID index (_id) is created automatically by MongoDB.

    # Add fields explicitly known to be queried frequently
    indexes_to_ensure.add(("wikidata_id", pymongo.ASCENDING))  # Query by specific QID
    indexes_to_ensure.add(("last_updated", pymongo.DESCENDING))  # Query/sort by recency

    # --- Infer indexes from schema (more robust checks) ---
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        logger.warning(
            "Schema 'properties' field is missing or not a dictionary. Cannot infer indexes from schema."
        )
        properties = {}

    # Geo index for coordinates
    if "coordinates" in properties:
        coord_prop = properties["coordinates"]
        # Check structure for GeoJSON Point
        if (
            isinstance(coord_prop, dict)
            and pydash_get(coord_prop, "properties.type.enum") == ["Point"]
            and pydash_get(coord_prop, "properties.coordinates.bsonType") == "array"
        ):
            indexes_to_ensure.add(("coordinates", pymongo.GEOSPHERE))

    # Index for admin hierarchy QIDs (array elements)
    if "admin_hierarchy" in properties:
        admin_prop = properties["admin_hierarchy"]
        if (
            isinstance(admin_prop, dict)
            and admin_prop.get("bsonType") == "array"
            and pydash_get(admin_prop, "items.properties.qid.bsonType") == "string"
        ):
            indexes_to_ensure.add(("admin_hierarchy.qid", pymongo.ASCENDING))

    # Index for wiki links (if present in schema)
    if "wiki_links" in properties:
        wiki_prop = properties["wiki_links"]
        if isinstance(wiki_prop, dict) and isinstance(
            wiki_prop.get("properties"), dict
        ):
            if (
                pydash_get(wiki_prop, "properties.normalized_enwiki_title.bsonType")
                == "string"
            ):
                indexes_to_ensure.add(
                    ("wiki_links.normalized_enwiki_title", pymongo.ASCENDING)
                )
            if pydash_get(wiki_prop, "properties.inlink_count.bsonType") in [
                "int",
                "long",
                "number",
                "double",
            ]:  # Allow various numeric types
                indexes_to_ensure.add(
                    ("wiki_links.inlink_count", pymongo.DESCENDING)
                )  # High counts first

    # Class/Type indexes
    if "instance_of" in properties:
        if pydash_get(properties, "instance_of.bsonType") == "array":
            # Indexing array fields directly can have performance implications.
            # Check MongoDB docs for best practices based on query patterns.
            indexes_to_ensure.add(("instance_of", pymongo.ASCENDING))
    if "corrected_class" in properties:
        # Check if 'string' is one of the allowed types
        allowed_types = pydash_get(properties, "corrected_class.bsonType", [])
        if isinstance(allowed_types, list) and "string" in allowed_types:
            indexes_to_ensure.add(("corrected_class", pymongo.ASCENDING))

    # Index for English label (exact match queries)
    if "english_label" in properties:
        if pydash_get(properties, "english_label.bsonType") == "string":
            indexes_to_ensure.add(("english_label", pymongo.ASCENDING))

    # Add index for Ontology Flag
    if "ontology_flag_resolved" in properties:
        if pydash_get(properties, "ontology_flag_resolved.bsonType") == "bool":
            indexes_to_ensure.add(("ontology_flag_resolved", pymongo.ASCENDING))

    # Add index for embedding vector if present and array type
    if "embedding_vector" in properties:
        if pydash_get(properties, "embedding_vector.bsonType") == "array":
            # Basic index, consider specialized vector index types if using Atlas Search etc.
            # This basic index isn't efficient for similarity search.
            # indexes_to_ensure.add(("embedding_vector", pymongo.ASCENDING))
            logger.warning(
                "Found 'embedding_vector' array in schema. Consider specialized vector indexes (e.g., Atlas Search) for efficient similarity search instead of a basic index."
            )

    logger.info(f"Ensuring indexes on collection '{collection.name}':")
    try:
        # Get current index information
        existing_indexes = collection.index_information()
        existing_index_keys: Dict[tuple, str] = {}  # Map key tuple -> index name
        for name, info in existing_indexes.items():
            # Convert list of lists/tuples from index_information() to tuple of tuples
            key_tuple = tuple(tuple(item) for item in info.get("key", []))
            existing_index_keys[key_tuple] = name
        logger.debug(f"Existing indexes: {existing_index_keys}")

    except OperationFailure as e:
        logger.error(
            f"Failed to get index information for collection '{collection.name}': {e.details}. Skipping index creation."
        )
        return  # Cannot proceed without knowing existing indexes

    created_count = 0
    skipped_count = 0
    failed_count = 0

    # Sort for deterministic order (optional, but good for logs/testing)
    sorted_indexes = sorted(list(indexes_to_ensure), key=lambda x: x[0])

    for field, direction in sorted_indexes:
        # Define the index key tuple in the format MongoDB expects
        index_key_tuple = ((field, direction),)

        # Define a consistent index name based on field and direction
        if isinstance(direction, str):
            direction_str = direction.lower().replace(" ", "")  # e.g., 'geosphere'
        elif direction == pymongo.ASCENDING:
            direction_str = "1"
        elif direction == pymongo.DESCENDING:
            direction_str = "-1"
        else:
            direction_str = str(direction)  # Fallback

        # Create index name, replacing dots with underscores
        safe_field_name = field.replace(".", "_")
        index_name = f"{safe_field_name}_{direction_str}_idx"

        # Check if an index with the *exact same key* already exists
        if index_key_tuple in existing_index_keys:
            existing_name = existing_index_keys[index_key_tuple]
            logger.debug(
                f"- Index on key '{index_key_tuple}' already exists (named '{existing_name}'). Skipping creation of '{index_name}'."
            )
            skipped_count += 1
            continue

        # Attempt to create the index
        try:
            logger.info(
                f"- Creating index '{index_name}' on key '{index_key_tuple}'..."
            )
            # background=True allows other operations while index builds (good for large collections)
            collection.create_index(
                list(index_key_tuple), name=index_name, background=True
            )
            logger.info(f"- Successfully initiated creation of index '{index_name}'.")
            created_count += 1
        except OperationFailure as e:
            # This might fail if e.g., index exists with different options, conflicting name, etc.
            logger.warning(
                f"- Could not create index '{index_name}' on key '{index_key_tuple}': {e.details}. It might conflict with an existing index or options."
            )
            failed_count += 1
        except Exception as e:
            logger.error(
                f"- Unexpected error creating index '{index_name}' on key '{index_key_tuple}': {e}",
                exc_info=True,
            )
            failed_count += 1

    logger.info(
        f"Index creation summary: {created_count} created/initiated, {skipped_count} skipped (already exist), {failed_count} failed."
    )


def main():
    """Main execution function."""
    parser = argparse.ArgumentParser(
        description="Setup MongoDB collection: apply schema validation and create indexes.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--config",
        default="config/config.yaml",
        help="Path to the global YAML configuration file.",
    )
    parser.add_argument(
        "--schema-file",  # Use hyphen for consistency with runner
        dest="schema_file_arg",  # Store in different variable
        default=None,
        help="Path to the JSON schema file (overrides config file path if provided).",
    )
    args = parser.parse_args()

    # --- Setup Phase ---
    config: Optional[Dict[str, Any]] = None
    try:
        base_dir = get_base_dir()
        config = load_config(config_path=args.config, base_dir=base_dir)
        setup_logging(config)

        # Determine schema file path
        schema_file_path_from_config = pydash_get(
            config, "mongodb.schema_file"
        )  # Check config first
        schema_file_path_rel = (
            args.schema_file_arg or schema_file_path_from_config
        )  # Override with arg if provided
        if not schema_file_path_rel:
            logger.critical(
                "MongoDB schema file path not found in arguments or configuration ('mongodb.schema_file')."
            )
            raise ConfigurationError("MongoDB schema file path not specified.")

        abs_schema_file_path = schema_file_path_rel
        if not os.path.isabs(abs_schema_file_path):
            abs_schema_file_path = os.path.join(base_dir, abs_schema_file_path)

        schema_data = load_schema(abs_schema_file_path)

        # Get DB and collection name
        db = get_database(config)  # Uses cached client/db handles
        collection_name = pydash_get(config, "mongodb.collection_entities")
        if not collection_name:
            logger.critical(
                "Entity collection name not found in configuration (mongodb.collection_entities)."
            )
            raise ConfigurationError("Entity collection name is not configured.")

    except (
        FileNotFoundError,
        ConfigurationError,
        MongoConfigError,
        KeyError,
        json.JSONDecodeError,
        ValueError,
    ) as e:
        # Catch setup errors before attempting DB operations
        logger.critical(
            f"Setup script failed during configuration/schema loading: {e}",
            exc_info=True,
        )
        sys.exit(1)
    except Exception as e:
        # Catch unexpected errors during setup
        logger.critical(
            f"An unexpected error occurred during setup phase: {e}", exc_info=True
        )
        sys.exit(1)

    # --- Execution Phase ---
    try:
        logger.info(
            f"--- Setting up MongoDB Collection: {db.name}.{collection_name} ---"
        )

        # Apply Schema Validation
        apply_schema_validation(db, collection_name, schema_data)

        # Get collection object *after* potential creation/validation
        collection = get_entity_collection(config)  # Gets the collection handle

        # Create Indexes
        create_indexes(collection, schema_data)

        logger.info("--- MongoDB setup script completed successfully. ---")

    except (ConnectionFailure, OperationFailure) as e:
        logger.critical(
            f"MongoDB operation failed during setup: {e.details if hasattr(e, 'details') else e}",
            exc_info=True,
        )
        sys.exit(1)
    except Exception as e:
        logger.critical(
            f"An unexpected error occurred during MongoDB setup execution: {e}",
            exc_info=True,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
