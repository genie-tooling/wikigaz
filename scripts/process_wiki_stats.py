#!/usr/bin/env python3
import logging
import argparse
import sys
import bz2
import json
import re
import time
import os
import sqlite3  # Added for SQLite output
from collections import Counter, defaultdict
from typing import (
    Dict,
    Any,
    Optional,
    Tuple,
    Generator,
    DefaultDict,
    List,
    Set,
)  # Added Set

# Use lxml if available for performance, otherwise fallback to ElementTree
try:
    from lxml import etree

    _parser_module = "lxml"
except ImportError:
    from xml.etree import ElementTree as etree

    _parser_module = "etree"

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
from utils.helpers import normalize_wikipedia_title  # Use canonical normalization
from utils.mapping import WikimapperLookup  # Import the lookup class

logger = logging.getLogger(__name__)

# Pre-compile regex for finding MediaWiki links: [[Target|Anchor]] or [[Target]]
# Handles optional fragment (#...) which is ignored before normalization.
WIKI_LINK_REGEX = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]+))?\]\]")

# Global variable to hold the mapper instance within the script's context
_wiki_mapper_instance: Optional[WikimapperLookup] = None


def _initialize_wikimapper_global(config: Dict[str, Any]):
    """Initializes the global Wikimapper instance for this script."""
    global _wiki_mapper_instance
    if _wiki_mapper_instance is None:
        logger.info("Initializing global WikimapperLookup instance...")
        try:
            # Instantiate without 'with', connection managed internally or by close call
            _wiki_mapper_instance = WikimapperLookup(config)
            # Perform a quick check to ensure DB is accessible
            _wiki_mapper_instance._connect()  # Ensure connection works upfront
            _wiki_mapper_instance.close()  # Close immediately after check (will reconnect on demand)
            logger.info("Global WikimapperLookup instance initialized successfully.")
        except (FileNotFoundError, ConfigurationError, sqlite3.Error) as e:
            logger.critical(f"Failed to initialize Wikimapper: {e}", exc_info=True)
            _wiki_mapper_instance = None  # Ensure it's None on failure
            raise  # Re-raise to halt script as it's required


def get_qid_from_wiki_title_local(normalized_title: str) -> Optional[str]:
    """Looks up QID using the script's global Wikimapper instance. Handles connection."""
    if _wiki_mapper_instance is None:
        logger.error("Wikimapper instance not initialized before lookup.")
        return None
    try:
        # WikimapperLookup handles connection internally now
        return _wiki_mapper_instance.get_qid(normalized_title)
    except Exception as e:
        logger.error(f"Error during Wikimapper lookup for '{normalized_title}': {e}")
        return None


def close_wikimapper_global():
    """Closes the global Wikimapper instance connection."""
    global _wiki_mapper_instance
    if _wiki_mapper_instance:
        logger.info("Closing global WikimapperLookup instance connection.")
        _wiki_mapper_instance.close()
        _wiki_mapper_instance = None


