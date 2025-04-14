#!/bin/bash

# Simple script to download required Wikipedia/Wikidata dumps.
# Uses URLs and filenames potentially defined in config/environment.
# Requires wget and sha1sum.

set -e # Exit on error
set -o pipefail # Ensure errors in pipes are caught

# --- Configuration ---
# Get BASE_DIR relative to the script's location
SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
BASE_DIR=$( cd -- "$SCRIPT_DIR/.." &> /dev/null && pwd )

# Config file path can be overridden by environment variable
CONFIG_FILE="${WIKI_CONFIG_FILE:-${BASE_DIR}/config/config.yaml}"

echo "--- Data Download Script ---"
echo "Project Base Dir: ${BASE_DIR}"
echo "Using Config File: ${CONFIG_FILE}"

# --- Function to extract value from YAML (basic grep/sed) ---
# Using yq would be more robust, but avoids adding dependency for this simple script.
get_yaml_value() {
    local key_path=$1
    local default_val=$2
    # Simple grep/sed: handles keys like logging.level or paths.data_dir
    local search_key=$(echo "$key_path" | sed 's/\./\\\./g') # Escape dots for grep
    local value=$(grep -E "^\s*${search_key}:\s*" "$CONFIG_FILE" | sed -E "s/^\s*${search_key}:\s*'?([^']*)'?\s*(#.*)?$/\1/" | head -n 1 | xargs) # Handle potential inline comments and quotes
    # Resolve environment variables within the extracted value (basic ${VAR:-default} support)
    if [[ "$value" =~ \$\{([a-zA-Z_][a-zA-Z0-9_]*)(:-([^}]*))?\} ]]; then
        local var_name="${BASH_REMATCH[1]}"
        local default_part="${BASH_REMATCH[2]}" # Includes ':-' if present
        local yaml_default="${BASH_REMATCH[3]}" # The default value part
        local env_var_value="${!var_name}" # Indirect expansion

        if [[ -n "$env_var_value" ]]; then
            # Env var is set, substitute it into the original string
            value=$(echo "$value" | sed "s|\${\s*${var_name}\s*\(:-\s*[^}]*\s*\)\?}|$env_var_value|g")
        elif [[ -n "$default_part" ]]; then
            # Env var not set, but default exists in YAML, substitute default
            value=$(echo "$value" | sed "s|\${\s*${var_name}\s*:-[^}]*}|$yaml_default|g")
        else
            # Env var not set, no default in YAML, substitute empty string
             value=$(echo "$value" | sed "s|\${\s*${var_name}\s*}||g")
        fi
    fi
    echo "${value:-${default_val}}" # Return resolved value or the script's default
}

# --- Resolve paths and filenames ---
DATA_DIR_REL=$(get_yaml_value "paths.data_dir" "data")
DATA_DIR_ABS="${WIKI_DATA_DIR:-${BASE_DIR}/${DATA_DIR_REL}}"

WIKIDATA_DUMP_FILENAME=$(get_yaml_value "wikidata_ingestion.dump_filename" "latest-all.json.bz2")
WIKIPEDIA_DUMP_FILENAME=$(get_yaml_value "wikipedia_processing.dump_filename" "enwiki-latest-pages-articles-multistream.xml.bz2")

# Determine dump date/prefix for Wikipedia URLs (YYYYMMDD or 'latest')
DUMP_PREFIX=$(basename "$WIKIPEDIA_DUMP_FILENAME" | grep -oE '^[a-z]+wiki-[0-9]{8}|^[a-z]+wiki-latest' | sed 's/^[a-z]*wiki-//')
WIKI_PREFIX="enwiki" # Assuming English Wikipedia

# Wikidata URLs
WIKIDATA_BASE_URL="https://dumps.wikimedia.org/wikidatawiki/entities"
WIKIDATA_DUMP_URL="${WIKIDATA_BASE_URL}/${WIKIDATA_DUMP_FILENAME}"
WIKIDATA_CHECKSUM_URL="${WIKIDATA_DUMP_URL}.sha1"
WIKIDATA_PATH="${DATA_DIR_ABS}/${WIKIDATA_DUMP_FILENAME}"
WIKIDATA_CHECKSUM_PATH="${WIKIDATA_PATH}.sha1"

