# tests/unit/test_config.py

import pytest
import os
import sys
import yaml
from unittest.mock import patch, MagicMock, call
from typing import Dict, Any, Generator

# Adjust path to import utils
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

# Import after potentially modifying sys.path
import utils.config
from utils.config import (
    load_config,
    get_config,
    resolve_value,
    ConfigurationError,
    get_base_dir,
)
from pydash import get as pydash_get

# --- Fixtures ---


@pytest.fixture(scope="session")
def project_root_for_tests() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def dummy_env_vars(monkeypatch) -> None:
    env_vars = {
        "MONGO_URI": "mongodb://env_user:env_pass@localhost:27017/env_db",
        "WIKIDATA_DUMP_FILENAME": "wikidata-env.json.bz2",
        "SECRET_KEY": "env_secret",
        "PROC_DIR": "data_test/processed_via_env",
        "EMPTY_VAR": "",
        "HAS_DEFAULT_VAL": "env_value",
    }
    for key, value in env_vars.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("MISSING_VAR", raising=False)
    monkeypatch.delenv("NO_DEFAULT_VAR", raising=False)


@pytest.fixture
def dummy_yaml_content_raw() -> str:
    # Raw string content as before
    return """
logging:
  level: DEBUG
  log_file_path: logs/test_pipeline.log
paths:
  data_dir: data_test/raw
  processing_dir: ${PROC_DIR}
  output_dir: ${config.paths.data_dir}/../output_sibling
  temp_dir: /tmp/wiki2gaz_test
mongodb:
  uri: ${MONGO_URI}
  database: test_db
  collection_entities: entities_test
wikidata_ingestion:
  dump_filename: ${WIKIDATA_DUMP_FILENAME:-default.json.bz2}
  complex_path: "${config.paths.processing_dir}/subfolder" # Reference processing_dir
pipeline:
  api_key: ${SECRET_KEY}
  empty_env: ${EMPTY_VAR}
  has_default: ${HAS_DEFAULT_VAL:-yaml_default}
  missing_var_test: ${MISSING_VAR:-fallback_value}
  no_default_test: ${NO_DEFAULT_VAR}
other:
  boolean_true: true
  boolean_false: false
  integer_val: 123
  float_val: 45.67
  list_val:
    - item1
    - ${config.pipeline.api_key} # Reference inside list, should resolve to string 'env_secret'
  nested_dict:
    level1:
      level2: ${config.other.integer_val} # Reference inside dict, should resolve to integer 123
"""


@pytest.fixture
def dummy_yaml_file_raw(tmp_path: Any, dummy_yaml_content_raw: str) -> str:
    config_file = tmp_path / "config_test_raw.yaml"
    config_file.write_text(dummy_yaml_content_raw, encoding="utf-8")
    return str(config_file)


# REMOVED autouse=True fixture - manage cache explicitly in tests needing it
@pytest.fixture
def clear_config_cache_manual() -> None:
    """Manually clear the config cache."""
    utils.config._config = None
    utils.config._base_dir = None


# --- Test Functions ---


def test_load_config_basic(
    dummy_yaml_file_raw: str,
    dummy_env_vars: None,
    project_root_for_tests: str,
    clear_config_cache_manual: None,
):
    config = load_config(
        config_path=dummy_yaml_file_raw, base_dir=project_root_for_tests
    )
    assert config is not None
    assert isinstance(config, dict)
    assert pydash_get(config, "logging.level") == "DEBUG"
    assert pydash_get(config, "mongodb.database") == "test_db"


def test_env_var_resolution(
    dummy_yaml_file_raw: str,
    dummy_env_vars: None,
    project_root_for_tests: str,
    clear_config_cache_manual: None,
):
    config = load_config(
        config_path=dummy_yaml_file_raw, base_dir=project_root_for_tests
    )
    assert (
        pydash_get(config, "mongodb.uri")
        == "mongodb://env_user:env_pass@localhost:27017/env_db"
    )
    assert pydash_get(config, "pipeline.api_key") == "env_secret"
    assert pydash_get(config, "paths.processing_dir").endswith(
        "data_test/processed_via_env"
    )
    assert pydash_get(config, "pipeline.empty_env") == ""