def parse_wikipedia_dump(
    dump_path: str,
) -> Generator[Tuple[str, str, str, bool], None, None]:
    """
    Parses the Wikipedia XML dump using iterparse for memory efficiency.
    Yields tuples of (page_title, page_text, page_id, is_redirect).
    """
    logger.info(
        f"Starting Wikipedia XML stream parsing from: {dump_path} using {_parser_module}"
    )
    ns = "{http://www.mediawiki.org/xml/export-0.10/}"
    tag_page = f"{ns}page"
    tag_title = f"{ns}title"
    tag_text = f"{ns}text"
    tag_id = f"{ns}id"
    tag_revision = f"{ns}revision"
    tag_redirect = f"{ns}redirect"

    page_count = 0
    processed_page_count = 0
    start_time = time.time()
    last_log_time = start_time

    try:
        with bz2.open(dump_path, "rt", encoding="utf-8", errors="ignore") as f:
            context = etree.iterparse(f, events=("end",), tag=tag_page)  # type: ignore

            for event, elem in context:
                page_count += 1
                try:
                    page_id_elem = elem.find(
                        f".//{tag_id}", namespaces={"": ns.strip("{}")}
                    )
                    page_title_elem = elem.find(
                        f".//{tag_title}", namespaces={"": ns.strip("{}")}
                    )
                    revision_elem = elem.find(
                        f".//{tag_revision}", namespaces={"": ns.strip("{}")}
                    )
                    redirect_elem = elem.find(
                        f".//{tag_redirect}", namespaces={"": ns.strip("{}")}
                    )

                    page_id = page_id_elem.text if page_id_elem is not None else None
                    page_title = (
                        page_title_elem.text if page_title_elem is not None else None
                    )
                    is_redirect = redirect_elem is not None

                    page_text = None
                    if not is_redirect and revision_elem is not None:
                        text_elem = revision_elem.find(
                            f".//{tag_text}", namespaces={"": ns.strip("{}")}
                        )
                        if text_elem is not None:
                            page_text = text_elem.text

                    if page_title and page_id:
                        if not is_redirect and page_text:
                            yield page_title, page_text, page_id, False
                            processed_page_count += 1
                        # else: # Optionally yield redirects if needed later
                        #     yield page_title, redirect_elem.get('title', ''), page_id, True

                    # Logging progress periodically
                    current_time = time.time()
                    if current_time - last_log_time > 60:  # Log every 60 seconds
                        elapsed = current_time - start_time
                        pages_per_sec = page_count / elapsed if elapsed > 0 else 0
                        logger.info(
                            f"Parsed {page_count:,} pages ({processed_page_count:,} processed non-redirects) in {elapsed:.2f}s ({pages_per_sec:.2f} pages/sec)..."
                        )
                        last_log_time = current_time

                except Exception as page_err:
                    logger.warning(
                        f"Error processing page element near count {page_count}: {page_err}",
                        exc_info=False,
                    )
                    # Continue to next page element

                finally:
                    # --- Memory Management: Critical for iterparse ---
                    elem.clear()
                    # Also remove siblings to prevent memory leak in lxml/etree
                    # This part is tricky, check parent element exists before trying to remove
                    parent = elem.getparent()
                    if parent is not None:
                        # Find index of current element and remove it
                        try:
                            parent.remove(elem)
                        except (
                            ValueError
                        ):  # Element might have already been removed by parent manipulation?
                            pass
                    # Alternative less safe: remove previous siblings?
                    # while elem.getprevious() is not None:
                    #     try: del elem.getparent()[0]
                    #     except TypeError: break # No parent?

    except etree.ParseError as e:
        logger.error(
            f"XML Parsing error in {dump_path} (approx page {page_count}): {e}",
            exc_info=False,
        )
        logger.error("Attempting to continue parsing if possible...")
    except Exception as e:
        logger.critical(
            f"Error processing Wikipedia dump {dump_path}: {e}", exc_info=True
        )
        raise

    elapsed = time.time() - start_time
    logger.info(
        f"Finished parsing Wikipedia dump. Parsed {page_count:,} total pages, processed {processed_page_count:,} non-redirect pages in {elapsed:.2f} seconds."
    )


