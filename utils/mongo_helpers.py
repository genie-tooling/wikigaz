import logging
from typing import Optional, Dict, Any, List
from pymongo import MongoClient
from pymongo.database import Database
from pymongo.collection import Collection
from pymongo.errors import ConnectionFailure, ConfigurationError, OperationFailure
from pymongo.driver_info import DriverInfo

# Conditional import for config utilities
try:
    from .config import get_config, pydash_get, ConfigurationError as ConfigUtilError
except ImportError:
    # Allow running script directly for testing/example
    try:
        from config import get_config, pydash_get, ConfigurationError as ConfigUtilError
    except ImportError:

        def get_config():
            return {}

        def pydash_get(cfg, key, default=None):
            return default

        class ConfigUtilError(Exception):
            pass


logger = logging.getLogger(__name__)

# --- Module-level cache for MongoDB client and database ---
_mongo_client: Optional[MongoClient] = None
_database: Optional[Database] = None


def get_mongo_client(
    config: Optional[Dict[str, Any]] = None, force_new: bool = False
) -> MongoClient:
    """
    Establishes and returns a MongoClient connection using configuration.

    Caches the client globally for reuse. Includes a basic check to verify
    the cached connection is still alive before returning it.

    Args:
        config: Optional configuration dictionary. Uses global config if None.
        force_new: If True, creates a new client instance instead of returning cached one.

    Returns:
        A connected MongoClient instance.

    Raises:
        ConnectionFailure: If connection to MongoDB fails during initial connect or check.
        ConfigurationError: If MongoDB URI is missing or invalid in the configuration.
    """
    global _mongo_client
    if _mongo_client is not None and not force_new:
        # Check if the cached client connection is still valid
        try:
            # The 'ping' command is cheap and suitable for checking connection status.
            _mongo_client.admin.command("ping")
            logger.debug("Using cached MongoDB client connection.")
            return _mongo_client
        except ConnectionFailure as e:
            logger.warning(
                f"Cached MongoDB client connection check failed: {e}. Attempting to reconnect."
            )
            _mongo_client = None  # Reset cache, force reconnection attempt
        except Exception as e:  # Catch other potential errors like timeout
            logger.warning(
                f"Error checking cached MongoDB client connection: {e}. Attempting to reconnect."
            )
            _mongo_client = None

    if config is None:
        try:
            config = get_config()
        except ConfigUtilError as e:
            logger.critical(f"Failed to load config for MongoDB client: {e}")
            raise ConfigurationError("Failed to load configuration for MongoDB.") from e

    mongo_uri = pydash_get(config, "mongodb.uri")
    if not mongo_uri:
        logger.critical(
            "MongoDB URI not found in configuration (check 'mongodb.uri' or MONGO_URI env var)."
        )
        raise ConfigurationError("MongoDB URI is not configured.")

    # Mask credentials in log message if present
    masked_uri = mongo_uri
    try:
        from urllib.parse import urlparse, urlunparse

        parsed = urlparse(mongo_uri)
        if parsed.username or parsed.password:
            # Rebuild URI without user/password
            host_port = (
                f"{parsed.hostname}:{parsed.port}" if parsed.port else parsed.hostname
            )
            masked_uri = urlunparse(
                parsed._replace(netloc=host_port)
            )  # Keep scheme, path, query etc.
    except Exception:
        pass  # Ignore parsing errors, log original URI

    logger.info(f"Attempting to connect to MongoDB at {masked_uri} ...")
    try:
        # Set a reasonable server selection timeout
        # Add application name for easier tracking in MongoDB logs
        driver_info = DriverInfo("wiki2gaz-modern", "1.0.0")  # App name, App version
        client = MongoClient(
            mongo_uri,
            serverSelectionTimeoutMS=5000,  # e.g., 5 seconds timeout for server selection
            connectTimeoutMS=5000,  # Timeout for establishing connection
            socketTimeoutMS=15000,  # Timeout for individual socket operations
            appname="wiki2gaz_modern",
            driver=driver_info,
        )
        # Verify connection by pinging the server
        client.admin.command("ping")
        server_info = client.server_info()
        logger.info(
            f"Successfully connected to MongoDB server version {server_info.get('version', 'unknown')}."
        )
        if not force_new:
            _mongo_client = client  # Cache the new client
        return client
    except ConnectionFailure as e:
        logger.critical(f"Failed to connect to MongoDB server at {masked_uri}: {e}")
        raise
    except ConfigurationError as e:  # PyMongo's ConfigurationError
        # This might catch issues with the URI format itself or invalid options
        logger.critical(f"Invalid MongoDB URI or configuration options: {e}")
        raise
    except Exception as e:
        # Catch other potential errors during client creation
        logger.critical(
            f"An unexpected error occurred while creating MongoDB client: {e}",
            exc_info=True,
        )
        raise ConnectionFailure(
            "Unexpected error during MongoDB client creation."
        ) from e


