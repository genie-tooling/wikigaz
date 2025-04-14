# Deployment & Operations Guide (Python Application Focus)

**Version:** 1.0
**Date:** 2025-04-14
**Author:** Python Knowledge Integration Architect (PKIA)
**Status:** Recommended Practices

## 1. Introduction

This guide provides instructions and considerations for packaging, deploying, configuring, and operating the Python components of the `wiki2gaz-modern` pipeline in a target environment (e.g., container orchestration platform like Kubernetes, VMs).

## 2. Containerization (Recommended)

Deploying the pipeline as a container image is highly recommended for consistency, portability, and dependency management.

*   **Dockerfile Example:**
    ```dockerfile
    # Use an official Python slim image matching project's version
    FROM python:3.9-slim as builder

    # Set working directory
    WORKDIR /app

    # Install system dependencies (if needed, e.g., for lxml or sqlite)
    # Example for Debian/Ubuntu based images:
    RUN apt-get update && apt-get install -y --no-install-recommends \
        libxml2-dev libxslt1-dev # Example for lxml
        # Add other dependencies like libsqlite3-dev if needed by system packages
    # && rm -rf /var/lib/apt/lists/* # Clean up apt cache

    # Install Poetry
    RUN pip install poetry==1.7.1 # Use a specific version for reproducibility

    # Copy only necessary files for dependency installation
    COPY pyproject.toml poetry.lock ./

    # Install dependencies (without dev dependencies)
    # --no-root is important if package-mode = false
    RUN poetry install --no-dev --no-interaction --no-ansi --no-root

    # --- Runtime Stage ---
    FROM python:3.9-slim

    WORKDIR /app

    # Copy essential system libraries from builder stage if needed
    # RUN apt-get update && apt-get install -y --no-install-recommends <runtime-libs> && rm -rf /var/lib/apt/lists/*

    # Copy installed dependencies from builder stage
    COPY --from=builder /app/.venv /.venv

    # Add venv to path
    ENV PATH="/app/.venv/bin:$PATH"

    # Copy application code (scripts, utils) and config
    COPY scripts/ /app/scripts/
    COPY utils/ /app/utils/
    # Copy config files - alternatively, mount them at runtime
    COPY config/ /app/config/

    # Ensure scripts are executable (if needed)
    RUN chmod +x /app/scripts/*.sh

    # Add a non-root user for security (optional but recommended)
    # RUN useradd --create-home appuser
    # USER appuser

    # Define the default command to run the pipeline
    ENTRYPOINT ["poetry", "run", "python", "scripts/run_pipeline.py"]
    # Default arguments can be added here or via CMD
    # CMD ["--config", "config/config.yaml"]
    ```
*   **Build Command:** `docker build -t wiki2gaz-modern:latest .`
*   **Build Optimization:** The example uses a multi-stage build to keep the final image smaller by excluding Poetry and build-time system dependencies.

## 3. Configuration at Runtime

Configuration should primarily be managed via environment variables when running the container.

*   **Mandatory:**
    *   **`MONGO_URI`**: The connection string for the MongoDB instance. Must be provided.
    *   Example (Kubernetes secret):
        ```yaml
        env:
        - name: MONGO_URI
          valueFrom:
            secretKeyRef:
              name: mongo-secrets
              key: uri
        ```
*   **Optional Overrides:** Any variable referenced as `${VAR}` or `${VAR:-default}` in `config.yaml` can be overridden. Examples:
    *   `LOGGING_LEVEL=DEBUG`
    *   `WIKIDATA_DUMP_FILENAME=wikidata-20240101-all.json.bz2`
    *   `EMBEDDINGS_ENABLED=true`
*   **Configuration Files:**
    *   The `config/` directory is copied into the image by default in the example Dockerfile.
    *   To use external configuration files (e.g., for different environments without rebuilding), mount a volume containing the alternative `config.yaml`, `pipeline_config.json`, etc., into `/app/config/` within the container. Be cautious about maintaining consistency.

## 4. Execution

