#!/usr/bin/env python3
import logging
import argparse
import sys
import json
import os
import time
import sqlite3  # For intermediate stats DB
import threading  # For StatsLookup lock
from typing import Dict, Any, Optional, List, Iterator, Tuple

# Make utils discoverable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from utils.config import (
    load_config,
    get_config,
    pydash_get,
    ConfigurationError,
    get_base_dir,
)
from utils.logging_config import setup_logging
from utils.helpers import normalize_wikipedia_title  # Ensure consistency
from utils.mongo_helpers import get_entity_collection
from utils.mapping import WikimapperLookup  # Import the lookup service
from pymongo.collection import Collection
from pymongo.errors import (
    ConnectionFailure,
    BulkWriteError,
    ConfigurationError as MongoConfigError,
)
from pymongo import UpdateOne  # Import UpdateOne

logger = logging.getLogger(__name__)

# Path for the temporary SQLite DB holding stats if created from JSONL
TEMP_STATS_DB_NAME = "temp_wiki_stats_lookup.db"  # Stored in project root by default


def create_temp_stats_db(config: Dict[str, Any], temp_db_path: str) -> bool:
    """
    Reads JSONL stats files and populates a temporary, indexed SQLite database.
    Returns True on success, False on failure.
    """
    stats_format = pydash_get(
        config, "wikipedia_processing.stats_output_format", ""
    ).lower()
    stats_location = pydash_get(config, "wikipedia_processing.stats_output_location")
    base_dir = get_base_dir()  # Get project root

    if stats_format != "json":
        logger.error(
            f"Stats format is '{stats_format}', not 'json'. Cannot create temp DB from JSONL."
        )
        logger.error(
            "Configure stats_output_format to 'sqlite' or 'mongo_collection' in process_wiki_stats step, or change config."
        )
        return False  # Indicate failure

    if not stats_location:
        logger.error(
            "Stats output location (directory for JSONL) not configured ('wikipedia_processing.stats_output_location')."
        )
        return False

    # Resolve stats_location relative to base_dir if needed
    abs_stats_location = stats_location
    if not os.path.isabs(abs_stats_location):
        abs_stats_location = os.path.abspath(os.path.join(base_dir, abs_stats_location))

    counts_jsonl_path = os.path.join(abs_stats_location, "qid_inlink_counts.jsonl")
    mentions_jsonl_path = os.path.join(abs_stats_location, "qid_mentions.jsonl")

    if not os.path.exists(counts_jsonl_path):
        logger.error(f"Counts JSONL file not found: {counts_jsonl_path}")
        return False
    if not os.path.exists(mentions_jsonl_path):
        logger.error(f"Mentions JSONL file not found: {mentions_jsonl_path}")
        return False

    # Remove existing temp DB if it exists
    if os.path.exists(temp_db_path):
        logger.warning(f"Removing existing temporary stats DB: {temp_db_path}")
        try:
            os.remove(temp_db_path)
        except OSError as e:
            logger.error(f"Failed to remove existing temp DB {temp_db_path}: {e}")
            return False

    logger.info(f"Creating temporary SQLite stats database at: {temp_db_path}")
    conn = None
    start_time = time.time()
    try:
        # Ensure directory for temp DB exists
        temp_db_dir = os.path.dirname(temp_db_path)
        if temp_db_dir:
            os.makedirs(temp_db_dir, exist_ok=True)

        conn = sqlite3.connect(temp_db_path)
        cursor = conn.cursor()

        # Create tables with indexes for efficient QID lookup
        cursor.execute(
            """
        CREATE TABLE qid_counts (
            qid TEXT PRIMARY KEY,
            count INTEGER NOT NULL
        ) WITHOUT ROWID;
        """
        )
        cursor.execute(
            """
        CREATE TABLE qid_mentions (
            qid TEXT NOT NULL,
            mention TEXT NOT NULL,
            count INTEGER NOT NULL,
            PRIMARY KEY (qid, mention)
        ) WITHOUT ROWID;
        """
        )
        cursor.execute("CREATE INDEX idx_qid_mentions_qid ON qid_mentions (qid);")

        conn.commit()  # Commit table creation

        # Load counts from JSONL
        logger.info(f"Loading counts from {counts_jsonl_path} into {temp_db_path}...")
        count_buffer = []
        buffer_size = 100000
        inserted_counts = 0
        with open(counts_jsonl_path, "r", encoding="utf-8") as f_counts:
            for line in f_counts:
                try:
                    data = json.loads(line)
                    count_buffer.append((data["qid"], data["count"]))
                    if len(count_buffer) >= buffer_size:
                        cursor.executemany(
                            "INSERT INTO qid_counts (qid, count) VALUES (?, ?)",
                            count_buffer,
                        )
                        conn.commit()  # Commit periodically
                        inserted_counts += len(count_buffer)
                        logger.debug(f"Inserted {inserted_counts:,} counts...")
                        count_buffer = []
                except (json.JSONDecodeError, KeyError) as e:
                    logger.warning(
                        f"Skipping invalid line in counts file: {line.strip()} - Error: {e}"
                    )
        # Insert remaining buffer
        if count_buffer:
            cursor.executemany(
                "INSERT INTO qid_counts (qid, count) VALUES (?, ?)", count_buffer
            )
            inserted_counts += len(count_buffer)
        conn.commit()
        logger.info(f"Finished loading {inserted_counts:,} counts.")

        # Load mentions from JSONL
        logger.info(
            f"Loading mentions from {mentions_jsonl_path} into {temp_db_path}..."
        )
        mention_buffer = []
        inserted_mentions = 0
        with open(mentions_jsonl_path, "r", encoding="utf-8") as f_mentions:
            for line in f_mentions:
                try:
                    data = json.loads(line)
                    mention_buffer.append((data["qid"], data["mention"], data["count"]))
                    if len(mention_buffer) >= buffer_size:
                        cursor.executemany(
                            "INSERT OR IGNORE INTO qid_mentions (qid, mention, count) VALUES (?, ?, ?)",
                            mention_buffer,
                        )
                        conn.commit()  # Commit periodically
                        inserted_mentions += len(
                            mention_buffer
                        )  # May not be exact due to OR IGNORE
                        logger.debug(
                            f"Processed {inserted_mentions:,} mentions inserts (using OR IGNORE)..."
                        )
                        mention_buffer = []
                except (json.JSONDecodeError, KeyError) as e:
                    logger.warning(
                        f"Skipping invalid line in mentions file: {line.strip()} - Error: {e}"
                    )
        # Insert remaining buffer
        if mention_buffer:
            cursor.executemany(
                "INSERT OR IGNORE INTO qid_mentions (qid, mention, count) VALUES (?, ?, ?)",
                mention_buffer,
            )
            inserted_mentions += len(mention_buffer)  # Approx
        conn.commit()
        logger.info(f"Finished processing {inserted_mentions:,} mention inserts.")

        duration = time.time() - start_time
        logger.info(
            f"Successfully created and populated temporary stats DB in {duration:.2f}s."
        )
        return True

    except sqlite3.Error as e:
        logger.error(
            f"SQLite error creating/populating temp DB {temp_db_path}: {e}",
            exc_info=True,
        )
        return False
    except IOError as e:
        logger.error(f"I/O error reading stats files: {e}", exc_info=True)
        return False
    except Exception as e:
        logger.error(f"Unexpected error creating temp DB: {e}", exc_info=True)
        return False
    finally:
        if conn:
            conn.close()