# Wikipedia URLs
WIKIPEDIA_BASE_URL="https://dumps.wikimedia.org/${WIKI_PREFIX}/${DUMP_PREFIX}"
WIKIPEDIA_DUMP_URL="${WIKIPEDIA_BASE_URL}/${WIKIPEDIA_DUMP_FILENAME}"
WIKIPEDIA_CHECKSUM_FILE="enwiki-${DUMP_PREFIX}-sha1sums.txt"
WIKIPEDIA_CHECKSUM_URL="${WIKIPEDIA_BASE_URL}/${WIKIPEDIA_CHECKSUM_FILE}"
WIKIPEDIA_PATH="${DATA_DIR_ABS}/${WIKIPEDIA_DUMP_FILENAME}"
WIKIPEDIA_CHECKSUM_PATH="${DATA_DIR_ABS}/${WIKIPEDIA_CHECKSUM_FILE}"

# Wikimapper Prereqs (SQL dumps) - Optional
WIKI_PROC_ENABLED=$(get_yaml_value "wikipedia_processing.enabled" "false")
if [[ "$WIKI_PROC_ENABLED" == "true" ]]; then
    echo "Wikipedia processing enabled, checking for Wikimapper SQL prerequisites..."
    PAGE_SQL_FILENAME="${WIKI_PREFIX}-${DUMP_PREFIX}-page.sql.gz"
    REDIRECT_SQL_FILENAME="${WIKI_PREFIX}-${DUMP_PREFIX}-redirect.sql.gz"
    PAGE_PROPS_SQL_FILENAME="${WIKI_PREFIX}-${DUMP_PREFIX}-page_props.sql.gz"

    PAGE_SQL_URL="${WIKIPEDIA_BASE_URL}/${PAGE_SQL_FILENAME}"
    REDIRECT_SQL_URL="${WIKIPEDIA_BASE_URL}/${REDIRECT_SQL_FILENAME}"
    PAGE_PROPS_SQL_URL="${WIKIPEDIA_BASE_URL}/${PAGE_PROPS_SQL_FILENAME}"

    PAGE_SQL_PATH="${DATA_DIR_ABS}/${PAGE_SQL_FILENAME}"
    REDIRECT_SQL_PATH="${DATA_DIR_ABS}/${REDIRECT_SQL_FILENAME}"
    PAGE_PROPS_SQL_PATH="${DATA_DIR_ABS}/${PAGE_PROPS_SQL_FILENAME}"
else
    echo "Wikipedia processing disabled in config, skipping SQL dump downloads."
fi

echo "Data Directory:     ${DATA_DIR_ABS}"

# --- Create Data Directory ---
mkdir -p "$DATA_DIR_ABS"
echo "Ensured data directory exists: ${DATA_DIR_ABS}"

# --- Download Function ---
# Usage: download_file <url> <output_path> [checksum_url] [checksum_path]
download_file() {
    local url=$1
    local path=$2
    local checksum_url=${3:-""}
    local checksum_path=${4:-""}
    local filename=$(basename "$path")

    echo "Checking file: ${filename} ..."

    # Check if file exists and checksum is OK
    if [[ -f "$path" ]]; then
        if [[ -n "$checksum_path" && -f "$checksum_path" ]]; then
            if verify_checksum "$path" "$checksum_path"; then
                echo "  File exists and checksum is valid. Skipping download."
                return 0
            else
                echo "  File exists but checksum FAILED. Redownloading..."
                rm -f "$path" # Remove corrupted file
            fi
        else
             echo "  File exists but no checksum file found/specified. Assuming OK. Skipping download."
             return 0
        fi
    fi

    echo "Downloading ${url} to ${path}..."
    # Use -c to continue downloads, -nv for non-verbose but show progress/errors
    if ! wget -c -nv -O "$path" "$url"; then
        echo "ERROR: Failed to download ${url}"
        rm -f "$path" # Remove partial file on failure
        return 1
    fi

    # Download checksum file if URL provided
    if [[ -n "$checksum_url" && -n "$checksum_path" ]]; then
        echo "Downloading checksum ${checksum_url} to ${checksum_path}..."
        if ! wget -nv -O "$checksum_path" "$checksum_url"; then
             echo "Warning: Failed to download checksum file ${checksum_url}"
             # Don't fail the whole script, just verification won't happen now
        fi
    fi

    # Verify checksum after download if checksum file is available
    if [[ -n "$checksum_path" && -f "$checksum_path" ]]; then
        if ! verify_checksum "$path" "$checksum_path"; then
            echo "ERROR: Checksum verification failed after download for ${filename}!"
            return 1 # Fail if checksum doesn't match after download
        fi
    else
        echo "Warning: Checksum file not available for ${filename}. Cannot verify integrity."
    fi

    echo "Download complete for: ${filename}"
    return 0
}