def aggregate_stats(
    parser_gen: Generator[Tuple[str, str, str, bool], None, None],
    config: Dict[str, Any],
) -> Tuple[DefaultDict[str, int], DefaultDict[str, Counter]]:
    """
    Aggregates link counts (QID -> count) and mention frequencies (QID -> mention -> count).
    Uses in-memory dictionaries - PI must monitor RAM usage.
    Requires global Wikimapper instance to be initialized.
    """
    if _wiki_mapper_instance is None:
        raise RuntimeError(
            "Wikimapper must be initialized before calling aggregate_stats."
        )

    logger.info(
        "Starting aggregation of Wikipedia link statistics (using in-memory dicts)..."
    )
    qid_inlink_counts: DefaultDict[str, int] = defaultdict(int)
    qid_mention_counts: DefaultDict[str, Counter] = defaultdict(Counter)
    unmapped_targets_counter: Counter = Counter()
    link_parse_errors = 0
    total_links_found = 0
    processed_page_count = 0
    start_time = time.time()
    last_log_time = start_time

    # Connect Wikimapper DB for the duration of aggregation
    _wiki_mapper_instance._connect()

    try:  # Ensure wikimapper connection is closed even if loop fails
        for raw_page_title, page_text, page_id, _ in parser_gen:
            processed_page_count += 1

            try:
                # Find all links in the page text
                for match in WIKI_LINK_REGEX.finditer(page_text):
                    total_links_found += 1
                    target_title_raw = match.group(1).strip()
                    # Use anchor text if present, otherwise use target title
                    anchor_text_raw = (
                        match.group(2).strip() if match.group(2) else target_title_raw
                    )

                    # Normalize the target title using the canonical helper
                    normalized_target_title = normalize_wikipedia_title(
                        target_title_raw
                    )

                    # Skip empty, invalid, or special namespace titles
                    if not normalized_target_title or ":" in normalized_target_title:
                        continue

                    # --- Wikimapper Lookup (using local helper accessing global instance) ---
                    target_qid = get_qid_from_wiki_title_local(normalized_target_title)

                    if target_qid:
                        qid_inlink_counts[target_qid] += 1
                        # Use raw anchor text as the mention key
                        qid_mention_counts[target_qid][anchor_text_raw] += 1
                    else:
                        # Track titles that couldn't be mapped
                        unmapped_targets_counter[normalized_target_title] += 1

            except Exception as e:
                logger.error(
                    f"Error processing links for page ID {page_id} ('{raw_page_title}'): {e}",
                    exc_info=False,
                )
                link_parse_errors += 1

            # Log progress periodically
            current_time = time.time()
            if current_time - last_log_time > 60:
                elapsed = current_time - start_time
                pages_per_sec = processed_page_count / elapsed if elapsed > 0 else 0
                # RAM usage check could be added here if psutil is available & desired
                # import psutil
                # process = psutil.Process(os.getpid())
                # mem_mb = process.memory_info().rss / (1024 * 1024)
                # logger.info(f"Aggregated {processed_page_count:,} pages ({mem_mb:.1f} MB RAM)...")
                logger.info(
                    f"Aggregated {processed_page_count:,} pages, found {total_links_found:,} links ({len(qid_inlink_counts):,} unique QIDs) in {elapsed:.2f}s ({pages_per_sec:.2f} pages/sec)..."
                )
                last_log_time = current_time
    finally:
        # Ensure connection is closed after aggregation finishes or fails
        _wiki_mapper_instance.close()

    elapsed = time.time() - start_time
    logger.info(f"Aggregation complete in {elapsed:.2f} seconds.")
    logger.info(f"Total non-redirect pages processed: {processed_page_count:,}")
    logger.info(f"Total links found (regex matches): {total_links_found:,}")
    logger.info(f"Unique QIDs with incoming links: {len(qid_inlink_counts):,}")
    logger.info(f"Link parsing errors encountered: {link_parse_errors:,}")
    if unmapped_targets_counter:
        total_unmapped_links = sum(unmapped_targets_counter.values())
        unique_unmapped_titles = len(unmapped_targets_counter)
        logger.warning(
            f"Could not map {total_unmapped_links:,} links ({unique_unmapped_titles:,} unique titles) to QIDs."
        )
        top_n = 20
        try:
            top_unmapped = unmapped_targets_counter.most_common(top_n)
            logger.warning(
                f"Top {len(top_unmapped)} unmapped target titles (normalized): {top_unmapped}"
            )
        except Exception as mc_err:
            logger.error(f"Could not get most_common unmapped titles: {mc_err}")

    return qid_inlink_counts, qid_mention_counts


