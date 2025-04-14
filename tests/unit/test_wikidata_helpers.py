import pytest
import sys
import os
from unittest.mock import patch, MagicMock, call
from typing import Dict, Any, List, Optional, Set  # Added necessary types
from collections import defaultdict  # Added for defaultdict used in extract_aliases
from datetime import datetime  # For testing date parsing

# Adjust path to import utils
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

# Import necessary components from the module under test
# Ensure NO import of _p31_cache exists
from utils.wikidata_helpers import (
    filter_wikidata_item,
    extract_coordinates,
    extract_admin_hierarchy,
    extract_aliases,
    extract_entity_data,
    GetItemDataFunc,
    _get_item_data_batch_fetcher,
    _get_item_data_from_cache,
    _batch_item_cache,  # <<< ENSURE THIS IS THE ONLY CACHE IMPORTED/USED
    extract_complex_property,
    _parse_wikidata_time,
    _extract_year_from_time_str,
    extract_simple_claim_values,  # <<< Import the added function
    P_INSTANCE_OF,
    P_COORDINATE_LOCATION,
    P_ADMIN_HIERARCHY,
    P_COUNTRY,
    P_POPULATION,
    P_AREA,
    P_INCEPTION,
    P_DISSOLVED,
    P_START_TIME,  # Import constants used in tests
    P_END_TIME,
)
from pymongo.collection import Collection  # For type hinting

# --- Fixtures ---


@pytest.fixture
def sample_config() -> dict:
    """Provides a basic config dictionary for tests."""
    return {
        "wikidata_ingestion": {
            "require_enwiki_sitelink": True,
            "require_coordinates": True,
            "properties_to_extract": [
                P_INSTANCE_OF,
                P_COUNTRY,
                P_ADMIN_HIERARCHY,
                P_COORDINATE_LOCATION,
                P_POPULATION,
                P_AREA,
                P_INCEPTION,
                P_DISSOLVED,
            ],
        },
        "paths": {},
        "mongodb": {},
    }


@pytest.fixture
def sample_item_new_york() -> dict:
    """A sample Wikidata item dictionary for New York City."""
    # Re-using the static dict definition is fine here
    return _MOCK_NYC_ITEM.copy()  # Return a copy to avoid modification across tests


@pytest.fixture
def sample_item_london() -> dict:
    return {
        "id": "Q84",
        "type": "item",
        "labels": {"en": {"language": "en", "value": "London"}},
        "aliases": {"en": [{"language": "en", "value": "City of London"}]},
        "claims": {
            P_INSTANCE_OF: [
                {
                    "mainsnak": {
                        "snaktype": "value",
                        "datavalue": {"value": {"id": "Q515"}},
                    }
                }
            ],
            P_COORDINATE_LOCATION: [
                {
                    "mainsnak": {
                        "snaktype": "value",
                        "datavalue": {
                            "value": {"latitude": 51.5072, "longitude": -0.1275}
                        },
                    }
                }
            ],
            P_ADMIN_HIERARCHY: [
                {
                    "mainsnak": {
                        "snaktype": "value",
                        "datavalue": {"value": {"id": "Q21"}},
                    }
                }
            ],
            P_COUNTRY: [
                {
                    "mainsnak": {
                        "snaktype": "value",
                        "datavalue": {"value": {"id": "Q145"}},
                    }
                }
            ],
        },
        "sitelinks": {"enwiki": {"site": "enwiki", "title": "London"}},
    }


@pytest.fixture
def sample_item_no_coords() -> dict:
    return {
        "id": "Q1000",
        "type": "item",
        "claims": {},
        "sitelinks": {"enwiki": {"title": "No Coords Test"}},
    }


@pytest.fixture
def sample_item_no_sitelink() -> dict:
    return {
        "id": "Q1001",
        "type": "item",
        "claims": {
            P_COORDINATE_LOCATION: [
                {
                    "mainsnak": {
                        "snaktype": "value",
                        "datavalue": {"value": {"latitude": 1.0, "longitude": 1.0}},
                    }
                }
            ]
        },
    }