def test_env_var_with_default(
    dummy_yaml_file_raw: str,
    dummy_env_vars: None,
    project_root_for_tests: str,
    monkeypatch: Any,
    clear_config_cache_manual: None,
):
    # Case 1: Env var IS set
    config = load_config(
        config_path=dummy_yaml_file_raw, base_dir=project_root_for_tests
    )
    assert (
        pydash_get(config, "wikidata_ingestion.dump_filename")
        == "wikidata-env.json.bz2"
    )
    assert pydash_get(config, "pipeline.has_default") == "env_value"

    # Case 2: Env var is NOT set
    monkeypatch.delenv("WIKIDATA_DUMP_FILENAME", raising=False)
    monkeypatch.delenv("HAS_DEFAULT_VAL", raising=False)
    monkeypatch.delenv("MISSING_VAR", raising=False)

    config_reloaded = load_config(
        config_path=dummy_yaml_file_raw,
        base_dir=project_root_for_tests,
        force_reload=True,
    )
    assert (
        pydash_get(config_reloaded, "wikidata_ingestion.dump_filename")
        == "default.json.bz2"
    )
    assert pydash_get(config_reloaded, "pipeline.has_default") == "yaml_default"
    assert pydash_get(config_reloaded, "pipeline.missing_var_test") == "fallback_value"


def test_missing_env_var_no_default(
    dummy_yaml_file_raw: str,
    dummy_env_vars: None,
    project_root_for_tests: str,
    clear_config_cache_manual: None,
):
    config = load_config(
        config_path=dummy_yaml_file_raw, base_dir=project_root_for_tests
    )
    assert pydash_get(config, "pipeline.no_default_test") == ""


def test_config_ref_resolution(
    dummy_yaml_file_raw: str,
    dummy_env_vars: None,
    project_root_for_tests: str,
    clear_config_cache_manual: None,
):
    """Test resolution of config references, checking type preservation."""
    config = load_config(
        config_path=dummy_yaml_file_raw, base_dir=project_root_for_tests
    )
    assert "output_sibling" in os.path.basename(
        pydash_get(config, "paths.output_dir", "")
    )
    assert os.path.isabs(pydash_get(config, "paths.output_dir", ""))

    # Check type preservation: api_key resolves to string
    assert pydash_get(config, "other.list_val")[1] == "env_secret"
    assert isinstance(pydash_get(config, "other.list_val")[1], str)

    # Check type preservation: integer_val resolves to int
    resolved_int = pydash_get(config, "other.nested_dict.level1.level2")
    assert resolved_int == 123
    assert isinstance(resolved_int, int)  # <<< FIXED ASSERTION


def test_complex_nested_resolution(
    dummy_yaml_file_raw: str,
    dummy_env_vars: None,
    project_root_for_tests: str,
    clear_config_cache_manual: None,
):
    """Test resolution involving env vars, config refs, and path absolutization."""
    config = load_config(
        config_path=dummy_yaml_file_raw, base_dir=project_root_for_tests
    )
    resolved_path = pydash_get(config, "wikidata_ingestion.complex_path")

    # Construct expected absolute path more robustly
    expected_processing_dir_abs = os.path.abspath(
        os.path.join(project_root_for_tests, "data_test/processed_via_env")
    )
    expected_complex_path_abs = os.path.join(expected_processing_dir_abs, "subfolder")

    assert resolved_path is not None
    assert os.path.isabs(resolved_path)
    # Use os.path.normpath for platform-agnostic comparison
    assert os.path.normpath(resolved_path) == os.path.normpath(
        expected_complex_path_abs
    )  # <<< FIXED ASSERTION


def test_path_absolutization(
    dummy_yaml_file_raw: str,
    dummy_env_vars: None,
    project_root_for_tests: str,
    clear_config_cache_manual: None,
):
    config = load_config(
        config_path=dummy_yaml_file_raw, base_dir=project_root_for_tests
    )
    assert os.path.isabs(pydash_get(config, "paths.data_dir"))
    assert pydash_get(config, "paths.data_dir").endswith("data_test/raw")
    assert os.path.isabs(pydash_get(config, "logging.log_file_path"))
    assert pydash_get(config, "logging.log_file_path").endswith(
        "logs/test_pipeline.log"
    )
    assert pydash_get(config, "paths.temp_dir") == "/tmp/wiki2gaz_test"


def test_circular_ref_detection(
    tmp_path: Any, project_root_for_tests: str, clear_config_cache_manual: None
):
    bad_yaml_content = """
circle_a: ${config.circle_b}
circle_b: ${config.circle_a}
"""
    bad_yaml_file = tmp_path / "bad_config.yaml"
    bad_yaml_file.write_text(bad_yaml_content, encoding="utf-8")
    with pytest.raises(ValueError, match=r"Circular configuration reference detected"):
        load_config(
            config_path=str(bad_yaml_file),
            base_dir=project_root_for_tests,
            force_reload=True,
        )