def save_stats_jsonl(
    qid_counts: DefaultDict[str, int],
    qid_mentions: DefaultDict[str, Counter],
    output_location: str,
    config: Dict[str, Any],
):
    """Saves stats as JSON Lines files (memory efficient)."""
    counts_path = os.path.join(output_location, "qid_inlink_counts.jsonl")
    mentions_path = os.path.join(output_location, "qid_mentions.jsonl")
    # Max mentions are handled during enrichment, save all here for flexibility
    # max_mentions_per_qid = pydash_get(config, 'enrichment.max_mentions_to_store', 10)

    logger.info(
        f"Saving QID inlink counts ({len(qid_counts):,} entries) to {counts_path}..."
    )
    try:
        with open(counts_path, "w", encoding="utf-8") as f_counts:
            for qid, count in qid_counts.items():
                json.dump({"qid": qid, "count": count}, f_counts)
                f_counts.write("\n")
    except IOError as e:
        logger.error(
            f"Failed to write counts JSONL file {counts_path}: {e}", exc_info=True
        )
        raise  # Re-raise as this is critical output

    logger.info(
        f"Saving QID mention counts ({len(qid_mentions):,} QIDs) to {mentions_path}..."
    )
    mentions_written = 0
    try:
        with open(mentions_path, "w", encoding="utf-8") as f_mentions:
            for qid, mention_counter in qid_mentions.items():
                for mention, count in mention_counter.items():
                    json.dump(
                        {"qid": qid, "mention": mention, "count": count}, f_mentions
                    )
                    f_mentions.write("\n")
                    mentions_written += 1
    except IOError as e:
        logger.error(
            f"Failed to write mentions JSONL file {mentions_path}: {e}", exc_info=True
        )
        raise  # Re-raise as this is critical output

    logger.info(f"Saved {mentions_written:,} total mention entries to {mentions_path}.")


def save_stats_sqlite(
    qid_counts: DefaultDict[str, int],
    qid_mentions: DefaultDict[str, Counter],
    output_location: str,  # Expects DB file path here
    config: Dict[str, Any],
):
    """Saves stats into an SQLite database with batching."""
    db_path = output_location  # Assume location is the db path for sqlite
    logger.info(f"Saving aggregated stats to SQLite DB: {db_path}")

    # Ensure directory exists
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    # Remove existing DB file first? Or let INSERT OR REPLACE handle it?
    # Removing ensures a clean state if the script is rerun.
    if os.path.exists(db_path):
        logger.warning(f"Removing existing SQLite stats DB: {db_path}")
        try:
            os.remove(db_path)
        except OSError as e:
            logger.error(f"Failed to remove existing stats DB {db_path}: {e}")
            # Decide whether to continue or raise

    conn: Optional[sqlite3.Connection] = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        # Consider WAL mode for potential concurrent reads later?
        # cursor.execute("PRAGMA journal_mode=WAL;")

        # Define table schemas and CREATE TABLE. Use IF NOT EXISTS just in case.
        # Use WITHOUT ROWID and PRIMARY KEYs for potentially better performance/space.
        cursor.execute(
            """
        CREATE TABLE IF NOT EXISTS qid_counts (
            qid TEXT PRIMARY KEY,
            count INTEGER NOT NULL
        ) WITHOUT ROWID;
        """
        )
        cursor.execute(
            """
        CREATE TABLE IF NOT EXISTS qid_mentions (
            qid TEXT NOT NULL,
            mention TEXT NOT NULL,
            count INTEGER NOT NULL,
            PRIMARY KEY (qid, mention)
        ) WITHOUT ROWID;
        """
        )
        # Index on QID for mentions table is useful for enrichment lookup
        cursor.execute(
            """
        CREATE INDEX IF NOT EXISTS idx_qid_mentions_qid ON qid_mentions (qid);
        """
        )
        conn.commit()  # Commit schema changes

        # Prepare data for insertion. Use executemany for efficiency.
        logger.info(f"Inserting {len(qid_counts):,} QID counts into SQLite...")
        counts_data = list(qid_counts.items())  # Convert defaultdict to list of tuples
        # Use INSERT OR REPLACE (or IGNORE) depending on desired behavior if run multiple times
        # REPLACE is safer if aggregation logic might change between runs.
        cursor.executemany(
            "INSERT OR REPLACE INTO qid_counts (qid, count) VALUES (?, ?)", counts_data
        )
        conn.commit()  # Commit after counts insert
        logger.info(f"Finished inserting counts.")

        logger.info(f"Inserting mention counts into SQLite (batching)...")
        mention_data_buffer: List[Tuple[str, str, int]] = []
        buffer_size = 100000  # Adjust buffer size based on memory/performance
        mentions_processed = 0
        # Iterate through mentions efficiently
        mention_iterator = (
            (qid, mention, count)
            for qid, counter in qid_mentions.items()
            for mention, count in counter.items()
        )

        for qid, mention, count in mention_iterator:
            mention_data_buffer.append((qid, mention, count))
            mentions_processed += 1
            if len(mention_data_buffer) >= buffer_size:
                cursor.executemany(
                    "INSERT OR REPLACE INTO qid_mentions (qid, mention, count) VALUES (?, ?, ?)",
                    mention_data_buffer,
                )
                conn.commit()  # Commit periodically
                logger.debug(f"Inserted {mentions_processed:,} mentions into SQLite...")
                mention_data_buffer = []
        # Insert remaining buffer
        if mention_data_buffer:
            cursor.executemany(
                "INSERT OR REPLACE INTO qid_mentions (qid, mention, count) VALUES (?, ?, ?)",
                mention_data_buffer,
            )
            conn.commit()  # Final commit
            logger.debug(f"Inserted final {len(mention_data_buffer):,} mentions.")

        logger.info(
            f"Successfully saved {mentions_processed:,} mention entries to SQLite DB {db_path}."
        )

    except sqlite3.Error as e:
        logger.error(f"SQLite error saving stats to {db_path}: {e}", exc_info=True)
        if conn:
            conn.rollback()  # Rollback partial changes on error
        raise  # Re-raise
    finally:
        if conn:
            conn.close()