def get_database(
    config: Optional[Dict[str, Any]] = None, force_new_client: bool = False
) -> Database:
    """
    Returns the specific MongoDB database instance specified in the config.

    Uses the globally cached client by default. Caches the database instance.

    Args:
        config: Optional configuration dictionary. Uses global config if None.
        force_new_client: If True, forces creation of a new client connection first.

    Returns:
        A Database instance configured according to the application config.

    Raises:
        ConfigurationError: If the database name is not configured.
        ConnectionFailure: If establishing connection via get_mongo_client fails.
    """
    global _database
    # Force new client implies we need a new DB handle too
    if _database is not None and not force_new_client:
        # Check if the underlying client is still connected
        try:
            if _database.client:  # Check if client attribute exists
                _database.client.admin.command(
                    "ping"
                )  # Check connection via the db's client
                logger.debug(f"Using cached database instance '{_database.name}'.")
                return _database
            else:
                logger.warning(
                    "Cached database instance missing client. Getting new instance."
                )
                _database = None
        except (
            ConnectionFailure,
            AttributeError,
        ):  # AttributeError if _database or client is None somehow
            logger.warning(
                "Cached database instance's client connection lost. Getting new instance."
            )
            _database = None  # Reset cache

    if config is None:
        try:
            config = get_config()
        except ConfigUtilError as e:
            logger.critical(f"Failed to load config for MongoDB database: {e}")
            raise ConfigurationError("Failed to load configuration for MongoDB.") from e

    db_name = pydash_get(config, "mongodb.database")
    if not db_name:
        logger.critical(
            "MongoDB database name not found in configuration (check 'mongodb.database')."
        )
        raise ConfigurationError("MongoDB database name is not configured.")

    # Get client (potentially cached, or new if force_new_client is True)
    try:
        client = get_mongo_client(config=config, force_new=force_new_client)
    except (ConnectionFailure, ConfigurationError) as e:
        logger.critical(f"Failed to get MongoDB client while retrieving database: {e}")
        raise  # Propagate the error

    db = client[db_name]

    if not force_new_client:  # Only cache if not forcing new client/db handles
        _database = db  # Cache the database instance
    logger.info(f"Using MongoDB database: '{db_name}'")
    return db


def get_entity_collection(config: Optional[Dict[str, Any]] = None) -> Collection:
    """
    Returns the primary entity collection instance specified in the config.

    Uses the cached database instance by default.

    Args:
        config: Optional configuration dictionary. Uses global config if None.

    Returns:
        A Collection instance for the main entity collection.

    Raises:
        ConfigurationError: If the entity collection name is not configured.
        ConnectionFailure: If establishing connection fails.
    """
    if config is None:
        try:
            config = get_config()
        except ConfigUtilError as e:
            logger.critical(f"Failed to load config for MongoDB collection: {e}")
            raise ConfigurationError("Failed to load configuration for MongoDB.") from e

    collection_name = pydash_get(config, "mongodb.collection_entities")
    if not collection_name:
        logger.critical(
            "Entity collection name not found in configuration (check 'mongodb.collection_entities')."
        )
        raise ConfigurationError("Entity collection name is not configured.")

    try:
        db = get_database(config=config)  # Use potentially cached DB
    except (ConnectionFailure, ConfigurationError) as e:
        logger.critical(
            f"Failed to get MongoDB database while retrieving collection: {e}"
        )
        raise  # Propagate the error

    collection = db[collection_name]
    logger.debug(f"Using entity collection: '{db.name}.{collection_name}'")
    return collection


