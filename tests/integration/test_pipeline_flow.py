import pytest
import subprocess
import os
import mongomock  # Use testcontainers if a real Mongo is needed
import yaml
from dotenv import dotenv_values
import sys  # For python executable path
from unittest.mock import patch, MagicMock, call
import importlib  # To import modules dynamically
from typing import List  # Added for type hint
from pymongo.database import Database  # For spec in mock
from pymongo.collection import Collection  # For spec in mock

# --- Test Setup ---

# Make utils and scripts discoverable for direct import
SRC_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if SRC_PATH not in sys.path:
    sys.path.insert(0, SRC_PATH)

# No longer need mock_mongo_client fixture at module scope for patching here


@pytest.fixture(scope="function")  # Use function scope to get clean DB for each test
def mock_db():
    """Provides a clean mock database for a test by returning a new client each time."""
    client = mongomock.MongoClient()
    db_name = "integration_test_db"
    db = client[db_name]
    yield db
    # Clean up collections after test
    for collection_name in db.list_collection_names():
        db.drop_collection(collection_name)
    client.close()  # Close the mock client


@pytest.fixture(scope="session")
def project_root():
    """Returns the absolute path to the project root directory."""
    # Assumes tests/integration is two levels down from project root
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(scope="session")
def test_config_path(project_root, tmp_path_factory):
    """Creates a temporary config file pointing to the mock MongoDB."""
    base_config_path = os.path.join(project_root, "config", "config.yaml")

    if not os.path.exists(base_config_path):
        pytest.fail(f"Base config file not found at {base_config_path}")

    with open(base_config_path, "r") as f:
        config_data = yaml.safe_load(f)

    # Override MongoDB settings for mock
    # The actual URI doesn't matter much now since we patch get_mongo_client,
    # but keep it reasonable.
    config_data["mongodb"][
        "uri"
    ] = f"mongodb://mock-host:27017/integration_test_db?serverSelectionTimeoutMS=100"
    config_data["mongodb"]["database"] = "integration_test_db"
    config_data["mongodb"]["collection_entities"] = "test_entities"
    # Disable features not easily mocked or not under test
    config_data["logging"]["log_file_path"] = None  # Avoid file IO
    config_data["embeddings"]["enabled"] = False
    config_data["wikipedia_processing"]["enabled"] = False
    config_data["enrichment"]["enabled"] = False
    # Simplify other paths for testing if needed
    tmp_data_dir = tmp_path_factory.mktemp("data_integration")
    config_data["paths"]["data_dir"] = str(tmp_data_dir)
    config_data["paths"]["processing_dir"] = str(tmp_data_dir / "processing")
    config_data["paths"]["output_dir"] = str(tmp_data_dir / "output")
    config_data["paths"]["wikimapper_db"] = str(tmp_data_dir / "dummy_wikimapper.db")

    # Create temp config file
    temp_dir = tmp_path_factory.mktemp("config_integration")
    config_file = temp_dir / "test_config_integration.yaml"
    with open(config_file, "w") as f:
        yaml.dump(config_data, f)

    return str(config_file)


# test_dotenv_path fixture remains the same


@pytest.fixture(scope="session")
def test_dotenv_path(tmp_path_factory):
    """Creates a dummy .env file"""
    env_content = "MONGO_URI=mongodb://mock-host:27017/integration_test_db?serverSelectionTimeoutMS=100\n"
    temp_dir = tmp_path_factory.mktemp("env_integration")
    dotenv_file = temp_dir / ".env_integration_test"
    with open(dotenv_file, "w") as f:
        f.write(env_content)
    return str(dotenv_file)


@pytest.fixture(scope="session")
def test_schema_path(project_root, tmp_path_factory):
    """Creates a minimal valid schema file"""
    schema_content = """
{
  "$jsonSchema": {
    "bsonType": "object",
    "properties": {
      "_id": { "bsonType": "string" }
    }
  }
}
"""
    temp_dir = tmp_path_factory.mktemp("schema_integration")
    schema_file = temp_dir / "test_schema.json"
    with open(schema_file, "w") as f:
        f.write(schema_content)
    return str(schema_file)


@pytest.fixture(scope="session")
def test_class_rules_path(project_root, tmp_path_factory):
    """Creates a minimal class rules file"""
    rules_content = """
preference_order:
  - Q515
"""
    temp_dir = tmp_path_factory.mktemp("rules_integration")
    rules_file = temp_dir / "test_class_rules.yaml"
    with open(rules_file, "w") as f:
        f.write(rules_content)
    return str(rules_file)


# --- Helper to run script's main function ---
# This helper allows testing script logic directly within the test process,
# making mocking effective.


def run_script_main(script_module_name: str, args: List[str]):
    """Imports and runs the main() function of a script module."""
    try:
        # Ensure the script module is loaded/reloaded for the test
        script_module = importlib.import_module(f"scripts.{script_module_name}")
        # Reload might be necessary if mocks changed between tests
        importlib.reload(script_module)

        # Patch sys.argv for the duration of the main call
        original_argv = sys.argv
        sys.argv = [f"scripts/{script_module_name}.py"] + args
        print(f"\nRunning {script_module_name}.main() with args: {sys.argv}")
        try:
            script_module.main()  # Call the script's main function
        except SystemExit as e:
            # Allow SystemExit(0) but fail on non-zero exits
            if e.code != 0:
                pytest.fail(f"{script_module_name}.main() exited with code {e.code}")
            else:
                print(f"{script_module_name}.main() exited with code 0 (Success).")
        finally:
            sys.argv = original_argv  # Restore original argv

    except ImportError:
        pytest.fail(f"Could not import script module: scripts.{script_module_name}")
    except Exception as e:
        pytest.fail(f"Exception calling {script_module_name}.main(): {e}")


