# Data Modeling Rationale

**Version:** 1.0
**Date:** 2025-04-14
**Author:** Knowledge Graph Architect & Modernization Strategist

## 1. Introduction

This document explains the rationale behind choosing the MongoDB document model, as defined in `config/mongo_schema_v1.json`, for storing the integrated Wikidata and Wikipedia data in the `wiki2gaz-modern` pipeline. It outlines the challenges, alternatives considered, and justifications for the chosen approach.

## 2. Core Challenge: Representing Interconnected Knowledge

Wikipedia and Wikidata represent vast, interconnected graphs of information. Key relationships include:
*   Wikidata items linked via properties (PIDs).
*   Wikidata items linked to Wikipedia pages (sitelinks).
*   Wikipedia pages linked internally (wikilinks).
*   Hierarchical relationships (P131 administrative levels, P279 subclass of).

The primary challenge is selecting a data model and storage technology that effectively represents these entities and relationships for the project's main goal: creating a rich gazetteer suitable for downstream tasks like Retrieval-Augmented Generation (RAG), while balancing performance, scalability, and development effort.

## 3. Storage Alternatives Considered

Several database paradigms were implicitly or explicitly considered:

*   **Relational Databases (e.g., PostgreSQL, MySQL):**
    *   *Pros:* ACID compliance, mature tooling, powerful SQL queries, JOIN capabilities.
    *   *Cons:* Representing graph structures often requires complex schemas (e.g., multiple join tables, adjacency lists) or compromises (EAV models), leading to potential impedance mismatch and complex queries for graph traversal. Schema evolution can be cumbersome. Less natural fit for semi-structured JSON-like data from Wikidata.
*   **Native Graph Databases (e.g., Neo4j, Neptune, ArangoDB):**
    *   *Pros:* Designed specifically for graph data. Native representation of nodes and relationships. Efficient graph traversal queries (Cypher, Gremlin, SPARQL). Natural fit for Wiki data relationships.
    *   *Cons:* Can introduce different query languages and operational complexities compared to more common databases. Tooling and ecosystem might be less mature in some areas. Potential learning curve for the team. Performance characteristics can vary significantly depending on the query type.
*   **Search Engines (e.g., Elasticsearch, OpenSearch):**
    *   *Pros:* Excellent for full-text search, relevance ranking, and aggregations. Can handle semi-structured data well.
    *   *Cons:* Primarily designed as a secondary index, not typically the primary system of record. Eventual consistency model. Less suited for enforcing complex schemas or transactional integrity. Graph traversal capabilities are limited compared to native graph DBs.
*   **Document Databases (e.g., MongoDB):**
    *   *Pros:* Flexible schema (adapted via validation rules), natural mapping for JSON-like Wikidata structures, good performance for retrieving entire entity documents by ID, supports rich data types (arrays, nested objects, GeoJSON), good Python driver (`pymongo`), mature ecosystem. Can represent *some* relationships via embedding or referencing.
    *   *Cons:* Complex JOINs or graph traversals across documents are less efficient than in relational or graph databases. Requires careful schema design to balance embedding vs. referencing for performance and data consistency.

## 4. Chosen Model: MongoDB Document Database

The decision was made to use MongoDB as the primary data store, employing a document-centric model defined by `config/mongo_schema_v1.json`.

**Rationale:**

1.  **Primary Use Case (RAG Gazetteer):** The main downstream goal involves retrieving rich information about a *specific entity* (identified potentially by QID or lookup). MongoDB excels at fetching entire documents quickly by their `_id` (which we set to the QID). The document can contain most necessary information (labels, descriptions, aliases, types, coordinates, key properties, Wikipedia signals) needed for augmenting prompts or providing context.
2.  **Data Structure Alignment:** Wikidata's item structure maps relatively well to a JSON document model. Storing multi-valued properties (aliases, instance_of, population records) as arrays and related simple data (dates) as nested objects feels natural.
3.  **Flexibility with Validation:** While flexible, MongoDB's `$jsonSchema` support allows us to enforce a consistent structure and data types (`scripts/setup_mongodb.py`), providing many benefits of a schema without the rigidity of relational models, crucial for handling evolving Wikidata structures.
4.  **GeoJSON Support:** Native support for GeoJSON and `2dsphere` indexing is highly beneficial for geographic entities, a primary focus.
5.  **Performance for Core Operations:** Ingestion involves bulk replacing/upserting documents by QID, which MongoDB handles efficiently. Enrichment and correction steps primarily involve finding documents and updating fields within them, also well-supported.
6.  **Developer Experience:** `pymongo` is a mature and widely used Python driver. Document structure is generally intuitive for developers familiar with JSON/dictionaries.
7.  **Scalability:** MongoDB offers well-understood horizontal scaling capabilities (sharding) if the dataset grows beyond a single node's capacity.
8.  **Simplified Hierarchy Storage:** The P131 hierarchy, while graph-like, is stored as an ordered embedded array. This is sufficient for displaying or simple filtering based on parent regions and avoids complex graph queries for this specific relationship, optimized by batch prefetching during ingestion.

## 5. Schema Design Principles within MongoDB

Given the choice of MongoDB, the schema design follows these principles:

*   **Embed Frequently Accessed Data:** Labels, descriptions, aliases, coordinates, instance_of, and core properties are embedded directly within the entity document for fast retrieval.
*   **Arrays for Multi-Valued Properties:** Properties like `instance_of`, `population`, `country_membership` are naturally stored as arrays of values or objects.
*   **Specific Handling for Key Relationships:** The `admin_hierarchy` is embedded as an ordered array, representing the pre-calculated traversal path. Wikipedia links/mentions are embedded in the `wiki_links` sub-document.
*   **Use Appropriate BSON Types:** Leveraging `Date`, `Double`, `Int64` (Long), `Boolean`, and `BinData` (for embeddings) where appropriate.
*   **Prioritize Read Performance for RAG:** The structure is optimized for retrieving a comprehensive entity profile in a single query.
*   **Indexing:** Key fields used for filtering or sorting are indexed (`_id`, `coordinates`, `corrected_class`, `wiki_links.normalized_enwiki_title`, `admin_hierarchy.qid`, etc.) - see `scripts/setup_mongodb.py`.

## 6. Accepted Trade-offs

*   **Complex Graph Queries:** Performing arbitrary, multi-hop graph traversals (e.g., finding all entities related through a chain of specific PIDs) is less efficient than in a native graph database and would require application-level logic or complex aggregation framework queries.
*   **Data Duplication:** Storing labels within the `admin_hierarchy` introduces some data duplication, but simplifies retrieval compared to requiring lookups for parent labels.
*   **Large Documents:** Entities with extremely rich data (many aliases, complex histories, extensive properties) could result in large documents, potentially impacting performance. This is monitored but considered manageable for the target entities.

## 7. Future Considerations

*   **Vector Search:** If performant nearest-neighbor search on `embedding_vector` becomes critical, migrating this aspect to MongoDB Atlas Vector Search (using a dedicated search index) is the recommended path, rather than relying on standard MongoDB indexing.
*   **Graph Analytics:** If complex graph analytics become a primary requirement beyond the RAG gazetteer use case, exporting data to or integrating with a dedicated graph database could be considered.

This MongoDB document model provides a pragmatic balance, effectively supporting the primary RAG gazetteer goal while leveraging MongoDB's strengths in handling semi-structured data and providing good performance for core pipeline operations.
