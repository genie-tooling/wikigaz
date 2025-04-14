import logging
from typing import Dict, Any, List, Optional, Tuple, Callable, Set
from pydash import get as pydash_get
import time  # For timing hierarchy traversal
from datetime import datetime  # For parsing dates
import re  # <<< ADDED IMPORT
from collections import defaultdict  # <<< ADDED IMPORT

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
    if not isinstance(item, dict) or item.get("type") != "item":
        return False  # Skip non-items or malformed entries

    item_id = item.get("id")  # For logging

    # Check for English Wikipedia sitelink if required
    if pydash_get(config, "wikidata_ingestion.require_enwiki_sitelink", False):
        # Use pydash_get for safe nested access
        if not pydash_get(item, "sitelinks.enwiki.title"):
            logger.debug(f"Item {item_id} skipped: Missing enwiki sitelink.")
            return False

    # Check for coordinates if required
    if pydash_get(config, "wikidata_ingestion.require_coordinates", False):
        coordinate_claims = item.get("claims", {}).get(P_COORDINATE_LOCATION, [])
        has_valid_coords = False
        for claim in coordinate_claims:
            # Check rank? Often preferred rank is best, but any valid coord might suffice
            # claim_rank = claim.get('rank', 'normal')
            # Check snaktype and presence of lat/lon
            if pydash_get(claim, "mainsnak.snaktype") == "value":
                value = pydash_get(claim, "mainsnak.datavalue.value", {})
                if isinstance(value.get("latitude"), (float, int)) and isinstance(
                    value.get("longitude"), (float, int)
                ):
                    has_valid_coords = True
                    break  # Found one valid coordinate claim
        if not has_valid_coords:
            logger.debug(
                f"Item {item_id} skipped: Missing valid coordinates claim (P625)."
            )
            return False

    # Add more filters based on P31 instance_of? Example:
    # required_types = set(pydash_get(config, 'wikidata_ingestion.required_instance_of', []))
    # if required_types:
    #     instance_of_claims = item.get('claims', {}).get(P_INSTANCE_OF, [])
    #     item_types = set(pydash_get(claim, 'mainsnak.datavalue.value.id')
    #                      for claim in instance_of_claims
    #                      if pydash_get(claim, 'mainsnak.snaktype') == 'value' and pydash_get(claim, 'mainsnak.datavalue.value.id'))
    #     if not item_types.intersection(required_types):
    #         logger.debug(f"Item {item_id} skipped: Type not in required list {required_types}.")
    #         return False

    logger.debug(f"Item {item_id} passed filters.")
    return True


