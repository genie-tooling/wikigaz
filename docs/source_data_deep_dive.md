# Source Data Deep Dive & Assumptions

**Version:** 1.0
**Date:** 2025-04-14
**Author:** Knowledge Graph Architect & Modernization Strategist

## 1. Introduction

This document provides a deeper look into the specific source data dumps used by the `wiki2gaz-modern` pipeline (Wikidata JSON, Wikipedia XML, Wikimapper SQL prerequisites) and outlines the key assumptions made during their processing. Understanding these details is crucial for debugging, assessing data quality, interpreting the final output, and adapting the pipeline to future data changes.

## 2. Target Data Dumps

It is critical to track the specific versions of the Wikimedia dumps used for any given pipeline run, as content and structure can change over time. The pipeline configuration (`config.yaml`) defines default filenames (e.g., `latest-all.json.bz2`, `enwiki-latest-pages-articles-multistream.xml.bz2`), but for reproducible runs, it's recommended to use date-stamped versions and record them.

*   **Wikidata JSON Dump:** e.g., `wikidata-YYYYMMDD-all.json.bz2` (from `https://dumps.wikimedia.org/wikidatawiki/entities/`)
*   **Wikipedia XML Dump:** e.g., `enwiki-YYYYMMDD-pages-articles-multistream.xml.bz2` (from `https://dumps.wikimedia.org/enwiki/`)
*   **Wikipedia SQL Dumps (for Wikimapper):** e.g., `enwiki-YYYYMMDD-page.sql.gz`, `-redirect.sql.gz`, `-page_props.sql.gz` (from the same Wikipedia dump date directory). **Crucially, these SQL dumps *must* correspond to the *same date* as the Wikipedia XML dump** for Wikimapper to function correctly.

**Assumption:** All input dumps are assumed to be UTF-8 encoded. Files are processed in their compressed state (`.bz2`, `.gz`) using appropriate Python libraries.

## 3. Wikidata JSON Dump (`latest-all.json.bz2`)

*   **Format:** A single JSON file (typically bz2 compressed) containing a large array `[`...`,`...`]`, where each element is a JSON object representing a Wikidata entity (Item, Property, Lexeme, etc.). The file starts with `[` and ends with `]`, with items separated by `,`.
*   **Parsing:** Processed using `bz2` and the `ijson` library (`ijson.items(f, 'item')`) for memory-efficient streaming. Each yielded dictionary represents one entity.
*   **Key Structures Used:**
    *   `id`: The QID or PID (e.g., "Q60", "P31"). Used as `_id` in MongoDB.
    *   `type`: "item", "property", etc. We filter for `"item"` (`filter_wikidata_item`).
    *   `labels`: Object mapping language codes (e.g., "en") to label objects (`{"language": "en", "value": "..."}`). We extract `labels.en.value`.
    *   `descriptions`: Similar structure to `labels`. We extract `descriptions.en.value`.
    *   `aliases`: Object mapping language codes to lists of alias objects (`[{"language": "en", "value": "..."}, ...]`). We extract and normalize these (`extract_aliases`).
    *   `claims`: Object mapping Property IDs (e.g., "P31") to lists of claim statements. This is the core data source.
        *   **Claim Statement:** Each claim object has:
            *   `mainsnak`: Contains the primary assertion.
                *   `snaktype`: "value" (standard assertion), "novalue", "somevalue". We primarily process `"value"` snaks.
                *   `property`: The PID of the assertion.
                *   `datavalue`: Contains the actual value.
                    *   `type`: "wikibase-entityid", "string", "time", "quantity", "globecoordinate", "monolingualtext", etc. Dictates the structure of `value`.
                    *   `value`: The actual data, format depends on `type`. Examples:
                        *   `{"entity-type": "item", "numeric-id": 60, "id": "Q60"}` (for entityid)
                        *   `"+1776-07-04T00:00:00Z"` (for time)
                        *   `{"amount": "+8500000", "unit": "1"}` or `{"amount": "+777", "unit": "http://www.wikidata.org/entity/Q712226"}` (for quantity)
                        *   `{"latitude": 40.7, "longitude": -74.0, "precision": 0.1, "globe": "http://www.wikidata.org/entity/Q2"}` (for globecoordinate)
            *   `qualifiers`: Optional object mapping PIDs to lists of qualifier snaks, providing context/details about the mainsnak. Crucial for properties like P17 (country), P1082 (population), P2046 (area).
            *   `qualifiers-order`: Optional list defining display order for qualifiers. (Not currently used).
            *   `rank`: "preferred", "normal", "deprecated". (Currently, we extract data primarily from the mainsnak regardless of rank, but might prioritize "preferred" for coordinates or specific single-value extractions if needed).
            *   `references`: Optional list of references supporting the claim. (Not currently extracted).
    *   `sitelinks`: Object mapping site IDs (e.g., "enwiki", "frwiki") to sitelink objects (`{"site": "enwiki", "title": "New York City", "badges": [...]}`). Used to link Wikidata items to Wikipedia pages and as a filter (`require_enwiki_sitelink`).

