#!/usr/bin/env python3
# utils/wikidata_helpers.py

import logging
import time
import re
from typing import Dict, Any, List, Optional, Tuple, Callable, Set
from pydash import get as pydash_get
from datetime import datetime, timezone
from collections import defaultdict

# Conditional import needed for type hint if Collection is not used elsewhere directly
try:
    from pymongo.collection import Collection
except ImportError:
    Collection = Any  # type: ignore

logger = logging.getLogger(__name__)

# Constants for Wikidata property IDs often used
P_INSTANCE_OF = "P31"
P_COORDINATE_LOCATION = "P625"
P_ADMIN_HIERARCHY = "P131"
P_COUNTRY = "P17"
P_POPULATION = "P1082"
P_AREA = "P2046"
P_INCEPTION = "P571"
P_DISSOLVED = "P576"
# Qualifiers
P_POINT_IN_TIME = "P585"
P_START_TIME = "P580"
P_END_TIME = "P582"
# Units (Example: Square Kilometer QID)
Q_SQUARE_KILOMETER = "Q712226"  # Verify this QID

# Define the expected signature for the data fetching function used by hierarchy traversal
# Takes a QID string, returns an optional dictionary (the item data) or None
GetItemDataFunc = Callable[[str], Optional[Dict[str, Any]]]


def filter_wikidata_item(item: Dict[str, Any], config: Dict[str, Any]) -> bool:
    """
    Checks if a Wikidata item meets the basic filtering criteria defined in config.

    Args:
        item: A dictionary representing a single Wikidata item from the dump.
        config: The loaded application configuration.

    Returns:
        True if the item should be processed, False otherwise.
    """
    item_id = item.get("id", "UNKNOWN_ID") # Get ID for logging

    if not isinstance(item, dict) or item.get("type") != "item":
        # logger.debug(f"Item {item_id} skipped: Not an item type.")
        return False

    # Check for English Wikipedia sitelink if required
    if pydash_get(config, "wikidata_ingestion.require_enwiki_sitelink", False):
        if not pydash_get(item, "sitelinks.enwiki.title"):
            # logger.debug(f"Item {item_id} skipped: Missing enwiki sitelink (required).")
            return False

    # Check for coordinates if required
    if pydash_get(config, "wikidata_ingestion.require_coordinates", False):
        coordinate_claims = item.get("claims", {}).get(P_COORDINATE_LOCATION, [])
        if not coordinate_claims:
            # logger.debug(f"Item {item_id} skipped: Missing P625 claim (required).")
            return False
        # Optionally add a check here to see if *any* claim yields valid coordinates
        # This adds overhead but is more robust than just checking claim presence.
        # If enabled, ensure extract_coordinates doesn't log excessively on simple checks.
        # if extract_coordinates(item) is None:
        #     logger.debug(f"Item {item_id} skipped: No *valid* P625 coordinates found (required).")
        #     return False


    # logger.debug(f"Item {item_id} passed filters.")
    return True


def extract_aliases(item: Dict[str, Any]) -> Dict[str, List[str]]:
    """
    Extracts aliases grouped by language code (e.g., 'en', 'fr').
    Merges aliases from language variants (e.g., 'en-gb', 'en-ca' into 'en').
    Explicitly maps 'simple' (Simple English) to 'en'.
    Ensures uniqueness within each language list. Uses defaultdict internally.
    """
    aliases_by_lang: Dict[str, Set[str]] = defaultdict(set)
    aliases_dict = item.get("aliases", {})
    if not isinstance(aliases_dict, dict):
        return {}

    for lang_code, alias_list in aliases_dict.items():
        if not isinstance(alias_list, list):
            continue

        # Map 'simple' wiki to 'en', standard base code for others
        if lang_code == "simple":
            target_lang = "en"
        else:
            target_lang = lang_code.split("-")[0]

        # Simple validation for target language code format (optional)
        # if not re.match(r"^[a-z]{2,3}$", target_lang):
        #     logger.warning(f"Unexpected language code format derived: '{target_lang}' from '{lang_code}'. Skipping.")
        #     continue

        for alias_entry in alias_list:
            if isinstance(alias_entry, dict):
                value = alias_entry.get("value")
                # Ensure value is string and non-empty before adding
                if isinstance(value, str) and value.strip():
                    aliases_by_lang[target_lang].add(value.strip())

    # Convert sets back to sorted lists
    result: Dict[str, List[str]] = {
        lang: sorted(list(alias_set)) for lang, alias_set in aliases_by_lang.items()
    }
    return result


