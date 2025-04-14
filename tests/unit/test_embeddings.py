import pytest
import sys
import os
import numpy as np
import bson
from unittest.mock import patch, MagicMock
from pymongo import UpdateOne
from pymongo.collection import Collection
from pymongo.results import BulkWriteResult

# Adjust path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from scripts.generate_embeddings import (
    _construct_text_for_embedding,
    _convert_to_bson_binary,
    load_embedding_model,
    generate_and_store_embeddings,
)


# --- Fixtures ---
@pytest.fixture
def sample_config() -> dict:
    """Uses batch_size=1 to ensure processing logic is hit"""
    return {
        "embeddings": {
            "enabled": True,
            "model_name": "mock-model",
            "text_fields_to_embed": [
                "english_label",
                "english_description",
                "aliases.en",
            ],
            "storage_method": "mongo_field",
            "embedding_field_name": "embedding_vector",
            "batch_size": 1,  # <<< CHANGED TO 1 >>>
        },
        "mongodb": {"collection_entities": "test_entities"},
    }


@pytest.fixture
def sample_doc_for_embedding():
    return {
        "_id": "Q123",
        "english_label": "Test Entity",
        "english_description": "A description for testing.",
        "aliases": {"en": ["Test E", "TE"], "fr": ["Entité Test"]},
    }


@pytest.fixture
def sample_doc_missing_fields():
    return {
        "_id": "Q456",
        "english_label": "Only Label",
    }


@pytest.fixture
def sample_doc_empty_fields():
    return {
        "_id": "Q789",
        "english_label": "",
        "english_description": " ",
        "aliases": {"en": []},
    }


# --- Unit Tests ---


def test_construct_text_all_fields(sample_doc_for_embedding):
    config_fields = ["english_label", "english_description", "aliases.en"]
    expected_text = "Test Entity. A description for testing.. Test E TE"
    assert (
        _construct_text_for_embedding(sample_doc_for_embedding, config_fields)
        == expected_text
    )


def test_construct_text_missing_fields(sample_doc_missing_fields):
    config_fields = ["english_label", "english_description", "aliases.en"]
    expected_text = "Only Label"
    assert (
        _construct_text_for_embedding(sample_doc_missing_fields, config_fields)
        == expected_text
    )


def test_construct_text_empty_fields(sample_doc_empty_fields):
    config_fields = ["english_label", "english_description", "aliases.en"]
    expected_text = " "
    assert (
        _construct_text_for_embedding(sample_doc_empty_fields, config_fields)
        == expected_text
    )


def test_convert_to_bson_binary():
    embedding_np = np.array([0.1, -0.5, 1.2], dtype=np.float32)
    expected_bytes = embedding_np.astype(np.float32).tobytes()
    bson_binary = _convert_to_bson_binary(embedding_np)
    assert isinstance(bson_binary, bson.Binary)
    assert bson_binary.subtype == 0x00
    assert bytes(bson_binary) == expected_bytes


@patch("scripts.generate_embeddings.SentenceTransformer")
@patch("scripts.generate_embeddings.get_entity_collection")
def test_generate_and_store_embeddings_flow(
    mock_get_collection: MagicMock,
    mock_SentenceTransformer: MagicMock,
    sample_config: dict,
):
    # Setup Mocks
    mock_collection = MagicMock(spec=Collection)
    mock_get_collection.return_value = mock_collection
    mock_cursor = MagicMock()
    sample_docs = [
        {"_id": "Q1", "english_label": "Doc 1", "english_description": "Desc 1"},
        {"_id": "Q2", "english_label": "Doc 2", "aliases": {"en": ["D2"]}},
    ]
    # Ensure find returns the cursor, and the cursor is iterable AND has batch_size method
    mock_cursor.__iter__.return_value = iter(sample_docs)
    # Mock the batch_size method called on the cursor
    mock_cursor.batch_size.return_value = (
        mock_cursor  # Return self to allow chaining if needed
    )
    mock_collection.find.return_value = mock_cursor
    mock_collection.count_documents.return_value = len(sample_docs)

    mock_model_instance = MagicMock()
    dummy_embeddings = np.array([[0.1, 0.2], [0.3, 0.4]], dtype=np.float32)
    # Make encode return embeddings one by one as batch_size is 1
    mock_model_instance.encode.side_effect = [
        np.array([[0.1, 0.2]], dtype=np.float32),
        np.array([[0.3, 0.4]], dtype=np.float32),
    ]
    mock_SentenceTransformer.return_value = mock_model_instance

    # Simulate a successful bulk write result
    mock_bulk_result_dict = {
        "nModified": 0,
        "nUpserted": 0,
        "nMatched": 1,
        "writeErrors": [],
        "upserted": [],
        "writeConcernErrors": [],
    }  # Reflect batch size 1
    mock_bulk_write_result = BulkWriteResult(mock_bulk_result_dict, True)
    mock_collection.bulk_write.return_value = mock_bulk_write_result

    # --- Execute ---
    processed, errors = generate_and_store_embeddings(
        mock_collection, mock_model_instance, sample_config
    )

    # --- Assertions ---
    assert errors == 0
    # Verify find was called correctly
    mock_collection.find.assert_called_once()
    query_arg = mock_collection.find.call_args[0][0]
    assert sample_config["embeddings"]["embedding_field_name"] in query_arg
    assert query_arg[sample_config["embeddings"]["embedding_field_name"]] == {
        "$exists": False
    }

    # Verify encode was called twice (once per doc due to batch_size=1)
    assert mock_model_instance.encode.call_count == len(sample_docs)
    # Check calls individually
    first_call_args = mock_model_instance.encode.call_args_list[0][0][0]
    second_call_args = mock_model_instance.encode.call_args_list[1][0][0]
    assert first_call_args == ["Doc 1. Desc 1"]
    assert second_call_args == ["Doc 2. D2"]

    # Verify bulk_write was called twice (once per doc)
    assert mock_collection.bulk_write.call_count == len(sample_docs)
    # Check first bulk write
    update_ops_sent_1 = mock_collection.bulk_write.call_args_list[0][0][0]
    assert len(update_ops_sent_1) == 1
    assert isinstance(update_ops_sent_1[0], UpdateOne)
    assert update_ops_sent_1[0]._filter == {"_id": "Q1"}
    assert isinstance(
        update_ops_sent_1[0]._doc["$set"]["embedding_vector"], bson.Binary
    )
    # Check second bulk write
    update_ops_sent_2 = mock_collection.bulk_write.call_args_list[1][0][0]
    assert len(update_ops_sent_2) == 1
    assert update_ops_sent_2[0]._filter == {"_id": "Q2"}
    assert isinstance(
        update_ops_sent_2[0]._doc["$set"]["embedding_vector"], bson.Binary
    )

    # Check final processed count reflects successful writes
    assert processed == len(sample_docs)  # Expecting 2 successful writes