*   **Assumptions & Handling:**
    *   We primarily process entities of `type: "item"`.
    *   We focus on English labels, descriptions, and aliases (`en`).
    *   We primarily extract data from `mainsnak` where `snaktype: "value"`.
    *   Specific helpers (`extract_complex_property`) handle known qualifiers (P580, P582, P585, units for P2046). Other qualifiers are ignored.
    *   Wikidata time values are parsed into years (`_extract_year_from_time_str`) or datetime objects (`_parse_wikidata_time`) where possible; BSON Date is used in Mongo for `point_in_time` if parsable, otherwise the raw string is stored. Large years (>9999) might not be stored correctly by BSON Date.
    *   Quantities are parsed assuming standard numeric formats (potentially with leading '+'). Unit conversion is only implemented for Area (sqkm).
    *   Coordinate precision is ignored; only lat/lon values are stored in GeoJSON.
    *   Hierarchy traversal (P131) uses batch prefetching based on QIDs found *within the same target MongoDB collection*. If P131 points to an entity not ingested (or missing key fields like label/P131), the hierarchy might be incomplete. It also assumes P131 represents the *primary* administrative parent for traversal.

## 4. Wikipedia XML Dump (`pages-articles-multistream.xml.bz2`)

*   **Format:** A large XML file (bz2 compressed) containing nested elements defined by the MediaWiki export schema (`http://www.mediawiki.org/xml/export-0.10/`). Uses `<mediawiki>` as root, containing many `<page>` elements.
*   **Parsing:** Processed using `bz2` and `lxml.etree.iterparse` (preferred for performance) or `xml.etree.ElementTree.iterparse` for streaming. Event-driven parsing (`event='end'`, `tag='{...}page'`) is used. **Crucially, `elem.clear()` and parent manipulation are used after processing each `<page>` element to prevent memory leaks.**
*   **Key Structures Used (`scripts/process_wiki_stats.py`):**
    *   `<page>`: Contains information about a single page.
    *   `<title>`: The page title (human-readable, uses spaces).
    *   `<ns>`: Namespace code (e.g., 0 for main articles, 4 for Wikipedia project space, 10 for templates, 14 for categories). **Assumption:** We primarily process pages in namespace 0 (main articles) by default, although the parser yields all pages. The `aggregate_stats` function implicitly only processes links *from* these pages.
    *   `<id>`: The `page_id`. (Currently logged but not stored).
    *   `<redirect title="..." />`: Indicates the page is a redirect to the specified title. **Assumption:** We currently skip processing the text of redirect pages in `aggregate_stats`. Redirect mapping itself is handled by Wikimapper.
    *   `<revision>`: Contains versions of the page content. We process the text from the *latest* revision included in the dump.
    *   `<text xml:space="preserve">...</text>`: Contains the page content in **MediaWiki markup**.