def extract_coordinates(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Extracts coordinates from the *first valid* P625 claim in a Wikidata item
    and returns a GeoJSON Point object.

    Args:
        item: The Wikidata item dictionary.

    Returns:
        A dictionary representing a GeoJSON Point, or None if no valid coordinates found.
    """
    item_id = item.get("id")
    try:
        coordinate_claims = item.get("claims", {}).get(P_COORDINATE_LOCATION, [])
        for claim in coordinate_claims:
            # Prioritize preferred rank if available? Not strictly necessary for now.
            if pydash_get(claim, "mainsnak.snaktype") == "value":
                value = pydash_get(claim, "mainsnak.datavalue.value", {})
                lat = value.get("latitude")
                lon = value.get("longitude")

                if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
                    # Basic validity check (though Wikidata usually enforces this)
                    if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
                        # GeoJSON format: [longitude, latitude]
                        logger.debug(
                            f"Extracted coordinates for {item_id}: lon={lon}, lat={lat}"
                        )
                        return {
                            "type": "Point",
                            "coordinates": [float(lon), float(lat)],
                        }
                    else:
                        logger.warning(
                            f"Invalid coordinate range found for {item_id}: lat={lat}, lon={lon}"
                        )
                else:
                    logger.debug(
                        f"Non-numeric coordinate types found in claim for {item_id}: lat={type(lat)}, lon={type(lon)}"
                    )

        logger.debug(f"No valid coordinate claim found for {item_id}")
        return None
    except Exception as e:
        logger.warning(
            f"Error extracting coordinates for {item_id}: {e}", exc_info=False
        )
        return None


def extract_aliases(item: Dict[str, Any]) -> Dict[str, List[str]]:
    """
    Extracts aliases grouped by language code (e.g., 'en', 'fr').
    Merges aliases from language variants (e.g., 'en-gb', 'en-ca' into 'en').
    Ensures uniqueness within each language list. Uses defaultdict internally.
    """
    aliases_by_lang: Dict[str, Set[str]] = defaultdict(set)  # defaultdict is used here
    aliases_dict = item.get("aliases", {})
    if not isinstance(aliases_dict, dict):
        return {}

    for lang_code, alias_list in aliases_dict.items():
        if not isinstance(alias_list, list):
            continue
        simple_lang = lang_code.split("-")[0]
        for alias_entry in alias_list:
            if isinstance(alias_entry, dict):
                value = alias_entry.get("value")
                if isinstance(value, str) and value:
                    aliases_by_lang[simple_lang].add(value)

    result: Dict[str, List[str]] = {
        lang: sorted(list(alias_set)) for lang, alias_set in aliases_by_lang.items()
    }
    return result


# --- Batch Prefetching Implementation ---
# Cache for item data fetched within a single batch processing cycle
_batch_item_cache: Dict[str, Optional[Dict[str, Any]]] = {}


def _get_item_data_batch_fetcher(
    qids: List[str], collection: "Collection"
) -> None:  # Use forward ref for Collection
    """
    Fetches data for multiple QIDs needed for hierarchy and caches it.
    Uses the main entity collection for lookups.
    """
    global _batch_item_cache
    # Filter out QIDs already in cache for this batch run
    qids_to_fetch = [qid for qid in qids if qid not in _batch_item_cache]
    if not qids_to_fetch:
        return

    logger.debug(f"Batch Prefetcher: Fetching data for {len(qids_to_fetch)} QIDs...")
    try:
        # Fetch necessary fields: labels, P131 claims
        projection = {
            "_id": 1,
            "english_label": 1,  # Fetch the stored English label
            "claims.P131.mainsnak.datavalue.value.id": 1,  # Fetch P131 targets
        }
        cursor = collection.find({"_id": {"$in": qids_to_fetch}}, projection)
        found_qids = set()
        for doc in cursor:
            doc_id = doc["_id"]
            _batch_item_cache[doc_id] = doc  # Store the fetched document
            found_qids.add(doc_id)

        # Mark QIDs that were requested but not found in DB as None in cache
        missing_qids = set(qids_to_fetch) - found_qids
        for qid in missing_qids:
            _batch_item_cache[qid] = None  # Explicitly mark as not found
        logger.debug(
            f"Batch Prefetcher: Fetched {len(found_qids)} docs, marked {len(missing_qids)} as missing."
        )

    except Exception as e:
        logger.error(
            f"Batch Prefetcher: Error fetching data for QIDs {qids_to_fetch}: {e}",
            exc_info=True,
        )
        # Mark all requested QIDs as None on error to avoid retries
        for qid in qids_to_fetch:
            _batch_item_cache[qid] = None


def _get_item_data_from_cache(qid: str) -> Optional[Dict[str, Any]]:
    """
    Retrieves item data from the batch cache.
    This function is passed to extract_admin_hierarchy.
    """
    global _batch_item_cache
    return _batch_item_cache.get(qid)  # Returns dict or None


# --- End Batch Prefetching ---


def extract_admin_hierarchy(
    item: Dict[str, Any], get_item_data_func: GetItemDataFunc, max_depth: int = 10
) -> List[Dict[str, Any]]:
    """
    Extracts the administrative hierarchy (P131) by traversing upwards.
    Relies on the provided get_item_data_func for fetching parent data,
    which should ideally use batching/caching (like _get_item_data_from_cache).

    Args:
        item: The starting Wikidata item dictionary.
        get_item_data_func: Callable taking QID -> Optional[item_data_dict].
        max_depth: Maximum levels to traverse upwards.

    Returns:
        An ordered list of hierarchy levels (dicts with 'qid', 'level', 'label'),
        starting from level 0 (direct parents). Empty if none.
    """
    start_time = time.time()
    item_id = item.get("id")
    hierarchy: List[Dict[str, Any]] = []
    # Get initial parent QIDs from the starting item's P131 claims
    initial_p131_claims = item.get("claims", {}).get(P_ADMIN_HIERARCHY, [])
    current_qids: List[str] = sorted(
        list(
            set(  # Sort for deterministic traversal order? Optional.
                pydash_get(claim, "mainsnak.datavalue.value.id")
                for claim in initial_p131_claims
                if pydash_get(claim, "mainsnak.snaktype") == "value"
                and pydash_get(claim, "mainsnak.datavalue.value.id")
            )
        )
    )

    visited_qids: Set[str] = {item_id} if item_id else set()
    level = 0

    logger.debug(
        f"Starting P131 traversal for {item_id} with initial parents: {current_qids}"
    )

    while current_qids and level < max_depth:
        next_level_qids_set: Set[str] = set()
        qids_processed_this_level: List[str] = []

        for qid in current_qids:
            if qid in visited_qids:
                # logger.debug(f"Skipping already visited QID {qid} at level {level} for {item_id}")
                continue
            visited_qids.add(qid)  # Mark as visited before processing
            qids_processed_this_level.append(qid)

            # Fetch data for the current parent QID using the provided function
            # This relies on the function being efficient (using cache/batching)
            parent_item_data = get_item_data_func(qid)

            if parent_item_data:
                # Extract preferred label (e.g., English) or fallback to QID
                label = pydash_get(
                    parent_item_data,
                    "english_label",  # Use stored label if available
                    pydash_get(parent_item_data, "labels.en.value", qid),
                )
                logger.debug(f"Adding level {level} for {item_id}: {qid} ({label})")
                hierarchy.append({"qid": qid, "level": level, "label": label})

                # Find the P131 claims of this parent to get the *next* level QIDs
                grandparent_claims = parent_item_data.get("claims", {}).get(
                    P_ADMIN_HIERARCHY, []
                )
                for claim in grandparent_claims:
                    next_qid = pydash_get(claim, "mainsnak.datavalue.value.id")
                    if pydash_get(claim, "mainsnak.snaktype") == "value" and next_qid:
                        # Add to the set for the *next* iteration if not already visited
                        if next_qid not in visited_qids:
                            next_level_qids_set.add(next_qid)

            else:
                # Handle case where linked item data is unavailable (e.g., not found in prefetch)
                logger.warning(
                    f"Could not retrieve data for P131 linked item {qid} (level {level}) referenced by {item_id}. Hierarchy might be incomplete."
                )
                # Optionally add a placeholder? Depends on requirements.
                # hierarchy.append({"qid": qid, "level": level, "label": f"{qid} (Data Unavailable)"})

        if not qids_processed_this_level:
            logger.debug(
                f"No new QIDs processed at level {level} for {item_id}. Stopping traversal."
            )
            break  # Avoid infinite loop if only visited QIDs remain

        # Prepare for the next level
        current_qids = sorted(list(next_level_qids_set))  # Sort for consistent order?
        level += 1

    if level == max_depth and current_qids:
        logger.warning(
            f"P131 hierarchy traversal reached max depth ({max_depth}) for item {item_id}. Remaining QIDs: {current_qids}"
        )

    end_time = time.time()
    logger.debug(
        f"Finished P131 traversal for {item_id} in {end_time - start_time:.4f}s. Found {len(hierarchy)} levels."
    )
    return hierarchy


def _parse_wikidata_time(time_str: Optional[str]) -> Optional[datetime]:
    """Attempts to parse Wikidata time strings into datetime objects."""
    if not time_str or not isinstance(time_str, str):
        return None

    # Try standard full format first
    match = re.match(r"\+?(-?\d+)-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z", time_str)
    if match:
        try:
            year, month, day, hour, minute, second = map(int, match.groups())
            # <<< MODIFIED: Check for 00 month/day specifically >>>
            if month == 0 or day == 0:
                # If month or day is 00, treat as year-only precision
                logger.debug(f"Handling '{time_str}' as year-only precision.")
                if abs(year) > 9999:
                    logger.debug(
                        f"Year {year} outside standard datetime range, storing None."
                    )
                    return None
                # Represent as the start of the year
                return datetime(year, 1, 1)
            # <<< END MODIFICATION >>>

            if abs(year) > 9999:
                logger.debug(
                    f"Year {year} outside standard datetime range, storing None."
                )
                return None
            return datetime(year, month, day, hour, minute, second)
        except ValueError as e:
            logger.warning(f"Could not parse date components from '{time_str}': {e}")
            return None

    # Try parsing just the year if format is simpler (e.g., '+2000-00-00T00:00:00Z')
    # This regex might be redundant now due to the check above, but keep as fallback
    year_match = re.match(r"\+?(-?\d{4,})-00-00T00:00:00Z", time_str)
    if year_match:
        try:
            year = int(year_match.group(1))
            if abs(year) <= 9999:
                return datetime(year, 1, 1)  # Represent as start of year
            else:
                logger.debug(
                    f"Year {year} outside standard datetime range, storing None."
                )
                return None
        except ValueError:
            logger.warning(f"Could not parse year from '{time_str}'.")
            return None

    logger.debug(f"Could not parse Wikidata time string: '{time_str}'")
    return None


def _extract_year_from_time_str(time_str: Optional[str]) -> Optional[int]:
    """Extracts year robustly from Wikidata time string, handling large years."""
    if not time_str or not isinstance(time_str, str):
        return None
    # Match leading '+' sign, optional minus sign, and 4+ digits for the year
    match = re.match(r"\+?(-?\d{4,})-.*", time_str)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return None
    return None


# <<< ADDED FUNCTION IMPLEMENTATION >>>
def extract_simple_claim_values(item: Dict[str, Any], pid: str) -> List[str]:
    """
    Extracts the string value (usually a QID) from simple claims
    (mainsnak only, value type, entity-type=item).

    Args:
        item: The Wikidata item dictionary.
        pid: The Property ID (e.g., "P31") to extract values for.

    Returns:
        A list of unique string values (QIDs) found for that property.
    """
    values: Set[str] = set()
    claims = item.get("claims", {}).get(pid, [])
    if not isinstance(claims, list):
        return []

    for claim in claims:
        # Check for basic structure and value type
        if pydash_get(claim, "mainsnak.snaktype") == "value":
            # Extract target item ID
            entity_id = pydash_get(claim, "mainsnak.datavalue.value.id")
            if isinstance(entity_id, str) and entity_id:
                values.add(entity_id)
            else:
                # Log if value exists but is not an item ID? Optional.
                # logger.debug(f"Claim for {pid} in {item.get('id')} has non-item value: {pydash_get(claim, 'mainsnak.datavalue.value')}")
                pass

    return sorted(list(values))


def extract_complex_property(
    item: Dict[str, Any], pid: str, config: Dict[str, Any]
) -> Optional[Any]:
    """
    Extracts data for configured complex properties requiring qualifier handling.

    Args:
        item: The Wikidata item dictionary.
        pid: The property ID (e.g., "P17", "P1082").
        config: The application configuration.

    Returns:
        The extracted data in the format defined by the schema, or None.
    """
    item_id = item.get("id")
    claims = item.get("claims", {}).get(pid, [])
    extracted_data = []

    if pid == P_COUNTRY:  # P17 - Country Membership
        for claim in claims:
            if pydash_get(claim, "mainsnak.snaktype") == "value":
                country_qid = pydash_get(claim, "mainsnak.datavalue.value.id")
                if country_qid:
                    entry = {
                        "country_qid": country_qid,
                        "start_year": None,
                        "end_year": None,
                    }
                    qualifiers = claim.get("qualifiers", {})
                    start_time_qual = qualifiers.get(P_START_TIME, [])
                    end_time_qual = qualifiers.get(P_END_TIME, [])
                    if (
                        start_time_qual
                        and pydash_get(start_time_qual[0], "snaktype") == "value"
                    ):
                        time_str = pydash_get(
                            start_time_qual[0], "datavalue.value.time"
                        )
                        entry["start_year"] = _extract_year_from_time_str(time_str)
                    if (
                        end_time_qual
                        and pydash_get(end_time_qual[0], "snaktype") == "value"
                    ):
                        time_str = pydash_get(end_time_qual[0], "datavalue.value.time")
                        entry["end_year"] = _extract_year_from_time_str(time_str)
                    extracted_data.append(entry)
        return extracted_data if extracted_data else None

    elif pid == P_POPULATION:  # P1082 - Population
        for claim in claims:
            if pydash_get(claim, "mainsnak.snaktype") == "value":
                amount_str = pydash_get(claim, "mainsnak.datavalue.value.amount")
                if amount_str:
                    try:
                        population_val = int(amount_str.lstrip("+"))
                        entry = {"value": population_val, "point_in_time": None}
                        qualifiers = claim.get("qualifiers", {})
                        time_qual = qualifiers.get(P_POINT_IN_TIME, [])
                        if (
                            time_qual
                            and pydash_get(time_qual[0], "snaktype") == "value"
                        ):
                            time_str = pydash_get(time_qual[0], "datavalue.value.time")
                            dt_obj = _parse_wikidata_time(time_str)
                            entry["point_in_time"] = dt_obj if dt_obj else time_str
                        extracted_data.append(entry)
                    except (ValueError, TypeError) as e:
                        logger.warning(
                            f"Could not parse population amount '{amount_str}' for {item_id}: {e}"
                        )
        return extracted_data if extracted_data else None

    elif pid == P_AREA:  # P2046 - Area
        best_area_sqkm: Optional[float] = None
        for claim in claims:
            if pydash_get(claim, "mainsnak.snaktype") == "value":
                amount_str = pydash_get(claim, "mainsnak.datavalue.value.amount")
                unit_qid = pydash_get(claim, "mainsnak.datavalue.value.unit", "").split(
                    "/"
                )[-1]
                if amount_str and unit_qid:
                    try:
                        area_val = float(amount_str.lstrip("+"))
                        if unit_qid == Q_SQUARE_KILOMETER:
                            area_sqkm = area_val
                            best_area_sqkm = area_sqkm
                            break
                        else:
                            logger.debug(
                                f"Area unit {unit_qid} not handled for {item_id}. Value: {area_val}"
                            )
                    except (ValueError, TypeError) as e:
                        logger.warning(
                            f"Could not parse area amount '{amount_str}' for {item_id}: {e}"
                        )
        return best_area_sqkm

    elif pid == P_INCEPTION:  # P571 - Inception Date
        earliest_year: Optional[int] = None
        for claim in claims:
            if pydash_get(claim, "mainsnak.snaktype") == "value":
                time_str = pydash_get(claim, "mainsnak.datavalue.value.time")
                year = _extract_year_from_time_str(time_str)
                if year is not None:
                    if earliest_year is None or year < earliest_year:
                        earliest_year = year
        return earliest_year

    elif pid == P_DISSOLVED:  # P576 - Dissolved/Abolished Date
        earliest_year: Optional[int] = None
        for claim in claims:
            if pydash_get(claim, "mainsnak.snaktype") == "value":
                time_str = pydash_get(claim, "mainsnak.datavalue.value.time")
                year = _extract_year_from_time_str(time_str)
                if year is not None:
                    if earliest_year is None or year < earliest_year:
                        earliest_year = year
        return earliest_year

    else:
        logger.warning(f"Complex property extractor not implemented for PID: {pid}")
        return None


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
        "last_updated": None,
    }

    # --- Extract Basic Information ---
    target_doc["english_label"] = pydash_get(item, "labels.en.value")
    target_doc["english_description"] = pydash_get(item, "descriptions.en.value")
    target_doc["aliases"] = extract_aliases(item)

    # --- Extract Configured Properties ---
    properties_to_extract = pydash_get(
        config, "wikidata_ingestion.properties_to_extract", []
    )

    # P31 - Instance Of (Uses the newly added function)
    if P_INSTANCE_OF in properties_to_extract:
        target_doc["instance_of"] = extract_simple_claim_values(
            item, P_INSTANCE_OF
        )  # <<< Now calls the implemented function

    # P625 - Coordinates
    if P_COORDINATE_LOCATION in properties_to_extract:
        target_doc["coordinates"] = extract_coordinates(item)

    # --- Extract Complex Properties ---
    if P_COUNTRY in properties_to_extract:
        target_doc["country_membership"] = extract_complex_property(
            item, P_COUNTRY, config
        )
    if P_POPULATION in properties_to_extract:
        target_doc["population"] = extract_complex_property(item, P_POPULATION, config)
    if P_AREA in properties_to_extract:
        target_doc["area_sqkm"] = extract_complex_property(item, P_AREA, config)

    dates_data: Dict[str, Optional[int]] = {}
    if P_INCEPTION in properties_to_extract:
        dates_data["inception_year"] = extract_complex_property(
            item, P_INCEPTION, config
        )
    if P_DISSOLVED in properties_to_extract:
        dates_data["dissolved_year"] = extract_complex_property(
            item, P_DISSOLVED, config
        )
    if dates_data:
        target_doc["dates"] = dates_data

    # P131 - Admin Hierarchy
    if P_ADMIN_HIERARCHY in properties_to_extract:
        # Pass the actual get_item_data_func provided to this function
        target_doc["admin_hierarchy"] = extract_admin_hierarchy(
            item, get_item_data_func
        )

    # Extract sitelink title (normalized)
    enwiki_title = pydash_get(item, "sitelinks.enwiki.title")
    if enwiki_title:
        # Import locally to avoid circular dependency if helpers imports wikidata_helpers
        from utils.helpers import normalize_wikipedia_title

        normalized_title = normalize_wikipedia_title(enwiki_title)
        if normalized_title:
            if "sitelinks" not in target_doc:
                target_doc["sitelinks"] = {}
            if "enwiki" not in target_doc["sitelinks"]:
                target_doc["sitelinks"]["enwiki"] = {}
            target_doc["sitelinks"]["enwiki"]["normalized_title"] = normalized_title

    # --- Fields added later ---
    target_doc["corrected_class"] = None
    target_doc["wiki_links"] = None
    target_doc["ontology_flag_resolved"] = False
    target_doc["embedding_vector"] = None

    return target_doc