@patch("utils.config.load_config")
def test_get_config_loads_once_isolated(mock_load_config: MagicMock):
    """Test get_config() caching behavior explicitly, ensuring mock sets cache."""
    # Explicitly manage the cache state for this test
    utils.config._config = None  # Ensure cache starts empty
    utils.config._base_dir = None
    mock_return_value = {"test": "value_from_mock_isolated"}

    # --- Define side effect function for the mock ---
    def mock_load_config_side_effect(*args, **kwargs):
        print("Mock load_config called!")  # Debug print
        # Simulate the real function: set the global cache
        utils.config._config = mock_return_value
        # Also set base_dir if it's relevant to subsequent calls (optional here)
        # utils.config._base_dir = get_base_dir() # Or extract from kwargs if needed
        return mock_return_value

    # --- Assign the side effect ---
    mock_load_config.side_effect = mock_load_config_side_effect

    # First call should trigger load_config (via side effect)
    print("Calling get_config first time...")
    cfg1 = get_config()
    mock_load_config.assert_called_once()
    assert cfg1 == mock_return_value
    # Verify the cache *was* set by the side effect
    assert utils.config._config is cfg1  # Check if module variable was set

    # Reset call count for the next check
    mock_load_config.reset_mock()

    # Second call should use the cache
    print("Calling get_config second time...")
    cfg2 = get_config()
    # Assert mock was NOT called again
    mock_load_config.assert_not_called()
    assert cfg2 is cfg1  # Should be the exact same cached object

    # Clean up cache manually after test
    utils.config._config = None
    utils.config._base_dir = None


def test_non_string_values_untouched(
    dummy_yaml_file_raw: str,
    dummy_env_vars: None,
    project_root_for_tests: str,
    clear_config_cache_manual: None,
):
    config = load_config(
        config_path=dummy_yaml_file_raw, base_dir=project_root_for_tests
    )
    assert pydash_get(config, "other.boolean_true") is True
    assert pydash_get(config, "other.boolean_false") is False
    assert pydash_get(config, "other.integer_val") == 123
    assert pydash_get(config, "other.float_val") == 45.67


def test_config_file_not_found(
    project_root_for_tests: str, clear_config_cache_manual: None
):
    with pytest.raises(ConfigurationError, match="Configuration file not found"):
        load_config(
            config_path="nonexistent_config.yaml", base_dir=project_root_for_tests
        )


def test_invalid_yaml(
    tmp_path: Any, project_root_for_tests: str, clear_config_cache_manual: None
):
    invalid_yaml_file = tmp_path / "invalid.yaml"
    invalid_yaml_file.write_text("logging: level: DEBUG\npath: /bad", encoding="utf-8")
    with pytest.raises(
        ConfigurationError, match="Error parsing YAML configuration file"
    ):
        load_config(config_path=str(invalid_yaml_file), base_dir=project_root_for_tests)


def test_resolve_value_direct(clear_config_cache_manual: None):
    """Direct tests for the resolve_value function, focusing on type preservation."""
    config_dict = {
        "a": {"b": 10, "b_str": "10"},
        "c": "${config.a.b}",  # Should resolve to int 10
        "c_str": "${config.a.b_str}",  # Should resolve to str "10"
        "d": ["${config.a.b}", 20],  # Should resolve to [10, 20]
        "e": "Value is ${config.a.b}",  # Should resolve to str "Value is 10"
        "f": True,
        "g": "${config.f}",  # Should resolve to bool True
    }
    os.environ["TEST_VAR"] = "env_value"
    os.environ["TEST_VAR_DEFAULT"] = "env_override"

    assert resolve_value("${TEST_VAR}", config_dict) == "env_value"
    assert resolve_value("${MISSING_VAR:-default}", config_dict) == "default"
    assert (
        resolve_value("${TEST_VAR_DEFAULT:-yaml_default}", config_dict)
        == "env_override"
    )

    # Test type preservation for config refs
    resolved_c = resolve_value("${config.c}", config_dict)
    assert resolved_c == 10 and isinstance(resolved_c, int)

    resolved_c_str = resolve_value("${config.c_str}", config_dict)
    assert resolved_c_str == "10" and isinstance(resolved_c_str, str)

    resolved_g = resolve_value("${config.g}", config_dict)
    assert resolved_g is True and isinstance(resolved_g, bool)

    # Test substitution into string
    resolved_e = resolve_value("${config.e}", config_dict)
    assert resolved_e == "Value is 10" and isinstance(resolved_e, str)

    # Test resolving within list, preserving types
    resolved_d = resolve_value(config_dict["d"], config_dict)
    assert resolved_d == [10, 20]
    assert isinstance(resolved_d[0], int)

    # Clean up env vars
    del os.environ["TEST_VAR"]
    del os.environ["TEST_VAR_DEFAULT"]