def save_stats(
    qid_counts: DefaultDict[str, int],
    qid_mentions: DefaultDict[str, Counter],
    config: Dict[str, Any],
):
    """Saves the aggregated statistics based on config."""
    output_format = pydash_get(
        config, "wikipedia_processing.stats_output_format", "json"
    ).lower()
    output_location = pydash_get(config, "wikipedia_processing.stats_output_location")
    base_dir = get_base_dir()  # Get project root

    if not output_location:
        raise ConfigurationError(
            "Output location ('wikipedia_processing.stats_output_location') not specified."
        )

    # Resolve output_location relative to base_dir if it's not absolute
    abs_output_location = output_location
    if not os.path.isabs(abs_output_location):
        abs_output_location = os.path.abspath(
            os.path.join(base_dir, abs_output_location)
        )

    # Ensure output dir exists for file-based formats
    output_dir = (
        abs_output_location
        if output_format == "json"
        else os.path.dirname(abs_output_location)
    )
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    logger.info(
        f"Saving aggregated stats (Format: '{output_format}', Location: '{abs_output_location}')..."
    )
    start_time = time.time()

    try:
        if output_format == "json":
            save_stats_jsonl(qid_counts, qid_mentions, abs_output_location, config)
        elif output_format == "sqlite":
            save_stats_sqlite(qid_counts, qid_mentions, abs_output_location, config)
        elif output_format == "mongo_collection":
            logger.error("Saving stats to MongoDB not implemented yet.")
            raise NotImplementedError("Saving stats to MongoDB required")
            # PI Tasks: Connect, structure docs, bulk_write with ReplaceOne+upsert.
        else:
            logger.error(f"Unsupported output format specified: {output_format}")
            raise ConfigurationError(f"Unsupported output format: {output_format}")

        elapsed = time.time() - start_time
        logger.info(f"Finished saving stats in {elapsed:.2f} seconds.")

    except Exception as e:
        logger.critical(f"Failed to save statistics: {e}", exc_info=True)
        sys.exit(1)  # Exit as saving output is critical