# --- Helper for P131 Traversal (Minimal Data) ---
# Note: This is used by wikidata_helpers, included here for context if needed standalone
def get_minimal_item_data_for_hierarchy(
    qids: List[str], collection: Collection
) -> Dict[str, Dict[str, Any]]:
    """
    Fetches minimal data (label, P131 IDs) needed for hierarchy traversal for a list of QIDs.
    Designed to be called by the batch prefetcher in wikidata_helpers.

    Args:
        qids: A list of Wikidata QIDs (strings) to fetch data for.
        collection: The MongoDB collection where entities are stored.

    Returns:
        A dictionary mapping QID -> {'label': str, 'p131_qids': List[str]}
    """
    if not qids:
        return {}

    logger.debug(f"Fetching minimal hierarchy data for {len(qids)} QIDs...")
    results: Dict[str, Dict[str, Any]] = {}
    try:
        # Fetch only the necessary fields: _id, english_label, and claims.P131
        projection = {
            "_id": 1,
            "english_label": 1,  # Assumes English label is stored
            "claims.P131.mainsnak.datavalue.value.id": 1  # Project only the target QID
            # Alternative: Project the whole 'claims' object if structure varies widely
            # "claims.P131": 1
        }
        # Use $in for efficient batch fetching
        cursor = collection.find({"_id": {"$in": qids}}, projection)

        for doc in cursor:
            doc_id = doc.get("_id")
            if not doc_id:
                continue

            label = doc.get("english_label", doc_id)  # Fallback to QID if label missing
            p131_qids: List[str] = []
            # Extract P131 QIDs carefully using pydash or similar safe access
            # This path assumes claims were stored directly, adjust if using different structure
            claims_p131 = pydash_get(doc, "claims.P131", [])
            if isinstance(claims_p131, list):
                for claim in claims_p131:
                    # Check if it's a standard claim with a value snak
                    if pydash_get(claim, "mainsnak.snaktype") == "value":
                        p131_id = pydash_get(claim, "mainsnak.datavalue.value.id")
                        if isinstance(p131_id, str):
                            p131_qids.append(p131_id)

            results[doc_id] = {"label": label, "p131_qids": p131_qids}

    except OperationFailure as e:
        logger.error(
            f"MongoDB operation failed while fetching hierarchy data: {e}",
            exc_info=True,
        )
    except Exception as e:
        logger.error(f"Unexpected error fetching hierarchy data: {e}", exc_info=True)

    found_qids = len(results)
    if found_qids < len(qids):
        logger.warning(
            f"Could only find hierarchy data for {found_qids} out of {len(qids)} requested QIDs."
        )

    return results


# Example Usage (requires a running MongoDB instance specified in .env)
if __name__ == "__main__":
    # Assume config.py can create dummy files if run standalone
    # Need config_temp_mongo.yaml and .env_temp_mongo
    base = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(base, ".env_temp_mongo")
    cfg_path = os.path.join(base, "config_temp_mongo.yaml")
    if not os.path.exists(env_path):
        with open(env_path, "w") as f:
            f.write(
                "MONGO_URI=mongodb://localhost:27017/?serverSelectionTimeoutMS=2000\n"
            )
    if not os.path.exists(cfg_path):
        with open(cfg_path, "w") as f:
            f.write(
                "logging:\n  level: DEBUG\nmongodb:\n  uri: ${MONGO_URI}\n  database: mongo_helper_test_db\n  collection_entities: test_entities\n"
            )

    from logging_config import (
        setup_logging,
    )  # Assume logging_config.py is in the same dir

    try:
        cfg = load_config(config_path=cfg_path, dotenv_path=env_path, force_reload=True)
        setup_logging(cfg)
    except Exception as e:
        logging.basicConfig(level=logging.INFO)
        logger.error(f"Failed to load config/setup logging for example: {e}")
        sys.exit(1)

    client: Optional[MongoClient] = None
    try:
        print("--- Testing MongoDB Helpers ---")
        print("Attempting to get MongoDB client...")
        client = get_mongo_client()
        print(
            f"Client Obtained: OK (Server Version: {client.server_info().get('version', 'N/A')})"
        )

        print("\nAttempting to get Database...")
        db = get_database()
        print(f"Database Obtained: '{db.name}'")

        print("\nAttempting to get Entity Collection...")
        collection = get_entity_collection()
        print(f"Collection Obtained: '{collection.name}'")

        # Perform a simple operation
        doc_count = collection.count_documents({})
        print(f"Document count in '{collection.name}': {doc_count}")

        # Add cleanup logic if needed

    except (ConnectionFailure, ConfigurationError, ConfigUtilError) as e:
        print(f"\n*** MongoDB connection/configuration failed: {e}")
        print(
            "*** Please ensure MongoDB is running and MONGO_URI is correctly set in '.env_temp_mongo'"
        )
    except Exception as e:
        print(f"\n*** An unexpected error occurred: {e}")
        logger.exception("Error during example execution:")
    finally:
        # Clean up example files
        if os.path.exists(env_path):
            os.remove(env_path)
        if os.path.exists(cfg_path):
            os.remove(cfg_path)
        # Close client connection if open
        if _mongo_client:
            _mongo_client.close()
            print("Closed MongoDB client connection.")
        elif client:
            client.close()
            print("Closed local MongoDB client instance.")