*   **Running the Pipeline:** Start the container. The `ENTRYPOINT` will typically invoke `scripts/run_pipeline.py`.
    *   Docker Example:
        ```bash
        docker run --rm -it \
          -e MONGO_URI="mongodb://user:pass@host/db" \
          -v /path/to/local/data:/app/data \
          -v /path/to/local/processing:/app/processing_output \
          -v /path/to/local/logs:/app/logs \
          wiki2gaz-modern:latest
        ```
*   **Passing Runner Arguments:** Override the container's command or pass arguments after the image name (depending on `ENTRYPOINT`/`CMD` definition).
    *   Docker Example (running specific steps):
        ```bash
        docker run --rm -it \
          -e MONGO_URI="mongodb://user:pass@host/db" \
          # ... volume mounts ...
          wiki2gaz-modern:latest \
          --run-steps "Ingest Wikidata" "Apply Semantic Corrections"
        ```
*   **Data Volumes:** Essential for persistent data and I/O:
    *   Mount host/cloud storage to `/app/data` (or configured `paths.data_dir`) for input dumps.
    *   Mount host/cloud storage to `/app/processing_output` (or configured `paths.processing_dir`) for intermediate files (e.g., `wiki_stats.db`).
    *   Mount host/cloud storage to `/app/logs` (or configured `logging.log_file_path` directory) if file logging is used.
    *   Ensure the container process has appropriate read/write permissions on mounted volumes.

## 5. Logging

*   **Production Format:** Configure `logging.format: "json"` in `config.yaml`. Structured logs are easier for automated systems to parse.
*   **Output Stream:** Containers should log to `stdout`/`stderr`. The container runtime/orchestrator (Docker, Kubernetes) captures these streams. Avoid relying solely on file logging within the container unless necessary and volumes are properly configured.
*   **Log Aggregation:** Integrate container logs with a centralized logging platform (e.g., ELK stack, Loki/Grafana, Splunk, Datadog) for searching, analysis, and alerting.

## 6. Monitoring (Based on PA Strategy)

*   **Key Metrics (Examples - Needs Refinement based on PA Strategy):**
    *   Pipeline run status (Success/Failure), duration.
    *   Individual step duration, success/failure.
    *   Items/pages processed per second (ingestion, stats processing).
    *   Database operation timings (batch write latency).
    *   Error rates (parsing, validation, write).
    *   Cache hit/miss rates (e.g., P31 prefetch).
    *   Container CPU/Memory utilization.
*   **Implementation Hooks (Requires PA Strategy Finalization):**
    *   **Structured Logs:** Embed metrics directly in JSON logs (e.g., `logger.info("Batch processed", extra={"duration_ms": 1234, "items": 1000})`). Log aggregation tools can extract these.
    *   **Prometheus:** If real-time metrics are needed, consider integrating a Prometheus client. For batch jobs like this pipeline, using the Prometheus Pushgateway might be more suitable than exposing an endpoint. This requires adding the `prometheus-client` library and instrumenting the code to push metrics.

## 7. Scaling

*   **Current State:** The pipeline runs sequentially within a single container.
*   **Vertical Scaling:** Increase CPU and/or Memory allocated to the container. This is the primary scaling method, especially beneficial for memory-intensive steps like `process_wiki_stats.py`. Monitor resource usage to determine appropriate limits/requests.
*   **Horizontal Scaling:**
    *   **Not Directly Applicable to Workflow:** Running multiple instances of the `run_pipeline.py` container won't parallelize the *workflow* itself due to sequential dependencies.
    *   **Internal Parallelization (Potential):** CPU-bound tasks *within* a script could potentially be parallelized using `multiprocessing` (if GIL allows and memory permits). This would require careful implementation and locking.
    *   **External Orchestrator:** Migrating to Airflow/Dagster/etc. would enable true parallel execution of independent tasks if the pipeline were redesigned with more parallelism.
*   **Database Scaling:** Ensure the backend MongoDB instance is adequately provisioned (CPU, RAM, IOPS) to handle the load, especially during bulk write operations. Consider MongoDB scaling options (vertical, sharding via Atlas) if bottlenecks occur.

This guide provides a baseline. Specific deployment details will vary based on the chosen infrastructure (Kubernetes, ECS, VMs, etc.).