# --- Module-level Data for Mocks (Defined Statically) ---
_MOCK_NYC_ITEM = {
    "id": "Q60",
    "type": "item",
    "labels": {"en": {"language": "en", "value": "New York City"}},
    "descriptions": {"en": {"language": "en", "value": "city in New York State, USA"}},
    "aliases": {
        "en": [
            {"language": "en", "value": "NYC"},
            {"language": "en", "value": "New York"},
        ],
        "es": [{"language": "es", "value": "Nueva York"}],
    },
    "claims": {
        P_INSTANCE_OF: [
            {
                "mainsnak": {
                    "snaktype": "value",
                    "property": P_INSTANCE_OF,
                    "datavalue": {
                        "value": {
                            "entity-type": "item",
                            "numeric-id": 515,
                            "id": "Q515",
                        }
                    },
                },
                "type": "statement",
                "rank": "normal",
            }
        ],
        P_COORDINATE_LOCATION: [
            {
                "mainsnak": {
                    "snaktype": "value",
                    "property": P_COORDINATE_LOCATION,
                    "datavalue": {
                        "value": {
                            "latitude": 40.7128,
                            "longitude": -74.006,
                            "precision": 0.0001,
                            "globe": "http://www.wikidata.org/entity/Q2",
                        }
                    },
                },
                "type": "statement",
                "rank": "preferred",
            }
        ],
        P_ADMIN_HIERARCHY: [
            {
                "mainsnak": {
                    "snaktype": "value",
                    "property": P_ADMIN_HIERARCHY,
                    "datavalue": {
                        "value": {
                            "entity-type": "item",
                            "numeric-id": 1384,
                            "id": "Q1384",
                        }
                    },
                },
                "type": "statement",
                "rank": "normal",
            }
        ],
        P_COUNTRY: [
            {
                "mainsnak": {
                    "snaktype": "value",
                    "property": P_COUNTRY,
                    "datavalue": {
                        "value": {"entity-type": "item", "numeric-id": 30, "id": "Q30"}
                    },
                },
                "type": "statement",
                "rank": "normal",
            }
        ],
    },
    "sitelinks": {"enwiki": {"site": "enwiki", "title": "New York City", "badges": []}},
}

mock_hierarchy_data = {
    "Q60": _MOCK_NYC_ITEM,
    "Q1384": {
        "id": "Q1384",
        "labels": {"en": {"value": "New York State"}},
        "claims": {
            P_ADMIN_HIERARCHY: [
                {
                    "mainsnak": {
                        "snaktype": "value",
                        "datavalue": {"value": {"id": "Q30"}},
                    }
                }
            ]
        },
    },
    "Q30": {"id": "Q30", "labels": {"en": {"value": "United States"}}, "claims": {}},
}


# --- Mock Function Using Static Data ---
def mock_get_item_data(qid: str) -> Optional[dict]:
    """Mock function to return data for hierarchy test from static dict."""
    return mock_hierarchy_data.get(qid)


# --- Test Functions ---


# Test filter_wikidata_item
def test_filter_passes(sample_item_new_york, sample_config):
    assert filter_wikidata_item(sample_item_new_york, sample_config) is True


def test_filter_fails_no_sitelink(sample_item_no_sitelink, sample_config):
    config_req_sitelink = sample_config.copy()
    config_req_sitelink["wikidata_ingestion"]["require_enwiki_sitelink"] = True
    config_req_sitelink["wikidata_ingestion"]["require_coordinates"] = False
    assert filter_wikidata_item(sample_item_no_sitelink, config_req_sitelink) is False


def test_filter_fails_no_coords(sample_item_no_coords, sample_config):
    config_req_coords = sample_config.copy()
    config_req_coords["wikidata_ingestion"]["require_enwiki_sitelink"] = False
    config_req_coords["wikidata_ingestion"]["require_coordinates"] = True
    assert filter_wikidata_item(sample_item_no_coords, config_req_coords) is False


def test_filter_passes_no_requirements(sample_item_no_coords, sample_config):
    config_no_reqs = sample_config.copy()
    config_no_reqs["wikidata_ingestion"]["require_enwiki_sitelink"] = False
    config_no_reqs["wikidata_ingestion"]["require_coordinates"] = False
    assert filter_wikidata_item(sample_item_no_coords, config_no_reqs) is True


def test_filter_non_item_type(sample_config):
    non_item = {"id": "Q1", "type": "property"}
    assert filter_wikidata_item(non_item, sample_config) is False


# Test extract_coordinates
def test_extract_coordinates_present(sample_item_new_york):
    coords = extract_coordinates(sample_item_new_york)
    assert coords == {"type": "Point", "coordinates": [-74.006, 40.7128]}


def test_extract_coordinates_absent(sample_item_no_coords):
    assert extract_coordinates(sample_item_no_coords) is None


def test_extract_coordinates_malformed():
    item = {
        "id": "QBAD",
        "claims": {
            P_COORDINATE_LOCATION: [
                {
                    "mainsnak": {
                        "snaktype": "value",
                        "datavalue": {"value": {"latitude": "invalid"}},
                    }
                }
            ]
        },
    }
    assert extract_coordinates(item) is None


