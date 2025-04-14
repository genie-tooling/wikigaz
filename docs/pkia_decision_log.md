# Technical Decision Log

**Version:** 1.1 (Updated 2025-04-14)
**Status:** Active (Requires Ongoing Updates)

## 1. Introduction

This document records significant technical decisions made during the design and implementation of the `wiki2gaz-modern` pipeline, including the rationale and alternatives considered.

---

## Log Entries

**(Format: YYYY-MM-DD - Decision Title)**

*   **Date:** 2025-03-XX (Approx)
*   **Decision:** Use Streaming Parsers for Dumps (`ijson`, `lxml.etree.iterparse`)
*   **Problem:** Handling multi-terabyte Wikidata JSON and Wikipedia XML dumps within limited RAM environment. Legacy batch/in-memory approaches failed.
*   **Alternatives:** Loading full files (Infeasible memory-wise), Chunking files (Complex, potential boundary issues).
*   **Rationale:** Streaming parsers process data iteratively, keeping memory usage low and constant regardless of file size. `ijson` is efficient for JSON, `lxml`/`etree` `iterparse` with `elem.clear()` is standard for large XML.
*   **Consequences:** Requires careful implementation to manage parser state and ensure resources are freed (esp. XML).

*   **Date:** 2025-03-XX (Approx)
*   **Decision:** Adopt Canonical Wikipedia Title Normalization Function (`utils.helpers.normalize_wikipedia_title`)
*   **Problem:** Inconsistent joining between Wikipedia links and Wikidata sitelinks due to variations in title formatting (spaces, underscores, encoding, case, fragments). Legacy system likely had simpler, insufficient normalization.
*   **Alternatives:** Ad-hoc normalization per script (Inconsistent), Using raw titles (Non-functional joins).
*   **Rationale:** A single, consistent normalization function applied everywhere ensures reliable key matching for joining/mapping data from different sources. Defined steps based on common MediaWiki/URL patterns.
*   **Consequences:** All code dealing with Wikipedia titles *must* use this function for lookups or comparisons.

*   **Date:** 2025-04-XX (Approx)
*   **Decision:** Use Wikimapper SQLite DB for Title->QID Mapping
*   **Problem:** Need efficient lookup of Wikidata QID based on a (normalized) Wikipedia title during Wikipedia stats processing. Querying live APIs is too slow for millions of links. In-memory map is too large.
*   **Alternatives:** Build custom lookup service (Reinventing wheel), Query Wikidata API/SPARQL (Too slow), Use alternative mapping DBs (Wikimapper is standard).
*   **Rationale:** Wikimapper pre-builds an indexed SQLite database optimized for this specific lookup task, leveraging Wikimedia SQL dumps. `utils.mapping.WikimapperLookup` provides a clean interface.
*   **Consequences:** Requires building the Wikimapper DB as a prerequisite step (`build_wikimapper_index.sh`). Build time can be long. DB must be kept reasonably synchronized with Wikipedia XML dump version.

*   **Date:** 2025-04-XX (Approx)
*   **Decision:** Use MongoDB Document Store as Primary Storage
*   **Problem:** Need a scalable and flexible storage solution for semi-structured Wikidata/Wikipedia integrated data, suitable for RAG use cases (entity retrieval).
*   **Alternatives:** Relational DB (Schema complexity for graph), Graph DB (Operational overhead/learning curve), Search Index (Not primary store).
*   **Rationale:** MongoDB provides good balance: flexible schema (w/ validation), good performance for document retrieval by QID, native GeoJSON, mature Python driver. Maps well to Wikidata structure and RAG retrieval patterns.
*   **Consequences:** Complex graph queries are less efficient. Requires careful schema design. See `docs/data_modeling_rationale.md`.

*   **Date:** 2025-04-XX (Approx)
*   **Decision:** Implement P131 Hierarchy Batch Prefetching
*   **Problem:** Naive recursive P131 traversal would cause excessive individual database lookups per entity, leading to poor performance during Wikidata ingestion.
*   **Alternatives:** No hierarchy, Store only direct parent, Recursive single lookups (Too slow).
*   **Rationale:** Prefetching minimal data (label, P131 links) for *all* required hierarchy QIDs within a batch significantly reduces DB load. Hierarchy is then built using an in-memory cache (`_batch_item_cache`) for that batch.
*   **Consequences:** Assumes parent entities exist or will be ingested into the same collection. Increases complexity slightly within the ingestion batch processing logic (`ingest_wikidata_mongo.py` populates cache, `wikidata_helpers.py` reads from it).

*   **Date:** 2025-04-XX (Approx)
*   **Decision:** Separate Wikidata Aliases and Wikipedia Link Mentions
*   **Problem:** Legacy system might have conflated these concepts. Wikidata aliases represent names for the entity concept itself, while Wikipedia mentions represent how it's linked *to* within Wikipedia context.
*   **Alternatives:** Combine them (Loses semantic distinction), Store only one type (Incomplete).
*   **Rationale:** Storing separately in distinct schema fields (`aliases` vs. `wiki_links.top_mentions`) provides clearer, more accurate data for downstream use. See `docs/mongo_schema_v1_documentation.md`.
*   **Consequences:** Requires separate processing steps (Wikidata ingestion for aliases, Wikipedia processing for mentions).