# --- Checksum Function ---
# Usage: verify_checksum <file_to_check> <checksum_file>
# Returns 0 on success, 1 on failure/skip
verify_checksum() {
    local file_path=$1
    local checksum_file_path=$2
    local expected_checksum=""
    local filename=$(basename "$file_path")

    # Ensure sha1sum is available
    if ! command -v sha1sum &> /dev/null; then
        echo "Warning: 'sha1sum' command not found. Cannot verify checksums."
        return 1
    fi

    if [[ ! -f "$file_path" ]]; then
        echo "Skipping checksum verification: File not found at ${file_path}"
        return 1 # Considered failure for verification purpose
    fi
    if [[ ! -f "$checksum_file_path" ]]; then
        echo "Skipping checksum verification: Checksum file not found at ${checksum_file_path}"
        return 1
    fi

    # Handle different checksum file formats
    if [[ "$checksum_file_path" == *".sha1" ]]; then # Direct sha1 file (Wikidata)
        expected_checksum=$(head -n 1 "$checksum_file_path" | awk '{print $1}')
    elif grep -q " ${filename}$" "$checksum_file_path"; then # Sums file containing multiple entries (Wikipedia) - match end of line
        expected_checksum=$(grep " ${filename}$" "$checksum_file_path" | awk '{print $1}')
    else
        echo "Warning: Could not find entry for '$filename' in checksum file '$checksum_file_path'."
        return 1
    fi

    if [[ -z "$expected_checksum" ]]; then
         echo "Warning: Could not extract expected checksum for '$filename' from '$checksum_file_path'."
         return 1
    fi

    echo "Verifying SHA1 checksum for ${filename}..."
    local actual_checksum=$(sha1sum "$file_path" | awk '{print $1}')

    if [[ "$actual_checksum" == "$expected_checksum" ]]; then
        echo "Checksum PASSED for ${filename}"
        return 0
    else
        echo "ERROR: Checksum FAILED for ${filename}"
        echo "  Expected: ${expected_checksum}"
        echo "  Actual:   ${actual_checksum}"
        return 1
    fi
}

# --- Download Files ---
# Download Wikidata (Dump + Checksum)
download_file "$WIKIDATA_DUMP_URL" "$WIKIDATA_PATH" "$WIKIDATA_CHECKSUM_URL" "$WIKIDATA_CHECKSUM_PATH" || exit 1

# Download Wikipedia XML + SQL (only if enabled)
if [[ "$WIKI_PROC_ENABLED" == "true" ]]; then
    echo "--- Downloading Wikipedia Files ---"
    # Download Checksum file first for subsequent checks
    download_file "$WIKIPEDIA_CHECKSUM_URL" "$WIKIPEDIA_CHECKSUM_PATH"
    # Download main XML dump
    download_file "$WIKIPEDIA_DUMP_URL" "$WIKIPEDIA_PATH" "" "$WIKIPEDIA_CHECKSUM_PATH" || exit 1
    # Download Wikimapper SQL files
    echo "Downloading Wikimapper prerequisite SQL files..."
    download_file "$PAGE_SQL_URL" "$PAGE_SQL_PATH" "" "$WIKIPEDIA_CHECKSUM_PATH" || exit 1
    download_file "$REDIRECT_SQL_URL" "$REDIRECT_SQL_PATH" "" "$WIKIPEDIA_CHECKSUM_PATH" || exit 1
    download_file "$PAGE_PROPS_SQL_URL" "$PAGE_PROPS_SQL_PATH" "" "$WIKIPEDIA_CHECKSUM_PATH" || exit 1
fi

echo "--- Data download script finished successfully. ---"
exit 0