# Test extract_aliases
def test_extract_aliases(sample_item_new_york, sample_item_london):
    aliases_nyc = extract_aliases(sample_item_new_york)
    assert aliases_nyc == {"en": ["NYC", "New York"], "es": ["Nueva York"]}

    aliases_lon = extract_aliases(sample_item_london)
    assert aliases_lon == {"en": ["City of London"]}


def test_extract_aliases_empty():
    item_empty = {"id": "QEMPTY", "aliases": {}}
    assert extract_aliases(item_empty) == {}
    item_no_alias = {"id": "QNONE"}
    assert extract_aliases(item_no_alias) == {}


# Test extract_admin_hierarchy (using the corrected mock_get_item_data)
@patch("utils.wikidata_helpers._batch_item_cache", {})
def test_extract_admin_hierarchy_simple(sample_item_new_york):
    hierarchy = extract_admin_hierarchy(sample_item_new_york, mock_get_item_data)
    assert hierarchy == [
        {"qid": "Q1384", "level": 0, "label": "New York State"},
        {"qid": "Q30", "level": 1, "label": "United States"},
    ]


# Test _get_item_data_batch_fetcher & _get_item_data_from_cache
@patch("utils.wikidata_helpers._batch_item_cache", {})
@patch("utils.mongo_helpers.get_entity_collection")
def test_batch_prefetching(mock_get_collection):
    mock_collection = MagicMock(spec=Collection)
    mock_cursor = MagicMock()
    mock_cursor.__iter__.return_value = iter(
        [
            {
                "_id": "Q10",
                "english_label": "County X",
                "claims": {
                    P_ADMIN_HIERARCHY: [
                        {
                            "mainsnak": {
                                "snaktype": "value",
                                "datavalue": {"value": {"id": "Q100"}},
                            }
                        }
                    ]
                },
            },
            {"_id": "Q100", "english_label": "State Y", "claims": {}},
        ]
    )
    mock_collection.find.return_value = mock_cursor

    qids_to_request = ["Q10", "Q100", "Q999"]
    _get_item_data_batch_fetcher(qids_to_request, mock_collection)

    mock_collection.find.assert_called_once_with(
        {"_id": {"$in": qids_to_request}},
        {"_id": 1, "english_label": 1, "claims.P131.mainsnak.datavalue.value.id": 1},
    )

    # Check cache via retriever
    assert _get_item_data_from_cache("Q10")["_id"] == "Q10"
    assert _get_item_data_from_cache("Q100")["_id"] == "Q100"
    assert _get_item_data_from_cache("Q999") is None
    assert _get_item_data_from_cache("Q_OTHER") is None


# Test extract_entity_data
@patch("utils.wikidata_helpers.extract_aliases")
@patch("utils.wikidata_helpers.extract_coordinates")
@patch("utils.wikidata_helpers.extract_admin_hierarchy")
@patch("utils.wikidata_helpers.extract_complex_property")
@patch(
    "utils.wikidata_helpers.extract_simple_claim_values"
)  # Patch the *real* function now
@patch("utils.helpers.normalize_wikipedia_title")
def test_extract_entity_data(
    mock_normalize_title: MagicMock,
    mock_extract_simple: MagicMock,
    mock_extract_complex: MagicMock,
    mock_extract_hierarchy: MagicMock,
    mock_extract_coords: MagicMock,
    mock_extract_aliases: MagicMock,
    sample_item_new_york: Dict[str, Any],
    sample_config: Dict[str, Any],
):
    # --- Configure Mock Return Values ---
    mock_extract_aliases.return_value = {"en": ["NYC", "New York"]}
    mock_extract_coords.return_value = {
        "type": "Point",
        "coordinates": [-74.006, 40.7128],
    }
    mock_extract_hierarchy.return_value = [
        {"qid": "Q1384", "level": 0, "label": "New York State"}
    ]
    mock_normalize_title.return_value = "New_York_City_Normalized"
    # Mock simple specifically for P31
    mock_extract_simple.side_effect = (
        lambda item, pid: ["Q515"] if pid == P_INSTANCE_OF else []
    )

    def complex_side_effect(item, pid, config):
        if pid == P_COUNTRY:
            return [{"country_qid": "Q30", "start_year": None, "end_year": None}]
        elif pid == P_POPULATION:
            return [{"value": 8000000, "point_in_time": None}]
        elif pid == P_AREA:
            return 783.8
        elif pid == P_INCEPTION:
            return 1624
        elif pid == P_DISSOLVED:
            return None
        else:
            return None

    mock_extract_complex.side_effect = complex_side_effect

    dummy_get_data_func: GetItemDataFunc = lambda qid: None

    # Call the function under test
    processed_data = extract_entity_data(
        sample_item_new_york, sample_config, dummy_get_data_func
    )

    # --- Assertions ---
    assert processed_data["_id"] == "Q60"
    assert processed_data["english_label"] == "New York City"
    assert processed_data["aliases"]["en"] == ["NYC", "New York"]
    assert processed_data["coordinates"]["coordinates"] == [-74.006, 40.7128]
    assert processed_data["admin_hierarchy"][0]["qid"] == "Q1384"
    assert processed_data["instance_of"] == ["Q515"]
    assert processed_data["country_membership"][0]["country_qid"] == "Q30"
    assert processed_data["population"][0]["value"] == 8000000
    assert processed_data["area_sqkm"] == 783.8
    assert processed_data["dates"]["inception_year"] == 1624
    assert (
        processed_data["sitelinks"]["enwiki"]["normalized_title"]
        == "New_York_City_Normalized"
    )

    # --- Verify Mocks Were Called Correctly ---
    mock_extract_aliases.assert_called_once_with(sample_item_new_york)
    mock_extract_coords.assert_called_once_with(sample_item_new_york)
    # <<< CORRECTED ASSERTION: Remove max_depth as it uses default >>>
    mock_extract_hierarchy.assert_called_once_with(
        sample_item_new_york, dummy_get_data_func
    )
    mock_normalize_title.assert_called_once_with("New York City")
    # Check the simple claims call
    mock_extract_simple.assert_any_call(sample_item_new_york, P_INSTANCE_OF)

    expected_complex_calls = [
        call(sample_item_new_york, pid, sample_config)
        for pid in [P_COUNTRY, P_POPULATION, P_AREA, P_INCEPTION, P_DISSOLVED]
    ]
    mock_extract_complex.assert_has_calls(expected_complex_calls, any_order=True)
    assert mock_extract_complex.call_count == len(expected_complex_calls)