*   **Date:** 2025-04-XX (Approx)
*   **Decision:** Use Custom Python Script for Pipeline Orchestration (`run_pipeline.py`) with `graphlib`
*   **Problem:** Need to execute multiple Python/Shell scripts in sequence, managing dependencies and parameters reliably.
*   **Alternatives:** Simple shell script (Poor dependency/error handling), Airflow/Dagster/Prefect (More powerful but significantly higher setup/operational overhead for current scope).
*   **Rationale:** Custom Python script enhanced with `graphlib` for topological sort provides sufficient control, dependency management, and error handling for the current linear pipeline complexity. Easier integration with project environment (Poetry) and configuration (`pipeline_config.json`).
*   **Consequences:** Lacks features of full orchestrators (UI, complex scheduling, dynamic workflows). Can be revisited if pipeline complexity grows significantly. See `docs/pkia_workflow_enhancements.md`.

*   **Date:** 2025-04-XX (Approx)
*   **Decision:** Store Embeddings as BSON Binary in MongoDB Field
*   **Problem:** Need to store generated vector embeddings associated with entities.
*   **Alternatives:** Store as array of floats (Less space efficient), Separate Mongo collection (Requires joins), Dedicated Vector DB (More complex setup initially), Atlas Vector Search (Future enhancement).
*   **Rationale:** Storing as BSON Binary (subtype 0x00) directly in the entity document is simple, space-efficient, and keeps data together. Sufficient for initial RAG use cases where vectors are retrieved with the document. See `docs/pkia_embedding_strategy.md`.
*   **Consequences:** Not suitable for efficient similarity search via standard MongoDB indexes. Requires Atlas Vector Search or export to a dedicated vector DB for performant k-NN search. Requires `bson` library for conversion.

*   **Date:** 2025-04-XX (Approx)
*   **Decision:** Aggregate Wikipedia Stats In-Memory (`process_wiki_stats.py`)
*   **Problem:** Need to count millions/billions of links targeting potentially millions of unique QIDs.
*   **Alternatives:** Disk-based aggregation (e.g., external sort/merge - complex to implement), Database aggregation (e.g., inserting every link into temp SQL table - potentially very slow writes/large DB).
*   **Rationale:** In-memory aggregation using `defaultdict`/`Counter` is significantly faster if sufficient RAM is available. This was chosen as the initial approach for performance.
*   **Consequences:** **Requires memory usage verification (PI Task).** If memory limits are exceeded on target environment/data size, this component will need refactoring to use a disk-based or DB-based aggregation strategy. See `docs/design_considerations.md`.

*   **Date:** 2025-04-XX (Approx)
*   **Decision:** Use SQLite for Intermediate Stats Storage/Lookup
*   **Problem:** Need efficient QID-based lookup for aggregated Wikipedia stats during the enrichment phase, without loading multi-GB JSON files into memory.
*   **Alternatives:** Keep stats in memory (Infeasible), Use MongoDB collection (Higher overhead for temporary data, potential contention), Flat files (Inefficient lookup).
*   **Rationale:** SQLite provides a file-based, indexed, relational store. Creating/querying an SQLite DB (either directly by `process_wiki_stats.py` or temporarily by `enrich_mongo_with_wiki.py` from JSONL) allows efficient key-based lookups (`SELECT count FROM ... WHERE qid = ?`) with low memory overhead during enrichment.
*   **Consequences:** Requires `sqlite3` module. Adds overhead of creating the DB if input is JSONL. Introduces file system I/O dependency.

*   **Date:** 2025-04-XX (Approx)
*   **Decision:** Disable P(E|M) Calculation
*   **Problem:** Legacy system's probability calculation was deemed potentially misleading or unnecessary for the current RAG use case.
*   **Alternatives:** Re-implement/fix the calculation (Complex, unclear benefit), Keep legacy calculation (Propagates potential issues).
*   **Rationale:** Simplifies the pipeline and avoids potentially confusing downstream applications. Focus is on providing clean entity data and signals (aliases, link counts, mentions).
*   **Consequences:** Downstream applications needing entity-mention probabilities must calculate them separately based on the provided data (e.g., mention counts).

*   **Date:** 2025-04-XX (Approx)
*   **Decision:** Use `all-MiniLM-L6-v2` for Embeddings
*   **Problem:** Need a suitable pre-trained model for generating general-purpose semantic embeddings for RAG.
*   **Alternatives:** Larger models (`all-mpnet-base-v2`), multilingual models, custom fine-tuning.
*   **Rationale:** `all-MiniLM-L6-v2` offers a good balance of speed, quality, and manageable dimensionality (384) for initial implementation and batch processing. Standard model in `sentence-transformers`. See `docs/pkia_embedding_strategy.md`.
*   **Consequences:** Embedding quality might be lower than larger models. May need re-evaluation if RAG performance is insufficient.

---
*(Add new decisions chronologically)*