class StatsLookup:
    """
    Provides access to Wikipedia statistics stored in an SQLite database.
    Designed to be used as a context manager for connection handling.
    Uses a thread lock for safe concurrent reads if the instance is shared.
    """

    def __init__(self, db_path: str):
        """
        Initializes the lookup service.

        Args:
            db_path: Absolute path to the SQLite database file containing the stats.
        """
        self.db_path = db_path
        self.conn: Optional[sqlite3.Connection] = None
        # Use a lock for thread safety if the same StatsLookup instance might be
        # accessed by multiple threads concurrently (e.g., in a web server context).
        # For a sequential pipeline script, this might be less critical but adds safety.
        self.lock = threading.Lock()
        if not os.path.exists(self.db_path):
            msg = f"Stats lookup database file not found: {self.db_path}"
            logger.error(msg)
            raise FileNotFoundError(msg)

    def _connect(self):
        """Establishes the SQLite database connection (read-only)."""
        with self.lock:
            if self.conn is None:
                try:
                    # Connect in read-only mode
                    # Use check_same_thread=False if threads might share the *same* instance
                    self.conn = sqlite3.connect(
                        f"file:{self.db_path}?mode=ro",
                        uri=True,
                        check_same_thread=False,
                    )
                    # Enable WAL mode for better read concurrency (best-effort)
                    try:
                        self.conn.execute("PRAGMA journal_mode=WAL;")
                    except sqlite3.Error:
                        pass  # Ignore if fails
                    logger.info(f"Connected to stats DB (RO): {self.db_path}")
                except sqlite3.Error as e:
                    logger.error(f"Failed to connect to stats DB {self.db_path}: {e}")
                    self.conn = None
                    raise  # Connection is critical

    def get_count(self, qid: str) -> Optional[int]:
        """Looks up the inlink count for a QID."""
        if self.conn is None:
            return None  # Not connected
        sql = "SELECT count FROM qid_counts WHERE qid = ?"
        cursor = None
        try:
            with self.lock:  # Ensure thread-safe access to the connection
                cursor = self.conn.cursor()
                cursor.execute(sql, (qid,))
                result = cursor.fetchone()
            return result[0] if result else None
        except sqlite3.Error as e:
            logger.error(f"SQLite error getting count for {qid}: {e}")
            return None
        finally:
            if cursor:
                cursor.close()  # Ensure cursor is closed

    def get_top_mentions(self, qid: str, limit: int) -> List[Dict[str, Any]]:
        """Looks up the top N mentions for a QID, ordered by count descending."""
        if self.conn is None:
            return []  # Not connected
        if limit <= 0:
            return []
        # Order by count DESC and limit in SQL for efficiency
        sql = "SELECT mention, count FROM qid_mentions WHERE qid = ? ORDER BY count DESC LIMIT ?"
        mentions: List[Dict[str, Any]] = []
        cursor = None
        try:
            with self.lock:  # Ensure thread-safe access
                cursor = self.conn.cursor()
                cursor.execute(sql, (qid, limit))
                results = cursor.fetchall()
            mentions = [{"mention": row[0], "count": row[1]} for row in results]
        except sqlite3.Error as e:
            logger.error(f"SQLite error getting mentions for {qid}: {e}")
        finally:
            if cursor:
                cursor.close()  # Ensure cursor is closed
        return mentions

    def close(self):
        """Closes the database connection."""
        with self.lock:
            if self.conn:
                self.conn.close()
                self.conn = None
                logger.info(f"Closed connection to stats DB: {self.db_path}")

    def __enter__(self):
        """Context manager entry: connect."""
        self._connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit: close."""
        self.close()


def find_entities_to_enrich(collection: Collection) -> Iterator[Dict[str, Any]]:
    """Finds entities in MongoDB that have an English Wikipedia sitelink and need enrichment."""
    # Query: Find entities with enwiki sitelink but no wiki_links field yet
    # We assume sitelinks were extracted during wikidata ingest if needed
    query = {
        # Check for the existence of the normalized title field created during ingest
        "sitelinks.enwiki.normalized_title": {"$exists": True},
        "wiki_links": {"$exists": False},
    }
    # Fetch fields needed: _id and the normalized title for Wikimapper lookup
    projection = {"_id": 1, "sitelinks.enwiki.normalized_title": 1}

    logger.info(f"Querying MongoDB for entities to enrich...")
    logger.debug(f"Using query: {query}, projection: {projection}")
    cursor = collection.find(query, projection, no_cursor_timeout=True)
    try:
        count = collection.count_documents(query, maxTimeMS=30000)  # 30 sec timeout
        logger.info(f"Found approximately {count:,} candidate entities for enrichment.")
    except Exception as e:
        logger.warning(f"Could not get exact count for enrichment candidates: {e}")
        logger.info("Proceeding with cursor iteration...")
    return cursor


def process_enrichment_batch(
    collection: Collection, config: Dict[str, Any], stats_lookup: StatsLookup
):
    """
    Finds entities and updates them in batches using the StatsLookup service.
    Uses WikimapperLookup to map normalized titles back to QIDs for stats lookup.
    """
    batch_size = pydash_get(config, "mongodb.batch_size", 1000)
    max_mentions_to_store = pydash_get(config, "enrichment.max_mentions_to_store", 10)
    updates: List[UpdateOne] = []
    processed_count = 0
    enriched_count = 0
    error_count = 0
    start_time = time.time()
    last_log_time = start_time

    entity_iterator = find_entities_to_enrich(collection)

    # Wikimapper is needed to map normalized title back to QID for stats lookup
    # Initialize outside the loop using context manager
    try:
        with WikimapperLookup(config) as wikimapper:
            logger.info("Starting enrichment processing loop...")
            for doc in entity_iterator:
                processed_count += 1
                doc_id = doc["_id"]
                # Use the pre-normalized title stored during ingest if available
                normalized_title = pydash_get(doc, "sitelinks.enwiki.normalized_title")

                if not normalized_title:
                    logger.warning(
                        f"Skipping doc {doc_id}: Missing 'sitelinks.enwiki.normalized_title' despite query?"
                    )
                    continue

                # --- Map Title to QID ---
                # We need the QID associated with the *normalized title* to look up stats
                target_qid = wikimapper.get_qid(normalized_title)

                inlink_count: Optional[int] = None
                top_mentions: List[Dict[str, Any]] = []

                if target_qid:
                    # --- Lookup Stats using SQLite Temp DB (via StatsLookup) ---
                    try:
                        inlink_count = stats_lookup.get_count(target_qid)
                        top_mentions = stats_lookup.get_top_mentions(
                            target_qid, max_mentions_to_store
                        )
                    except Exception as lookup_err:
                        logger.error(
                            f"Error looking up stats for QID {target_qid} (title '{normalized_title}'): {lookup_err}",
                            exc_info=True,
                        )
                        # Continue processing, but stats will be null/empty
                else:
                    # This might happen if wikimapper DB is older than wikidata dump,
                    # or normalization differences persist.
                    logger.warning(
                        f"Could not map normalized title '{normalized_title}' (from doc {doc_id}) back to QID using Wikimapper. Cannot fetch stats."
                    )
                    # Still store normalized title, but stats will be empty/null

                # Construct update data
                if inlink_count is not None or top_mentions:
                    enriched_count += 1
                    logger.debug(
                        f"Stats found for QID {target_qid} (title '{normalized_title}'): count={inlink_count}, mentions={len(top_mentions)}"
                    )
                else:
                    logger.debug(
                        f"No stats found for QID {target_qid} (title '{normalized_title}')"
                    )

                # Use field names from config for flexibility
                wiki_relevance_field = pydash_get(
                    config, "enrichment.wiki_relevance_field", "wiki_links.inlink_count"
                )
                wiki_mentions_field = pydash_get(
                    config, "enrichment.wiki_mentions_field", "wiki_links.top_mentions"
                )

                # Prepare the wiki_links subdocument
                wiki_links_data = {
                    "normalized_enwiki_title": normalized_title,
                    # Store stats under the configured field names (if simple dot notation)
                    # For simplicity, schema defines a 'wiki_links' object. We populate its fields.
                    "inlink_count": inlink_count,  # Matches schema 'wiki_links.inlink_count'
                    "top_mentions": top_mentions,  # Matches schema 'wiki_links.top_mentions'
                }

                # Prepare update op using UpdateOne
                updates.append(
                    UpdateOne(
                        {"_id": doc_id}, {"$set": {"wiki_links": wiki_links_data}}
                    )
                )

                # Execute Batch
                if len(updates) >= batch_size:
                    write_errors = 0
                    try:
                        logger.debug(f"Writing batch of {len(updates)} enrichments...")
                        result = collection.bulk_write(updates, ordered=False)
                        logger.debug(
                            f"Successfully wrote batch (modified: {result.modified_count})."
                        )
                    except BulkWriteError as bwe:
                        write_errors = len(bwe.details.get("writeErrors", []))
                        logger.error(
                            f"Bulk write error during enrichment ({write_errors} errors): {bwe.details}",
                            exc_info=False,
                        )  # Less verbose log
                        error_count += write_errors
                    except Exception as e:
                        write_errors = len(updates)
                        logger.error(
                            f"Unexpected error during bulk write: {e}", exc_info=True
                        )
                        error_count += write_errors
                    finally:
                        updates = []  # Clear batch regardless of success/failure

                    # Log progress
                    current_time = time.time()
                    if current_time - last_log_time > 30:
                        elapsed = current_time - start_time
                        rate = processed_count / elapsed if elapsed > 0 else 0
                        logger.info(
                            f"Processed {processed_count:,} entities ({enriched_count:,} enriched) in {elapsed:.2f}s ({rate:.2f}/sec). Write Errors: {error_count}"
                        )
                        last_log_time = current_time

            # Write final batch
            if updates:
                write_errors = 0
                try:
                    logger.debug(
                        f"Writing final batch of {len(updates)} enrichments..."
                    )
                    result = collection.bulk_write(updates, ordered=False)
                    logger.debug(
                        f"Successfully wrote final batch (modified: {result.modified_count})."
                    )
                except BulkWriteError as bwe:
                    write_errors = len(bwe.details.get("writeErrors", []))
                    logger.error(
                        f"Bulk write error during final enrichment batch ({write_errors} errors): {bwe.details}",
                        exc_info=False,
                    )
                    error_count += write_errors
                except Exception as e:
                    write_errors = len(updates)
                    logger.error(
                        f"Unexpected error during final bulk write: {e}", exc_info=True
                    )
                    error_count += write_errors

    except (
        FileNotFoundError,
        ConfigurationError,
        sqlite3.Error,
        MongoConfigError,
    ) as e:
        logger.error(
            f"Failed to initialize Wikimapper or StatsLookup: {e}", exc_info=True
        )
        raise  # Cannot proceed without these lookups

    finally:
        # Close entity cursor if necessary
        if isinstance(entity_iterator, Iterator):
            try:
                if hasattr(entity_iterator, "close"):
                    entity_iterator.close()  # type: ignore
            except Exception:
                pass

    # Final summary
    elapsed = time.time() - start_time
    logger.info("-" * 50)
    logger.info("Enrichment process finished.")
    logger.info(f"Total time: {elapsed:.2f} seconds")
    logger.info(f"Total MongoDB entities processed (candidates): {processed_count:,}")
    logger.info(
        f"Entities successfully enriched/updated with stats: {enriched_count:,}"
    )
    logger.info(f"Write errors encountered: {error_count:,}")
    logger.info("-" * 50)


def main():
    parser = argparse.ArgumentParser(
        description="Enrich MongoDB Wikidata entities with Wikipedia link statistics."
    )
    parser.add_argument(
        "--config",
        default="config/config.yaml",
        help="Path to the global YAML configuration file.",
    )
    parser.add_argument(
        "--stats-format",
        default=None,
        help="Override stats format (json, sqlite, mongo_collection) from config.",
    )
    parser.add_argument(
        "--stats-location",
        default=None,
        help="Override stats location (path or collection) from config.",
    )
    parser.add_argument(
        "--temp-db-path",
        default=None,
        help=f"Override path for temporary stats SQLite DB if created from JSONL (default: {TEMP_STATS_DB_NAME} in project root)",
    )
    parser.add_argument(
        "--skip-temp-db-creation",
        action="store_true",
        help="Skip creating the temp stats DB from JSONL (assume it exists)",
    )
    parser.add_argument(
        "--keep-temp-db",
        action="store_true",
        help="Keep the temporary stats DB after completion (for debugging)",
    )
    args = parser.parse_args()

    config: Optional[Dict[str, Any]] = None
    stats_lookup_service: Optional[StatsLookup] = None
    temp_db_file_path: Optional[str] = None
    actual_stats_format: Optional[str] = None
    base_dir: str = get_base_dir()  # Get project root early

    try:
        config = load_config(config_path=args.config, base_dir=base_dir)
        setup_logging(config)

        # Apply overrides
        if args.stats_format:
            from pydash import set_ as pydash_set

            pydash_set(
                config, "wikipedia_processing.stats_output_format", args.stats_format
            )
            logger.info(f"Overriding stats format to: {args.stats_format}")
        if args.stats_location:
            from pydash import set_ as pydash_set

            pydash_set(
                config,
                "wikipedia_processing.stats_output_location",
                args.stats_location,
            )
            logger.info(f"Overriding stats location to: {args.stats_location}")

        if not pydash_get(config, "enrichment.enabled", False):
            logger.info(
                "Enrichment step is disabled in the configuration ('enrichment.enabled'). Skipping."
            )
            sys.exit(0)

        # Determine stats format and location
        actual_stats_format = pydash_get(
            config, "wikipedia_processing.stats_output_format", "json"
        ).lower()
        stats_location = pydash_get(
            config, "wikipedia_processing.stats_output_location"
        )

        if not stats_location:
            raise ConfigurationError(
                f"Stats location ('wikipedia_processing.stats_output_location') is required when enrichment is enabled."
            )

        # --- Handle Stats Loading Strategy ---
        stats_db_path_for_lookup: Optional[str] = None

        if actual_stats_format == "json":
            # Determine path for temporary stats DB
            temp_db_file_path = args.temp_db_path or os.path.join(
                base_dir, TEMP_STATS_DB_NAME
            )
            logger.info(f"Using temporary stats SQLite DB at: {temp_db_file_path}")

            if not args.skip_temp_db_creation:
                success = create_temp_stats_db(config, temp_db_file_path)
                if not success:
                    logger.critical(
                        "Failed to create temporary stats DB from JSONL files. Cannot proceed."
                    )
                    sys.exit(1)
            elif not os.path.exists(temp_db_file_path):
                logger.critical(
                    f"Temporary stats DB creation skipped, but file not found: {temp_db_file_path}"
                )
                sys.exit(1)
            else:
                logger.info(
                    f"Skipping temp DB creation, assuming existing file is valid: {temp_db_file_path}"
                )
            # Use the temporary SQLite DB for lookups
            stats_db_path_for_lookup = temp_db_file_path

        elif actual_stats_format == "sqlite":
            # Use the SQLite DB generated directly by process_wiki_stats
            # Resolve path relative to base_dir if needed
            sqlite_db_path = stats_location
            if not os.path.isabs(sqlite_db_path):
                sqlite_db_path = os.path.abspath(os.path.join(base_dir, sqlite_db_path))

            if not os.path.exists(sqlite_db_path):
                logger.critical(
                    f"SQLite stats DB specified but not found at: {sqlite_db_path}"
                )
                sys.exit(1)
            logger.info(f"Using direct SQLite stats DB for lookup: {sqlite_db_path}")
            stats_db_path_for_lookup = sqlite_db_path  # Use same class

        elif actual_stats_format == "mongo_collection":
            logger.error(
                "Loading stats from MongoDB not implemented for enrichment yet."
            )
            raise NotImplementedError(
                "Loading stats from MongoDB required for enrichment"
            )
            # PI Need to implement a StatsLookup class variant for Mongo
        else:
            logger.error(
                f"Unsupported stats format for enrichment: {actual_stats_format}"
            )
            raise ConfigurationError(f"Unsupported stats format: {actual_stats_format}")

        # --- Run Enrichment ---
        if stats_db_path_for_lookup:
            with StatsLookup(stats_db_path_for_lookup) as stats_lookup_service:
                collection = get_entity_collection(config)
                process_enrichment_batch(collection, config, stats_lookup_service)
        else:
            # Should have been caught earlier, but safeguard
            logger.critical("Stats lookup service could not be initialized.")
            sys.exit(1)

    except (
        FileNotFoundError,
        ConfigurationError,
        ConnectionFailure,
        NotImplementedError,
        sqlite3.Error,
        MongoConfigError,
    ) as e:
        logger.critical(f"Enrichment script failed: {e}", exc_info=True)
        sys.exit(1)
    except Exception as e:
        logger.critical(
            f"An unexpected error occurred during enrichment: {e}", exc_info=True
        )
        sys.exit(1)
    finally:
        # Clean up: Optionally remove the temporary SQLite DB if created from JSONL
        if (
            temp_db_file_path
            and actual_stats_format == "json"
            and os.path.exists(temp_db_file_path)
        ):
            if not args.keep_temp_db:
                logger.info(f"Removing temporary stats DB: {temp_db_file_path}")
                try:
                    os.remove(temp_db_file_path)
                except OSError as e:
                    logger.warning(
                        f"Could not remove temporary stats DB {temp_db_file_path}: {e}"
                    )
            else:
                logger.info(f"Keeping temporary stats DB: {temp_db_file_path}")


if __name__ == "__main__":
    main()