# Test date/year helpers (including the fix for YYYY-00-00)
def test_parse_wikidata_time_valid():
    assert _parse_wikidata_time("+1995-11-17T00:00:00Z") == datetime(
        1995, 11, 17, 0, 0, 0
    )
    assert _parse_wikidata_time("+0500-01-01T00:00:00Z") == datetime(500, 1, 1, 0, 0, 0)


def test_parse_wikidata_time_only_year():
    # <<< VERIFY FIXED LOGIC >>>
    assert _parse_wikidata_time("+2000-00-00T00:00:00Z") == datetime(2000, 1, 1)
    # Test with month=0 but day!=0 (should still parse as year start)
    assert _parse_wikidata_time("+1985-00-15T00:00:00Z") == datetime(1985, 1, 1)


def test_parse_wikidata_time_invalid():
    assert _parse_wikidata_time("invalid date") is None
    assert _parse_wikidata_time("+10000-01-01T00:00:00Z") is None
    assert _parse_wikidata_time(None) is None


def test_extract_year_from_time_str():
    assert _extract_year_from_time_str("+1995-11-17T00:00:00Z") == 1995
    assert _extract_year_from_time_str("+2023-00-00T00:00:00Z") == 2023
    assert _extract_year_from_time_str("-0475-03-15T00:00:00Z") == -475
    assert _extract_year_from_time_str("+12000-01-01T00:00:00Z") == 12000
    assert _extract_year_from_time_str("invalid") is None
    assert _extract_year_from_time_str(None) is None


# Test complex property extractors
def test_extract_complex_property_country_with_dates():
    item_with_hist_country = {
        "id": "QTESTC",
        "claims": {
            P_COUNTRY: [
                {
                    "mainsnak": {
                        "snaktype": "value",
                        "datavalue": {"value": {"id": "Q30"}},
                    },
                    "qualifiers": {
                        P_START_TIME: [
                            {
                                "snaktype": "value",
                                "datavalue": {
                                    "value": {"time": "+1950-01-01T00:00:00Z"}
                                },
                            }
                        ]
                    },
                },
                {
                    "mainsnak": {
                        "snaktype": "value",
                        "datavalue": {"value": {"id": "Q145"}},
                    },
                    "qualifiers": {
                        P_END_TIME: [
                            {
                                "snaktype": "value",
                                "datavalue": {
                                    "value": {"time": "+1949-12-31T00:00:00Z"}
                                },
                            }
                        ]
                    },
                },
            ]
        },
    }
    config = {}
    result = extract_complex_property(item_with_hist_country, P_COUNTRY, config)
    assert result == [
        {"country_qid": "Q30", "start_year": 1950, "end_year": None},
        {"country_qid": "Q145", "start_year": None, "end_year": 1949},
    ]


# Add tests for other complex property extractors here...