# --- Coordinate Extraction Logic ---
# Regex to capture DMS components
DMS_REGEX = re.compile(
    r"""
    ^\s*                               # Optional leading whitespace
    (?P<deg>\d{1,3})                   # Degrees (1-3 digits)
    (?:\s*[°d:]\s*                     # Separator (° or d or :)
       (?P<min>\d{1,2})                # Minutes (1-2 digits)
       (?:\s*['m:]\s*                  # Optional separator (' or m or :)
          (?P<sec>[\d.]+)             # Optional Seconds (digits and dot)
          (?:\s*["s])?                 # Optional separator (" or s)
       )?                              # Seconds part is optional
    )?                                 # Minutes/Seconds part is optional
    \s*([NSEWnsew])                    # Direction (required as the last captured group)
    \s*$                               # Optional trailing whitespace
    """, re.VERBOSE | re.IGNORECASE
)

def parse_dms_string(dms_str: str) -> Optional[float]:
    """Parses a single DMS coordinate string component (e.g., 44°14'N)."""
    if not dms_str:
        return None
    match = DMS_REGEX.match(dms_str)
    if not match:
        # logger.debug(f"DMS regex did not match input: '{dms_str}'")
        return None

    parts = match.groupdict()
    direction_match = match.groups()[-1]
    if not direction_match:
        logger.warning(f"Could not extract direction component from DMS string: '{dms_str}'")
        return None
    direction = direction_match[0].upper()

    try:
        degrees = float(parts['deg'])
        minutes = float(parts.get('min') or 0)
        seconds = float(parts.get('sec') or 0)
        decimal = degrees + (minutes / 60.0) + (seconds / 3600.0)
        if direction in ('S', 'W'):
            decimal = -decimal
        return decimal
    except (ValueError, TypeError) as e:
        logger.warning(f"Could not parse DMS numeric components from '{dms_str}' (parts: {parts}): {e}")
        return None

