# PKIA Deliverable: Embedding Model & Strategy Definition

**Date:** 2025-04-14
**Author:** Python Knowledge Integration Architect (PKIA)
**Status:** Final Draft

## 1. Introduction

This document outlines the strategy for generating and storing semantic vector embeddings for entities within the MongoDB collection, intended primarily for downstream Retrieval-Augmented Generation (RAG) applications. This strategy is contingent on the `embeddings.enabled` flag being set to `true` in `config/config.yaml`.

## 2. Embedding Model Selection

*   **Selected Model:** `all-MiniLM-L6-v2` (from `sentence-transformers` library)
*   **Rationale:**
    *   **Performance:** Offers a good balance between computational efficiency (suitable for batch processing large datasets) and embedding quality for general-purpose semantic similarity tasks.
    *   **Maturity & Support:** Widely used, well-documented, and readily available via the popular `sentence-transformers` library.
    *   **Dimensionality:** Produces 384-dimensional vectors, which is manageable for storage and initial experimentation.
*   **Alternative Considerations (Deferred):** If higher accuracy is required and computational resources allow, models like `all-mpnet-base-v2` (768 dimensions) or larger multilingual models could be evaluated later. Task-specific fine-tuning is out of scope for this phase.
*   **Implementation:** The `sentence-transformers` library should be added to `pyproject.toml`. The `scripts/generate_embeddings.py` script will use this library to load the model.

## 3. Text Fields for Embedding

*   **Input Text Construction:** To capture the core semantic identity of an entity, the input text for the embedding model will be constructed by concatenating the following fields from the MongoDB document, separated by a period and space (`. `):
    1.  `english_label`
    2.  `english_description` (if available)
    3.  Joined English aliases (`aliases.en`, joined by spaces, if available)
*   **Rationale:** This combination provides the primary name, a contextual description, and common alternative names, offering a richer semantic representation than just the label alone.
*   **Implementation:** The `scripts/generate_embeddings.py` script must query these fields, handle potential missing values gracefully, and concatenate them in the specified order before passing the combined string to the `model.encode()` method. The `embeddings.text_fields_to_embed` key in `config.yaml` should reflect this (`["english_label", "english_description", "aliases.en"]`), although the script logic will perform the specific concatenation.

## 4. Storage Strategy

*   **Selected Strategy:** `mongo_field`
*   **Rationale:**
    *   **Simplicity:** Storing the embedding vector directly within the main entity document is the simplest approach for initial implementation and keeps entity data self-contained.
    *   **Current Scope:** Suitable for scenarios where embeddings are retrieved alongside other entity data or where downstream processes perform batch exports.
*   **Implementation:**
    *   The generated embedding vector (a NumPy array) will be stored in the MongoDB field specified by `embeddings.embedding_field_name` (default: `embedding_vector`).
    *   **Data Type:** To optimize storage space and ensure compatibility, the vector should be stored as **BSON Binary data (subtype 0x00)**. The `scripts/generate_embeddings.py` script must convert the NumPy array (ensure it's `float32`) to bytes (`tobytes()`) and wrap it using `bson.Binary(..., subtype=0x00)`. The schema (`mongo_schema_v1.json`) allows `binData` for this field.
    *   **Indexing:** Standard MongoDB indexing on array/binary fields is generally *not* efficient for high-dimensional vector similarity search. A basic index on this field is **not** recommended.
*   **Alternative Considerations (Deferred):**
    *   **`mongo_collection`:** Storing embeddings in a separate collection (mapping `_id` to `embedding_vector`) can reduce the size of the main entity document but requires joins or separate lookups. Consider if main document size becomes problematic.
    *   **`vector_db` / Atlas Vector Search:** For efficient similarity search (e.g., k-NN) required by many RAG applications, a dedicated vector database or MongoDB Atlas Vector Search is the recommended production solution. Migrating to Atlas Vector Search would involve creating a specific index type and using the `$vectorSearch` aggregation pipeline stage. This is the recommended upgrade path if performant vector search becomes a requirement.

## 5. Implementation Script (`scripts/generate_embeddings.py`)

*   The script should implement the logic described above:
    *   Load the specified `sentence-transformers` model.
    *   Query MongoDB for documents missing the `embedding_vector` field, fetching required text fields.
    *   Process documents in batches (`embeddings.batch_size`).
    *   Construct the input text string for each document.
    *   Generate embeddings using `model.encode()`.
    *   Convert embeddings to `bson.Binary` (subtype 0x00, from `float32` bytes).
    *   Use `bulk_write` with `UpdateOne` to store the binary embedding in the target field.
    *   Include robust error handling and logging.