def main():
    parser = argparse.ArgumentParser(
        description="Process Wikipedia XML dump to extract link statistics."
    )
    parser.add_argument(
        "--config",
        default="config/config.yaml",
        help="Path to the global YAML configuration file.",
    )
    parser.add_argument(
        "--wiki-dump", default=None, help="Override path to Wikipedia XML dump file."
    )
    parser.add_argument(
        "--output-format",
        default=None,
        help="Override output format (json, sqlite, mongo_collection)",
    )
    parser.add_argument(
        "--output-location",
        default=None,
        help="Override output location (dir path for json, DB path for sqlite, collection name for mongo)",
    )
    parser.add_argument(
        "--wikimapper-db", default=None, help="Override path to Wikimapper database."
    )
    # Add arg for memory profiling if implemented
    # parser.add_argument("--profile-memory", action="store_true", help="Enable memory profiling for aggregation.")

    args = parser.parse_args()

    config: Optional[Dict[str, Any]] = None
    base_dir = get_base_dir()  # Get project root

    try:
        config = load_config(config_path=args.config, base_dir=base_dir)
        setup_logging(config)  # Setup logging ASAP

        # Apply overrides from command line arguments
        if args.output_format:
            from pydash import set_ as pydash_set

            pydash_set(
                config, "wikipedia_processing.stats_output_format", args.output_format
            )
            logger.info(f"Overriding output format to: {args.output_format}")
        if args.output_location:
            from pydash import set_ as pydash_set

            pydash_set(
                config,
                "wikipedia_processing.stats_output_location",
                args.output_location,
            )
            logger.info(f"Overriding output location to: {args.output_location}")
        if args.wikimapper_db:
            from pydash import set_ as pydash_set

            pydash_set(config, "paths.wikimapper_db", args.wikimapper_db)
            logger.info(f"Overriding Wikimapper DB path to: {args.wikimapper_db}")

        # Initialize Wikimapper (required for aggregation)
        _initialize_wikimapper_global(config)  # Will raise if failed

        # Determine Wikipedia dump path
        wiki_dump_path = args.wiki_dump
        if not wiki_dump_path:
            data_dir = pydash_get(config, "paths.data_dir")  # Should be absolute now
            wiki_dump_filename = pydash_get(
                config, "wikipedia_processing.dump_filename"
            )
            if not data_dir or not wiki_dump_filename:
                logger.critical("Wikipedia dump path/filename not configured.")
                raise ConfigurationError("Wikipedia dump path/filename not configured.")
            wiki_dump_path = os.path.join(data_dir, wiki_dump_filename)

        if not os.path.exists(wiki_dump_path):
            logger.critical(f"Wikipedia dump file not found at '{wiki_dump_path}'")
            raise FileNotFoundError(f"Wikipedia dump file not found: {wiki_dump_path}")

        # --- Run Pipeline ---
        logger.info(f"Processing Wikipedia dump: {wiki_dump_path}")
        parser_gen = parse_wikipedia_dump(wiki_dump_path)
        qid_counts, qid_mentions = aggregate_stats(parser_gen, config)
        save_stats(qid_counts, qid_mentions, config)

        logger.info("Wikipedia statistics processing finished successfully.")

    except (
        FileNotFoundError,
        ConfigurationError,
        sqlite3.Error,
        NotImplementedError,
    ) as e:  # Add DB errors
        logger.critical(f"Configuration or Processing error: {e}", exc_info=True)
        sys.exit(1)
    except Exception as e:
        logger.critical(f"An unexpected error occurred: {e}", exc_info=True)
        sys.exit(1)
    finally:
        # Ensure Wikimapper connection is closed on exit/error
        close_wikimapper_global()


if __name__ == "__main__":
    main()
