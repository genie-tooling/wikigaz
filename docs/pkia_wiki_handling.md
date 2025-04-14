# Wiki Data Handling Strategy (Implementation Details)

**Version:** 1.0
**Date:** 2025-04-14
**Author:** Python Knowledge Integration Architect (PKIA)
**Status:** Implemented

## 1. Introduction

This document details the specific low-level implementation choices and assumptions made when parsing, interpreting, and linking data from Wikidata and Wikipedia within the `wiki2gaz-modern` Python code. This complements the higher-level Source Data Deep Dive document.

## 2. Wikidata JSON Parsing (`ingest_wikidata_mongo.py` & `utils/wikidata_helpers.py`)

*   **Parsing Library:** `ijson` is used via `ijson.items(f, 'item')` on a `bz2.open()` stream to iterate through top-level entities without loading the entire multi-terabyte dump into memory.
*   **Entity Filtering (`filter_wikidata_item`):**
    *   Checks `type == 'item'`.
    *   Conditionally checks for presence of `sitelinks.enwiki.title` based on `config.wikidata_ingestion.require_enwiki_sitelink`.
    *   Conditionally checks for presence and basic validity (lat/lon exist as numbers) of `claims.P625` based on `config.wikidata_ingestion.require_coordinates`.
*   **Data Access:** Uses `pydash.get(item, 'path.to.key', default=None)` or `dict.get('key', default=None)` extensively to safely access potentially missing keys/nested structures in the raw Wikidata JSON.
*   **Label/Description/Alias Extraction (`extract_aliases`, `extract_entity_data`):** Focuses on English (`en`) values. `extract_aliases` merges language variants (e.g., `en-gb` -> `en`) and deduplicates aliases.
*   **Coordinate Extraction (`extract_coordinates`):** Extracts the first valid latitude/longitude pair from `claims.P625`. Ignores precision, globe. Returns GeoJSON Point format `{"type": "Point", "coordinates": [lon, lat]}`.
*   **Simple Claim Extraction (`extract_simple_claim_values`):** Extracts target QIDs from specified property claims (e.g., P31) where `mainsnak.snaktype == 'value'` and the value is an entity ID. Returns a sorted list of unique QIDs.
*   **Complex Claim Extraction (`extract_complex_property`):** Handles specific PIDs with known qualifier patterns:
    *   `P17` (Country): Extracts target QID, checks `P580` (start), `P582` (end) qualifiers, parses years using `_extract_year_from_time_str`.
    *   `P1082` (Population): Extracts `amount`, checks `P585` (point in time), parses date using `_parse_wikidata_time` (stores `datetime` or raw string).
    *   `P2046` (Area): Extracts `amount`, checks `unit`. Only processes if unit is `Q712226` (sqkm). No other unit conversions implemented.
    *   `P571`/`P576` (Inception/Dissolved): Extracts earliest year using `_extract_year_from_time_str`.
*   **Date/Time Parsing (`_parse_wikidata_time`, `_extract_year_from_time_str`):** Handles standard Wikidata ISO-like format (`+YYYY-MM-DDTHH:MM:SSZ`). Critically, it interprets `+YYYY-00-00...` as year-only precision (stored as `YYYY-01-01` datetime). Handles large years (>9999) by returning None or just the year integer, as BSON Date has limitations.
*   **Hierarchy Traversal (P131) (`extract_admin_hierarchy`, `_get_item_data_batch_fetcher`, `_get_item_data_from_cache`):**
    *   Iterative upward traversal using `claims.P131` links.
    *   Relies on batch prefetching: `ingest_wikidata_mongo.py` calls `_get_item_data_batch_fetcher` *once per batch* to fetch minimal data (`_id`, `english_label`, `claims.P131`) for all needed parent QIDs from MongoDB.
    *   `extract_admin_hierarchy` uses `_get_item_data_from_cache` for lookups during traversal, avoiding direct DB calls.
    *   Includes cycle detection (`visited_qids`) and `max_depth` limit.
    *   Stores result as ordered list `[{qid, level, label}, ...]`.

