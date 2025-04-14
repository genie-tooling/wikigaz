import sqlite3
import logging
import os
from typing import Optional, Dict, Any
from threading import Lock

# Make utils discoverable if run standalone (e.g., for testing)
try:
    # Use absolute imports within the package
    from .config import (
        pydash_get,
        ConfigurationError,
        get_base_dir,
    )  # Added get_base_dir
    from .helpers import (
        normalize_wikipedia_title,
    )  # Import for potential testing/example
except ImportError:
    # Allow running standalone for testing utils/mapping.py itself
    import sys

    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
    from utils.config import pydash_get, ConfigurationError, get_base_dir
    from utils.helpers import normalize_wikipedia_title

logger = logging.getLogger(__name__)


class WikimapperLookup:
    """
    Provides an interface to query a Wikimapper SQLite database
    for Wikipedia title to Wikidata QID mappings.

    Designed to be used as a context manager:
    with WikimapperLookup(config) as mapper:
        qid = mapper.get_qid("Normalized_title")

    Uses a thread lock for safe concurrent access if the same instance is shared.

    Attributes:
        db_path (str): Absolute path to the SQLite database file.
        conn (Optional[sqlite3.Connection]): The SQLite connection object.
        lock (Lock): Thread lock for safe concurrent access.
    """

    def __init__(self, config: Dict[str, Any]):
        """
        Initializes the lookup service with the database path from config.

        Args:
            config: The application configuration dictionary.

        Raises:
            FileNotFoundError: If the database file does not exist.
            ConfigurationError: If the database path is not configured.
        """
        # Config path should be absolute after config loading
        db_path_cfg = pydash_get(config, "paths.wikimapper_db")
        if not db_path_cfg:
            msg = (
                "Wikimapper DB path not found in configuration ('paths.wikimapper_db')."
            )
            logger.critical(msg)
            raise ConfigurationError(msg)

        # Ensure the path is absolute
        self.db_path: str
        if os.path.isabs(db_path_cfg):
            self.db_path = db_path_cfg
        else:
            # Resolve relative to project base directory
            base_dir = get_base_dir()
            self.db_path = os.path.abspath(os.path.join(base_dir, db_path_cfg))
            logger.debug(f"Resolved relative wikimapper DB path to: {self.db_path}")

        if not os.path.exists(self.db_path):
            msg = f"Wikimapper database file not found at resolved path: {self.db_path}"
            logger.critical(msg)
            raise FileNotFoundError(msg)

        self.conn: Optional[sqlite3.Connection] = None
        self.lock = Lock()
        logger.info(f"WikimapperLookup initialized with DB: {self.db_path}")

    def _connect(self):
        """Establishes the SQLite database connection (read-only)."""
        # Acquire lock before checking/establishing connection
        with self.lock:
            if self.conn is None:
                try:
                    # Connect in read-only mode for safety in query operations
                    # check_same_thread=False allows sharing across threads if needed
                    self.conn = sqlite3.connect(
                        f"file:{self.db_path}?mode=ro",
                        uri=True,
                        check_same_thread=False,
                    )
                    # Optional: WAL mode for better read concurrency (best effort)
                    try:
                        self.conn.execute("PRAGMA journal_mode=WAL;")
                    except sqlite3.Error as e_wal:
                        logger.warning(
                            f"Could not set WAL mode on {self.db_path}: {e_wal}"
                        )
                    logger.info(f"Connected to Wikimapper DB (RO): {self.db_path}")
                except sqlite3.Error as e:
                    logger.error(
                        f"Failed to connect to SQLite DB at {self.db_path}: {e}",
                        exc_info=True,
                    )
                    self.conn = None
                    raise  # Re-raise as connection is critical

    def get_qid(self, normalized_title: str) -> Optional[str]:
        """
        Retrieves the Wikidata QID for a canonically normalized Wikipedia title.

        Args:
            normalized_title: The Wikipedia title, pre-normalized using
                              utils.helpers.normalize_wikipedia_title.

        Returns:
            The corresponding Wikidata QID (e.g., "Q123"), or None if not found or on error.
        """
        if self.conn is None:
            self._connect()  # Attempt connection if used outside context manager
            if self.conn is None:  # Check again after connect attempt
                logger.error(
                    "Attempted to query Wikimapper DB without an active connection."
                )
                return None

        if not normalized_title:  # Handle empty string input gracefully
            return None

        # --- Wikimapper Schema Confirmation & Query - CORRECTED ---
        # Actual schema: table='mapping', columns='wikipedia_title', 'wikidata_id'
        table_name = "mapping"
        title_column = "wikipedia_title"  # Corrected
        qid_column = "wikidata_id"  # Corrected
        # Use parameterized query to prevent SQL injection
        sql = f"SELECT {qid_column} FROM {table_name} WHERE {title_column} = ?"
        # --- End Confirmation ---

        qid_result: Optional[str] = None
        cursor: Optional[sqlite3.Cursor] = None
        try:
            # Using lock and creating cursor per-call for thread safety if instance is shared
            with self.lock:
                cursor = self.conn.cursor()
                cursor.execute(sql, (normalized_title,))
                result = (
                    cursor.fetchone()
                )  # fetchone is efficient for primary key lookups
                cursor.close()
                cursor = None  # Help GC

            if result:
                qid_result = result[0]
                logger.debug(
                    f"Wikimapper lookup: '{normalized_title}' -> '{qid_result}'"
                )
            else:
                logger.debug(f"Wikimapper lookup: '{normalized_title}' -> Not Found")
                qid_result = None  # Explicitly None if not found

        except sqlite3.Error as e:
            logger.error(
                f"SQLite error looking up title '{normalized_title}': {e}",
                exc_info=False,
            )
            qid_result = None  # Return None on error
        except Exception as e:
            logger.error(
                f"Unexpected error during QID lookup for '{normalized_title}': {e}",
                exc_info=True,
            )
            qid_result = None
        finally:
            # Ensure cursor is closed if error happened before lock release or outside 'with'
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass

        return qid_result

    def close(self):
        """Closes the SQLite database connection if it's open."""
        with self.lock:
            if self.conn:
                try:
                    self.conn.close()
                    logger.info(f"Closed connection to Wikimapper DB: {self.db_path}")
                    self.conn = None
                except sqlite3.Error as e:
                    logger.error(
                        f"Error closing SQLite connection for {self.db_path}: {e}",
                        exc_info=True,
                    )

    def __enter__(self):
        """Context manager entry: connect to the database."""
        self._connect()  # Ensure connection is established
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit: close the database connection."""
        self.close()


# Example Usage (requires a wikimapper DB file)
if __name__ == "__main__":
    # Setup basic logging for example
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s - %(levelname)s [%(name)s] - %(message)s",
    )
    logger.info("Running WikimapperLookup example...")

    # --- Create a dummy config and DB for testing ---
    # Note: This requires creating a dummy SQLite file.
    test_dir = os.path.abspath("temp_test_wikimapper")
    os.makedirs(test_dir, exist_ok=True)
    dummy_db_path = os.path.join(test_dir, "dummy_index.db")
    # Get base dir for resolving relative paths in config
    base_dir = get_base_dir()  # Use the util function
    dummy_config = {
        "paths": {
            # Store path relative to base_dir for testing resolution
            "wikimapper_db": os.path.relpath(dummy_db_path, base_dir)
        }
    }

    # Create and populate dummy DB with CORRECT schema
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = sqlite3.connect(dummy_db_path)
        cursor = conn.cursor()
        cursor.execute("DROP TABLE IF EXISTS mapping;")
        # *** Use Correct Column Names ***
        cursor.execute(
            "CREATE TABLE mapping (wikipedia_title TEXT PRIMARY KEY, wikidata_id TEXT NOT NULL);"
        )
        cursor.executemany(
            "INSERT INTO mapping (wikipedia_title, wikidata_id) VALUES (?, ?)",
            [
                ("United States", "Q30"),
                ("Los Angeles", "Q65"),
                ("Python (programming language)", "Q28865"),
                ("Café René", "Q12345"),  # Example with unicode
            ],
        )
        conn.commit()
        print(f"Created dummy Wikimapper DB at {dummy_db_path}")
    except sqlite3.Error as e:
        print(f"Error creating dummy DB: {e}")
    finally:
        if conn:
            conn.close()

    # Test lookup
    try:
        print("\n--- Testing with Context Manager ---")
        # Pass the full config dict
        with WikimapperLookup(dummy_config) as mapper:
            # Use normalize_wikipedia_title to ensure correct input format for lookup
            title1 = normalize_wikipedia_title("United_States")
            qid1 = mapper.get_qid(title1)
            print(f"Lookup '{title1}': {qid1}")
            assert qid1 == "Q30"

            title2 = normalize_wikipedia_title(
                "los angeles"
            )  # Test normalization + lookup
            qid2 = mapper.get_qid(title2)
            print(f"Lookup '{title2}': {qid2}")
            assert qid2 == "Q65"

            title3 = normalize_wikipedia_title("NonExistentPage")
            qid3 = mapper.get_qid(title3)
            print(f"Lookup '{title3}': {qid3}")
            assert qid3 is None

            title4 = normalize_wikipedia_title("Café_René")  # Unicode test
            qid4 = mapper.get_qid(title4)
            print(f"Lookup '{title4}': {qid4}")
            assert qid4 == "Q12345"

            print("Context manager test finished.")

    except (ConfigurationError, FileNotFoundError, sqlite3.Error) as e:
        print(f"\n*** WikimapperLookup example failed: {e}")
    except Exception as e:
        print(f"\n*** An unexpected error occurred: {e}")
        import traceback

        traceback.print_exc()
    finally:
        # Clean up dummy DB and dir
        print("\n--- Cleaning up dummy files ---")
        if os.path.exists(dummy_db_path):
            try:
                os.remove(dummy_db_path)
                print(f"Removed {dummy_db_path}")
            except Exception as e:
                print(f"Error removing dummy DB: {e}")
        if os.path.exists(test_dir):
            try:
                os.rmdir(test_dir)
                print(f"Removed {test_dir}")
            except Exception as e:
                print(f"Error removing test dir: {e}")