def extract_coordinates(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Extracts coordinates from P625 claim, handling various numeric types
    (int, float, string, potentially Decimal) and DMS strings.
    Prioritizes robust float conversion. Returns a GeoJSON Point object.
    """
    item_id = item.get("id", "UNKNOWN_ID")
    coordinate_claims = item.get("claims", {}).get(P_COORDINATE_LOCATION, [])
    if not coordinate_claims:
        return None

    for i, claim in enumerate(coordinate_claims):
        if not isinstance(claim, dict): continue
        mainsnak = claim.get("mainsnak")
        if not isinstance(mainsnak, dict) or mainsnak.get("snaktype") != "value": continue
        datavalue = mainsnak.get("datavalue")
        if not isinstance(datavalue, dict) or datavalue.get("type") != "globecoordinate": continue
        value_dict = datavalue.get("value")
        if not isinstance(value_dict, dict): continue

        lat_val: Any = value_dict.get("latitude")
        lon_val: Any = value_dict.get("longitude")

        lat_num: Optional[float] = None
        lon_num: Optional[float] = None

        # --- Attempt Robust Float Conversion FIRST ---
        if lat_val is not None and lon_val is not None:
            try:
                lat_num = float(lat_val)
                lon_num = float(lon_val)
            except (ValueError, TypeError) as e:
                 logger.warning(f"Item {item_id}: Claim {i}: Failed to convert lat/lon ('{lat_val}', '{lon_val}') to float: {e}. Will check DMS.")
                 lat_num = None
                 lon_num = None

        # --- Validate and Return if numeric conversion succeeded ---
        if lat_num is not None and lon_num is not None:
            range_check_passed = -90.0 <= lat_num <= 90.0 and -180.0 <= lon_num <= 180.0
            if range_check_passed:
                # Log successful extraction at INFO level
                # logger.info(f"Item {item_id}: Claim {i}: Successfully extracted valid numeric coordinates: {[lon_num, lat_num]}")
                return {
                    "type": "Point",
                    "coordinates": [lon_num, lat_num],
                }
            else:
                # Log invalid range at WARNING level
                logger.warning(f"Item {item_id}: Claim {i}: Invalid numeric coordinate range: lat={lat_num}, lon={lon_num}. Discarding value.")
                lat_num = None
                lon_num = None

        # --- Attempt DMS String Parsing (Fallback) ---
        value_str = datavalue.get("value")
        if isinstance(value_str, str):
            parts = value_str.split(',')
            if len(parts) == 2:
                lat_dms = parse_dms_string(parts[0].strip())
                lon_dms = parse_dms_string(parts[1].strip())
                if lat_dms is not None and lon_dms is not None:
                    dms_range_check_passed = -90.0 <= lat_dms <= 90.0 and -180.0 <= lon_dms <= 180.0
                    if dms_range_check_passed:
                        # logger.info(f"Item {item_id}: Claim {i}: Successfully extracted valid DMS coordinates: {[lon_dms, lat_dms]}")
                        return {
                            "type": "Point",
                            "coordinates": [lon_dms, lat_dms],
                        }
                    else:
                        logger.warning(f"Item {item_id}: Claim {i}: Parsed DMS coordinate range invalid: lat={lat_dms}, lon={lon_dms}. Discarding value.")

        # Continue to the next claim if this one was invalid or couldn't be parsed

    # Log final failure only if loop completes without success
    logger.warning(f"Item {item_id}: No valid P625 coordinates found after checking all claims.")
    return None
# --- End Coordinate Extraction ---


# --- Batch Prefetching Implementation ---
# Module-level cache, managed per batch by ingest script
_batch_item_cache: Dict[str, Optional[Dict[str, Any]]] = {}

def _get_item_data_batch_fetcher(
    qids: List[str], collection: "Collection"
) -> None:
    """ Fetches data for multiple QIDs needed for hierarchy and caches it. """
    global _batch_item_cache
    qids_to_fetch = sorted([qid for qid in qids if qid not in _batch_item_cache]) # Sort for consistent logging
    if not qids_to_fetch:
        # logger.debug("Batch Prefetcher: No new QIDs to fetch for this batch.")
        return

    # Log the request at INFO level to see which QIDs are being fetched per batch
    # logger.info(f"Batch Prefetcher: Requesting data for {len(qids_to_fetch)} QIDs: {qids_to_fetch[:20]}...") # Log first few QIDs

    docs_found_count = 0 # Counter for fetched docs
    try:
        projection = {
            "_id": 1,
            "english_label": 1,
            "claims.P131.mainsnak.datavalue.value.id": 1
        }
        # Time the query
        query_start_time = time.monotonic()
        cursor = collection.find({"_id": {"$in": qids_to_fetch}}, projection)
        found_qids = set()
        # Consume cursor and populate cache
        for doc in cursor:
            docs_found_count += 1 # Increment counter
            doc_id = doc["_id"]
            _batch_item_cache[doc_id] = doc
            found_qids.add(doc_id)
        query_duration = time.monotonic() - query_start_time

        # Mark QIDs that were requested but not found in DB as explicitly None in cache
        missing_qids = set(qids_to_fetch) - found_qids
        for qid in missing_qids:
            _batch_item_cache[qid] = None

        # Log Results at INFO level
        # logger.info(
        #     f"Batch Prefetcher: Query duration={query_duration:.4f}s. "
        #     f"Fetched {docs_found_count} docs. Marked {len(missing_qids)} QIDs as missing in cache."
        # )

    except Exception as e:
        logger.error(f"Batch Prefetcher: Error during MongoDB find operation for QIDs {qids_to_fetch[:20]}: {e}", exc_info=True)
        # Ensure missing QIDs are marked as None even on error to prevent retries within batch
        for qid in qids_to_fetch:
            if qid not in _batch_item_cache:
                 _batch_item_cache[qid] = None
        logger.error(f"Batch Prefetcher: Marked {len(qids_to_fetch)} requested QIDs as missing in cache due to error.")


def _get_item_data_from_cache(qid: str) -> Optional[Dict[str, Any]]:
    """ Retrieves item data from the batch cache. """
    global _batch_item_cache
    # Returns the cached dict or None if QID wasn't found/fetched
    cached_value = _batch_item_cache.get(qid)
    # logger.debug(f"Cache Lookup: QID='{qid}', Found: {'Yes' if qid in _batch_item_cache else 'No'}, Type: {type(cached_value)}")
    return cached_value

# --- End Batch Prefetching ---


# --- Admin Hierarchy Extraction (Revised Version) ---
def extract_admin_hierarchy(
    item: Dict[str, Any], get_item_data_func: GetItemDataFunc, max_depth: int = 10
) -> List[Dict[str, Any]]:
    """ Extracts the administrative hierarchy (P131) by traversing upwards.
        Ensures label field is always a non-empty string (fallback to QID).
    """
    item_id = item.get("id")
    hierarchy: List[Dict[str, Any]] = []
    initial_p131_claims = item.get("claims", {}).get(P_ADMIN_HIERARCHY, [])
    current_qids: List[str] = sorted(list(set(
        pydash_get(claim, "mainsnak.datavalue.value.id")
        for claim in initial_p131_claims
        if pydash_get(claim, "mainsnak.snaktype") == "value" and pydash_get(claim, "mainsnak.datavalue.value.id")
    )))
    visited_qids: Set[str] = {item_id} if item_id else set()
    level = 0

    while current_qids and level < max_depth:
        next_level_qids_set: Set[str] = set()
        qids_processed_this_level: List[str] = []

        for qid in current_qids:
            if qid in visited_qids:
                continue
            visited_qids.add(qid)
            qids_processed_this_level.append(qid)

            # === Core Data Retrieval ===
            parent_item_data = get_item_data_func(qid)
            # ===========================

            # Check if data was successfully retrieved
            if parent_item_data:
                # --- Robust Label Extraction ---
                label_to_append: str # Type hint for clarity
                fetched_label = pydash_get(parent_item_data, "english_label")

                # Check if fetched_label is a valid, non-empty string after stripping
                if isinstance(fetched_label, str) and fetched_label.strip():
                    label_to_append = fetched_label.strip()
                else:
                    # Fallback to QID if label is None, not a string, or empty/whitespace
                    label_to_append = qid # qid is guaranteed to be a non-empty string here
                    # Only log warning if label was genuinely missing or None from fetch
                    if fetched_label is None:
                         logger.warning(
                             f"Using QID '{qid}' as fallback label for hierarchy level {level} in item {item_id} (english_label missing in fetched data)"
                         )
                # --- End Label Extraction ---

                hierarchy.append({"qid": qid, "level": level, "label": label_to_append}) # label_to_append is now guaranteed string

                # Find next level QIDs (grandparents)
                grandparent_claims = parent_item_data.get("claims", {}).get(P_ADMIN_HIERARCHY, [])
                if isinstance(grandparent_claims, list):
                    for claim in grandparent_claims:
                        next_qid = pydash_get(claim, "mainsnak.datavalue.value.id")
                        if pydash_get(claim, "mainsnak.snaktype") == "value" and next_qid and next_qid not in visited_qids:
                            next_level_qids_set.add(next_qid)
            else:
                # Log warning IF data retrieval failed (parent_item_data is None)
                logger.info(f"Could not retrieve data for P131 linked item {qid} (level {level}) referenced by {item_id}. Hierarchy might be incomplete.")

        if not qids_processed_this_level:
            break

        current_qids = sorted(list(next_level_qids_set))
        level += 1

    if level == max_depth and current_qids:
        logger.warning(f"P131 hierarchy traversal reached max depth ({max_depth}) for item {item_id}.")

    return hierarchy
# --- End Admin Hierarchy ---


# --- Date/Time Parsing Helpers ---
def _parse_wikidata_time(time_str: Optional[str]) -> Optional[datetime]:
    """Attempts to parse Wikidata time strings into datetime objects."""
    if not time_str or not isinstance(time_str, str): return None
    match = re.match(r"\+?(-?\d+)-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z", time_str)
    if match:
        try:
            year, month, day, hour, minute, second = map(int, match.groups())
            year_to_use = year
            month_to_use = month if month != 0 else 1
            day_to_use = day if day != 0 else 1
            if abs(year_to_use) > 9999:
                 # logger.debug(f"Year {year_to_use} outside BSON range, returning None.")
                 return None
            return datetime(year_to_use, month_to_use, day_to_use, hour, minute, second)
        except ValueError as e:
            logger.warning(f"Could not parse date components from '{time_str}': {e}")
            return None
    year_match = re.match(r"\+?(-?\d{4,})-00-00T00:00:00Z", time_str)
    if year_match:
        try:
            year = int(year_match.group(1))
            if abs(year) <= 9999: return datetime(year, 1, 1)
            else:
                # logger.debug(f"Year {year} outside BSON range.")
                return None
        except ValueError:
            logger.warning(f"Could not parse year from '{time_str}'.")
            return None
    return None

def _extract_year_from_time_str(time_str: Optional[str]) -> Optional[int]:
    """Extracts year robustly from Wikidata time string, handling large years."""
    if not time_str or not isinstance(time_str, str): return None
    match = re.match(r"\+?(-?\d{4,})-.*", time_str)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return None
    return None
# --- End Date/Time Helpers ---


# --- Simple Claim Extractor ---
def extract_simple_claim_values(item: Dict[str, Any], pid: str) -> List[str]:
    """ Extracts the string value (usually a QID) from simple claims. """
    values: Set[str] = set()
    claims = item.get("claims", {}).get(pid, [])
    if not isinstance(claims, list):
        return []

    for claim in claims:
        if pydash_get(claim, "mainsnak.snaktype") == "value":
            entity_id = pydash_get(claim, "mainsnak.datavalue.value.id")
            if isinstance(entity_id, str) and entity_id:
                values.add(entity_id)

    return sorted(list(values))
# --- End Simple Claim Extractor ---


# --- Complex Property Extractor ---
def extract_complex_property(
    item: Dict[str, Any], pid: str, config: Dict[str, Any]
) -> Optional[Any]:
    """ Extracts data for configured complex properties requiring qualifier handling. """
    item_id = item.get("id")
    claims = item.get("claims", {}).get(pid, [])
    if not isinstance(claims, list):
         # logger.warning(f"Invalid claims data format for PID {pid} in item {item_id}. Expected list.")
         return None

    extracted_data = []

    try:
        if pid == P_COUNTRY:
            for claim in claims:
                if pydash_get(claim, "mainsnak.snaktype") == "value":
                    country_qid = pydash_get(claim, "mainsnak.datavalue.value.id")
                    if country_qid and isinstance(country_qid, str):
                        entry: Dict[str, Any] = {"country_qid": country_qid, "start_year": None, "end_year": None}
                        qualifiers = claim.get("qualifiers", {})
                        if isinstance(qualifiers, dict):
                            start_time_qual = qualifiers.get(P_START_TIME, [])
                            end_time_qual = qualifiers.get(P_END_TIME, [])
                            if start_time_qual and isinstance(start_time_qual, list) and pydash_get(start_time_qual[0], "snaktype") == "value":
                                entry["start_year"] = _extract_year_from_time_str(pydash_get(start_time_qual[0], "datavalue.value.time"))
                            if end_time_qual and isinstance(end_time_qual, list) and pydash_get(end_time_qual[0], "snaktype") == "value":
                                entry["end_year"] = _extract_year_from_time_str(pydash_get(end_time_qual[0], "datavalue.value.time"))
                        extracted_data.append(entry)
            return extracted_data if extracted_data else None

        elif pid == P_POPULATION:
            for claim in claims:
                if pydash_get(claim, "mainsnak.snaktype") == "value":
                    amount_str = pydash_get(claim, "mainsnak.datavalue.value.amount")
                    if amount_str and isinstance(amount_str, str):
                        try:
                            population_val = int(amount_str.lstrip("+")) # BSON long handled by pymongo if needed
                            entry: Dict[str, Any] = {"value": population_val, "point_in_time": None}
                        except (ValueError, TypeError) as e:
                            logger.warning(f"Could not parse population amount '{amount_str}' for {item_id}: {e}")
                            continue

                        qualifiers = claim.get("qualifiers", {})
                        if isinstance(qualifiers, dict):
                            time_qual = qualifiers.get(P_POINT_IN_TIME, [])
                            if time_qual and isinstance(time_qual, list) and pydash_get(time_qual[0], "snaktype") == "value":
                                time_str = pydash_get(time_qual[0], "datavalue.value.time")
                                dt_obj = _parse_wikidata_time(time_str)
                                entry["point_in_time"] = dt_obj if dt_obj else time_str
                        extracted_data.append(entry)
            return extracted_data if extracted_data else None

        elif pid == P_AREA:
            best_area_sqkm: Optional[float] = None
            for claim in claims:
                if pydash_get(claim, "mainsnak.snaktype") == "value":
                    amount_str = pydash_get(claim, "mainsnak.datavalue.value.amount")
                    unit_url = pydash_get(claim, "mainsnak.datavalue.value.unit", "")
                    if amount_str and isinstance(amount_str, str) and unit_url and isinstance(unit_url, str):
                        unit_qid = unit_url.split("/")[-1]
                        try:
                            area_val = float(amount_str.lstrip("+"))
                        except (ValueError, TypeError) as e:
                            logger.warning(f"Could not parse area amount '{amount_str}' for {item_id}: {e}")
                            continue

                        if unit_qid == Q_SQUARE_KILOMETER:
                            best_area_sqkm = area_val
                        else:
                            pass # logger.debug(f"Area unit {unit_qid} not handled for {item_id}.")
            return best_area_sqkm

        elif pid == P_INCEPTION or pid == P_DISSOLVED:
            earliest_year: Optional[int] = None
            for claim in claims:
                year_for_this_claim: Optional[int] = None
                if pydash_get(claim, "mainsnak.snaktype") == "value":
                    time_str = pydash_get(claim, "mainsnak.datavalue.value.time")
                    year_for_this_claim = _extract_year_from_time_str(time_str)

                if year_for_this_claim is not None:
                    # Check BSON int range before comparing (years need 32-bit BSON int)
                    # Technically, BSON int is approx +/- 2.147 billion.
                    # Let's check if year_for_this_claim fits. Large negative years are common.
                    # MongoDB allows up to 64-bit longs, schema updated to allow int/long/null
                    # So, no range check needed here, let Mongo handle storage type.
                    if earliest_year is None or year_for_this_claim < earliest_year:
                        earliest_year = year_for_this_claim
            return earliest_year

        else:
            # logger.warning(f"Complex property extractor not implemented for PID: {pid}")
            return None

    except Exception as e:
        logger.error(f"Unexpected error extracting complex PID {pid} for item {item_id}: {e}", exc_info=True)
        return None
# --- End Complex Property Extractor ---


# --- Main Data Extraction Orchestrator ---
def extract_entity_data(
    item: Dict[str, Any], config: Dict[str, Any], get_item_data_func: GetItemDataFunc
) -> Dict[str, Any]:
    """
    Extracts relevant information from a filtered Wikidata item dictionary
    and transforms it into the target MongoDB schema structure.
    """
    item_id = item["id"]
    target_doc: Dict[str, Any] = {
        "_id": item_id,
        "wikidata_id": item_id,
        "english_label": None,
        "english_description": None,
        "aliases": {},
        "instance_of": [],
        "coordinates": None,
        "country_membership": None,
        "admin_hierarchy": [],
        "population": None,
        "area_sqkm": None,
        # Removed 'dates': {} initialization - will add only if populated
        # Removed 'sitelinks': {} initialization - will add only if populated
        # Fields added by later steps initialized
        "corrected_class": None,
        "wiki_links": None,
        "ontology_flag_resolved": False,
        "embedding_vector": None,
        "last_updated": None # Will be set during ingest
    }

    # Basic Info
    target_doc["english_label"] = pydash_get(item, "labels.en.value")
    target_doc["english_description"] = pydash_get(item, "descriptions.en.value")
    target_doc["aliases"] = extract_aliases(item)

    # Configured Properties
    properties_to_extract = pydash_get(config, "wikidata_ingestion.properties_to_extract", [])
    dates_data: Dict[str, Optional[int]] = {"inception_year": None, "dissolved_year": None}

    for pid in properties_to_extract:
        try:
            if pid == P_INSTANCE_OF:
                 target_doc["instance_of"] = extract_simple_claim_values(item, pid)
            elif pid == P_COORDINATE_LOCATION:
                 target_doc["coordinates"] = extract_coordinates(item)
            elif pid == P_ADMIN_HIERARCHY:
                 target_doc["admin_hierarchy"] = extract_admin_hierarchy(item, get_item_data_func)
            elif pid == P_COUNTRY:
                 target_doc["country_membership"] = extract_complex_property(item, pid, config)
            elif pid == P_POPULATION:
                 target_doc["population"] = extract_complex_property(item, pid, config)
            elif pid == P_AREA:
                 target_doc["area_sqkm"] = extract_complex_property(item, pid, config)
            elif pid == P_INCEPTION:
                 dates_data["inception_year"] = extract_complex_property(item, pid, config)
            elif pid == P_DISSOLVED:
                 dates_data["dissolved_year"] = extract_complex_property(item, pid, config)
            else:
                simple_values = extract_simple_claim_values(item, pid)
                if simple_values:
                    # logger.debug(f"Extracted simple values for unhandled PID {pid} in {item_id}: {simple_values}")
                    target_doc.setdefault("other_claims", {})[pid] = simple_values
        except Exception as prop_ex:
             logger.error(f"Error extracting property {pid} for item {item_id}: {prop_ex}", exc_info=True)

    # Add dates object ONLY if it contains actual year data
    if dates_data.get("inception_year") is not None or dates_data.get("dissolved_year") is not None:
        target_doc["dates"] = dates_data

    # Extract sitelink title (normalized)
    enwiki_title = pydash_get(item, "sitelinks.enwiki.title")
    if enwiki_title and isinstance(enwiki_title, str):
        try:
            from utils.helpers import normalize_wikipedia_title
            normalized_title = normalize_wikipedia_title(enwiki_title)
            if normalized_title:
                target_doc.setdefault("sitelinks", {}).setdefault("enwiki", {})["normalized_title"] = normalized_title
        except ImportError:
            logger.error("Could not import normalize_wikipedia_title from utils.helpers.")
        except Exception as norm_err:
            logger.error(f"Error normalizing title '{enwiki_title}' for {item_id}: {norm_err}")

    # Final Cleanup: Remove top-level fields that are still None
    # This prevents inserting explicit nulls unless the schema specifically allows/requires null
    # Schema fields updated previously allow nulls for label/desc/dates/etc.
    # Only remove if the value is None AND the field is purely optional (not required and null not meaningful)
    # Let's keep fields that were explicitly processed even if result is None for now,
    # as the schema allows null for most optional fields.
    # Example: If we wanted to remove fields strictly if None:
    # fields_to_remove_if_none = ["coordinates", "country_membership", "population", "area_sqkm", ...]
    # for field in fields_to_remove_if_none:
    #     if target_doc.get(field) is None:
    #         target_doc.pop(field, None)

    # Clean up empty containers that might have been initialized but not populated
    if not target_doc.get("aliases"): target_doc.pop("aliases", None)
    if not target_doc.get("instance_of"): target_doc.pop("instance_of", None)
    if not target_doc.get("admin_hierarchy"): target_doc.pop("admin_hierarchy", None)
    if not target_doc.get("sitelinks"): target_doc.pop("sitelinks", None)
    if not target_doc.get("dates"): target_doc.pop("dates", None)


    return target_doc
# --- End Orchestrator ---