*   **MediaWiki Markup & Link Extraction:**
    *   The `<text>` content is complex markup including wikitext, templates (`{{...}}`), tables (`{|...|}`), parser functions (`{{#...}}`), HTML tags, and internal links (`[[...]]`).
    *   **Assumption:** We use a regular expression (`WIKI_LINK_REGEX`) to extract simple internal wikilinks of the form `[[Target]]` or `[[Target|Anchor Text]]`. This regex aims to capture the link target *before* any `#` fragment or `|` separator.
    *   **Limitation:** This approach **does not parse or expand templates**. Links hidden inside complex templates will likely be missed. It also doesn't interpret parser functions or complex conditional logic. This is a significant simplification for performance, focusing on direct links.
    *   **Normalization:** The extracted `Target` is normalized using `utils.helpers.normalize_wikipedia_title` before being looked up in Wikimapper. The `Anchor Text` is used directly as the 'mention' key.

*   **Aggregation (`scripts/process_wiki_stats.py`):**
    *   Builds two in-memory structures:
        *   `qid_inlink_counts`: `defaultdict(int)` mapping `Target QID -> Count`.
        *   `qid_mention_counts`: `defaultdict(Counter)` mapping `Target QID -> Counter(Anchor Text -> Count)`.
    *   **Assumption:** This aggregation fits within available RAM. **PI must verify this.** If not, alternative strategies (external sort/merge, DB aggregation) are needed.
    *   Uses the global `WikimapperLookup` instance to map normalized link targets to QIDs. Links targeting titles not found in the Wikimapper DB are counted but logged and discarded from the final stats.

## 5. Wikimapper Prerequisite SQL Dumps

*   **Format:** Standard MySQL dump files (`.sql.gz`) containing `INSERT` statements for specific database tables from a Wikipedia database snapshot.
*   **Key Files Used:**
    *   `page.sql.gz`: Contains mapping between `page_title`, `page_id`, and `page_namespace`. Also includes `page_is_redirect` flag.
    *   `redirect.sql.gz`: Explicitly lists source/target pairs for redirects.
    *   `page_props.sql.gz`: Contains page properties, notably the `wikibase_item` property linking a Wikipedia page `page_id` to its corresponding Wikidata QID.
*   **Processing:** These are **not** directly parsed by our Python scripts. They are the required input for the external `wikimapper create` command (`scripts/build_wikimapper_index.sh`). Wikimapper processes these SQL files to build its efficient SQLite lookup database.
*   **Assumption:** The `wikimapper` tool correctly interprets these SQL dumps to build the title -> QID mapping. The version/date of these dumps **must match** the XML dump for consistency.

## 6. Interlinking Strategy Summary

The pipeline connects these data sources as follows:

1.  **Wikidata -> Wikipedia Page:** Wikidata `item.sitelinks.enwiki.title` provides the initial link. This title is normalized using `utils.helpers.normalize_wikipedia_title`.
2.  **Wikipedia Link -> Wikidata Item:**
    *   A link `[[Target|Anchor]]` is found in Wikipedia XML text.
    *   The `Target` is normalized using `utils.helpers.normalize_wikipedia_title`.
    *   The normalized target title is looked up in the Wikimapper SQLite DB (via `utils.mapping.WikimapperLookup`) to get the corresponding Wikidata QID.
    *   Statistics (inlink count, mention counts using `Anchor`) are aggregated against this target QID.
3.  **Enrichment:** The aggregated statistics (from step 2) are joined back to the Wikidata documents stored in MongoDB using the QID as the key (`scripts/enrich_mongo_with_wiki.py`).

**Core Assumption:** Wikimapper provides an accurate and sufficiently complete mapping between normalized Wikipedia titles and Wikidata QIDs for the target dump date. Inaccuracies or missing entries in Wikimapper will lead to unmapped links and potentially incomplete statistics. Canonical title normalization is essential for this lookup to succeed reliably.