# --- Integration Tests (Using Direct Main Calls) ---


@pytest.mark.integration
# Patch get_mongo_client *where it's looked up* (in utils.mongo_helpers)
@patch("utils.mongo_helpers.get_mongo_client")
# Patch get_database *where it's looked up* (in utils.mongo_helpers)
@patch("utils.mongo_helpers.get_database")
# Patch get_entity_collection *where it's looked up* (in utils.mongo_helpers)
@patch("utils.mongo_helpers.get_entity_collection")
# Patch config loading within the test process
@patch("utils.config.load_config")
def test_setup_mongodb_step_direct_call(
    mock_load_config: MagicMock,
    mock_get_collection: MagicMock,  # << Patched get_entity_collection
    mock_get_db: MagicMock,  # << Patched get_database
    mock_get_mongo_client: MagicMock,  # << Patched get_mongo_client
    test_config_path: str,
    test_schema_path: str,
    mock_db,  # Fixture providing clean mock DB handle (still useful for setup/other tests)
):
    """
    Tests if the 'Setup Database' step runs via direct call, patching the problematic
    db.command call by mocking the entire database object returned by get_database.
    """
    # --- Mock Configuration ---
    # Configure the mock load_config to return our test config
    with open(test_config_path, "r") as f:
        test_config_data = yaml.safe_load(f)
    # Add/override the schema path in the config returned by the mock
    test_config_data["mongodb"]["schema_file"] = test_schema_path
    mock_load_config.return_value = test_config_data

    # Configure the mocked get_mongo_client (may not be strictly needed if get_db is fully mocked)
    mock_mongo_client_instance = mongomock.MongoClient()
    mock_get_mongo_client.return_value = mock_mongo_client_instance

    # --- Mock the Database object and its 'command' method ---
    mock_db_object = MagicMock(spec=Database)
    mock_db_object.name = test_config_data["mongodb"]["database"]
    # Configure the 'command' method on this mock_db_object
    mock_db_object.command = MagicMock()
    # Configure the mocked get_database (which setup_mongodb.py imports from utils)
    mock_get_db.return_value = mock_db_object

    # --- Mock the Collection object ---
    # setup_mongodb.py also calls get_entity_collection -> create_indexes
    mock_collection_object = MagicMock(spec=Collection)  # Use pymongo.collection spec
    mock_collection_object.name = test_config_data["mongodb"]["collection_entities"]
    mock_collection_object.index_information.return_value = {
        "_id_": {"key": [("_id", 1)]}
    }  # Mock existing index
    mock_collection_object.create_index = MagicMock()  # Mock index creation
    # Configure the mocked get_entity_collection
    mock_get_collection.return_value = mock_collection_object

    # --- Execute Script ---
    run_script_main("setup_mongodb", ["--config", test_config_path])

    # --- Assertions ---
    # Assert that our mocks for getting DB/Collection handles were called
    mock_get_db.assert_called_once()
    mock_get_collection.assert_called_once()

    # Assert that the mock db.command was called correctly
    mock_db_object.command.assert_called_once()
    command_call_args = mock_db_object.command.call_args[0][0]
    assert "collMod" in command_call_args
    assert (
        command_call_args["collMod"]
        == test_config_data["mongodb"]["collection_entities"]
    )
    assert "$jsonSchema" in command_call_args["validator"]

    # Assert that create_index was called on the mock collection
    assert mock_collection_object.create_index.called


@pytest.mark.integration
# Patch get_mongo_client within the mongo_helpers module
@patch("utils.mongo_helpers.get_mongo_client")
@patch("utils.config.load_config")  # Patch config loading
def test_semantic_corrections_step_direct_call(
    mock_load_config: MagicMock,
    mock_get_mongo_client: MagicMock,  # Function patch
    test_config_path: str,
    test_class_rules_path: str,
    mock_db,  # Fixture providing clean mock DB handle (can be removed if not used for asserts)
):
    """
    Test if the semantic corrections script runs via direct call without errors.
    Patches get_mongo_client to return mongomock instance.
    """
    # Configure mock load_config
    with open(test_config_path, "r") as f:
        test_config_data = yaml.safe_load(f)
    # Ensure class rules path is set correctly in the config
    test_config_data["semantic_correction"] = {
        "class_assignment_rules_file": test_class_rules_path,
        "class_assignment_strategy": "most_specific_known",  # Match default or config
    }
    mock_load_config.return_value = test_config_data

    # Configure the mocked get_mongo_client
    mock_client_instance = mongomock.MongoClient()
    mock_get_mongo_client.return_value = mock_client_instance

    # Pre-create the collection using the mock client instance that the script will receive
    test_db_name = test_config_data["mongodb"]["database"]
    collection_name = test_config_data["mongodb"]["collection_entities"]
    # Ensure collection exists before the script runs
    mock_client_instance[test_db_name].drop_collection(
        collection_name
    )  # Ensure clean start
    mock_client_instance[test_db_name].create_collection(collection_name)
    print(
        f"Pre-created collection '{collection_name}' for corrections test using mocked client."
    )

    # Use the helper to run the script's main
    # When apply_semantic_corrections.main calls get_entity_collection -> get_database -> get_mongo_client,
    # the call to get_mongo_client will return the mongomock client directly.
    run_script_main("apply_semantic_corrections", ["--config", test_config_path])

    # Basic check: script ran without raising unhandled exceptions or non-zero sys.exit
    # More advanced tests would insert data into the mock collection and verify changes.
    assert True  # If run_script_main didn't fail, the basic flow worked


# TODO: Add integration tests for the pipeline runner script itself, potentially
# mocking subprocess.run to check command formation and env passing.
# TODO: Add tests that insert data into mongomock and verify transformations.