## 3. Wikipedia XML Parsing (`process_wiki_stats.py`)

*   **Parsing Library:** `lxml.etree.iterparse` (preferred) is used on a `bz2.open(..., rt=True)` stream.
*   **Memory Management:** Crucially uses `elem.clear()` and `parent.remove(elem)` after processing each `<page>` element to prevent the parser from building a full tree in memory.
*   **Page Filtering:** Currently yields all pages from the parser but only processes the `<text>` of non-redirect pages (`<redirect>` tag absent) within `aggregate_stats`.
*   **Link Extraction:**
    *   Uses `WIKI_LINK_REGEX = re.compile(r'\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]+))?\]\]')` on the raw `<text>` content.
    *   Extracts `Target` (group 1) and `Anchor Text` (group 2, defaults to Target if absent).
    *   **Limitation:** Does **not** parse wikitext templates (`{{...}}`), parser functions, or tables. Links inside these complex structures will be missed. This is a performance trade-off.
*   **Normalization:** The extracted `Target` string is **always** normalized using `utils.helpers.normalize_wikipedia_title` before further processing or lookup.
*   **QID Mapping:** The normalized `Target` is looked up using `utils.mapping.WikimapperLookup.get_qid()`.
*   **Aggregation:** Uses in-memory `defaultdict(int)` for QID->Count and `defaultdict(Counter)` for QID->Mention->Count. **Memory usage must be monitored by PI.** If excessive, this step requires redesign (e.g., external merge sort, DB aggregation).
*   **Output:** Saves aggregated stats to either JSON Lines (`.jsonl`) files or an SQLite database (`.db`) based on configuration.

## 4. Canonical Normalization (`utils/helpers.py`)

*   **Function:** `normalize_wikipedia_title` is the single source of truth.
*   **Process:**
    1.  Handles `None` / non-string inputs.
    2.  `_` -> ` ` (Space).
    3.  Repeatedly URL-decode (`urllib.parse.unquote`).
    4.  Remove `#fragment`.
    5.  Unicode NFC normalization (`unicodedata.normalize`).
    6.  Collapse multiple whitespace (`re.sub(r'\s+', ' ', ...)`).
    7.  Strip leading/trailing whitespace.
    8.  Capitalize **first letter only** (`s[0].upper() + s[1:]`).
*   **Application:** Used on Wikidata `sitelinks.enwiki.title` before storing/lookup, and on Wikipedia link targets before Wikimapper lookup.

## 5. QID Mapping (`utils/mapping.py`)

*   **Mechanism:** `WikimapperLookup` class queries an SQLite database built by the external `wikimapper create` command.
*   **Input:** Requires a canonically normalized Wikipedia title string.
*   **Query:** Performs `SELECT qid FROM mapping WHERE title = ?`. Assumes the table schema created by `wikimapper`.
*   **Output:** Wikidata QID string (e.g., "Q60") or `None`.
*   **Dependency:** Relies on the existence and correctness of the pre-built Wikimapper SQLite database (`index_*.db`). The database date should align with the Wikipedia XML dump date.

## 6. Specific Issue Handling

*   **Ontology Flaw (P131):** Handled post-ingestion by `scripts/fix_ontology_mongo.py`. Identifies specific patterns (sub-part type -> major admin type) using configured QID lists and cached P31 lookups, potentially modifying the stored `admin_hierarchy` array. See `docs/ontology_flaw.md`.
*   **Class Assignment:** Handled post-ingestion by `scripts/apply_semantic_corrections.py`. Assigns `corrected_class` based on `instance_of` values and `config/class_mapping_rules.yaml`. See `docs/semantic_corrections.md`.
*   **Data Errors:** Relies on safe access (`.get()`, `pydash.get`), type checks (`isinstance`), and `try...except` blocks around parsing (e.g., dates, numbers) to handle missing or malformed data gracefully, logging warnings instead of crashing where possible.

This detailed handling strategy aims to process the complex and evolving Wiki data sources robustly and efficiently, acknowledging necessary assumptions and limitations.
