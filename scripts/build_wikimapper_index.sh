#!/bin/bash

# Automates the creation of the Wikimapper index database.
# Requires wikimapper to be installed and the necessary SQL dumps downloaded.

set -e # Exit on error
set -o pipefail

# --- Configuration ---
# Get BASE_DIR relative to the script's location
SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
BASE_DIR=$( cd -- "$SCRIPT_DIR/.." &> /dev/null && pwd )

# Use environment variables for overrides, fallback to config resolution (best effort)
CONFIG_FILE="${WIKI_CONFIG_FILE:-${BASE_DIR}/config/config.yaml}"

# Basic YAML parser function (requires grep/sed) - Improved robustness
get_yaml_value() {
    local key_path=$1
    local default_val=$2
    local config_file_path=$CONFIG_FILE

    if [[ ! -f "$config_file_path" ]]; then
        echo "${default_val}"
        return
    fi

    local search_key_pattern=$(echo "$key_path" | sed 's/\./:/g; s/:/:\s*/g')
    local line=$(grep -E "^\s*${search_key_pattern}\s*" "$config_file_path" | head -n 1)
    local value=$(echo "$line" | sed -E "s/^\s*${search_key_pattern}\s*([^#]*?)\s*(#.*)?$/\1/" | xargs)
    value=$(echo "$value" | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")

    local resolved_value="$value"
    local i=0
    local max_loops=5
    while [[ "$resolved_value" =~ \$\{([a-zA-Z_][a-zA-Z0-9_]*)(:-([^}]*))?\} ]] && [[ $i -lt $max_loops ]]; do
        local var_name="${BASH_REMATCH[1]}"
        local default_part="${BASH_REMATCH[2]}"
        local yaml_default="${BASH_REMATCH[3]}"
        local env_var_value="${!var_name}"

        if [[ -n "$env_var_value" ]]; then
            resolved_value=$(echo "$resolved_value" | sed "s|\${\s*${var_name}\s*\(:-\s*[^}]*\s*\)\?}|$env_var_value|g")
        elif [[ -n "$default_part" ]]; then
            resolved_value=$(echo "$resolved_value" | sed "s|\${\s*${var_name}\s*:-[^}]*}|$yaml_default|g")
        else
             resolved_value=$(echo "$resolved_value" | sed "s|\${\s*${var_name}\s*}||g")
        fi
        i=$((i+1))
    done

    echo "${resolved_value:-${default_val}}"
}

# Resolve paths using config values (relative paths assumed relative to BASE_DIR)
DATA_DIR_REL=$(get_yaml_value "paths.data_dir" "data")
WIKIMAPPER_DB_REL=$(get_yaml_value "paths.wikimapper_db" "data/index_enwiki-latest.db")
WIKI_DUMP_FILENAME=$(get_yaml_value "wikipedia_processing.dump_filename" "enwiki-latest-pages-articles-multistream.xml.bz2")

# Ensure DATA_DIR_ABS is correct and absolute
DATA_DIR_ABS="${WIKI_DATA_DIR:-${BASE_DIR}/${DATA_DIR_REL}}"
if [[ "$DATA_DIR_ABS" != /* ]]; then
  DATA_DIR_ABS="$BASE_DIR/$DATA_DIR_ABS"
fi
DATA_DIR_ABS=$(cd "$DATA_DIR_ABS" && pwd) # Resolve any '..' etc.

# Ensure OUTPUT_DB_PATH_ABS uses BASE_DIR correctly if path is relative and make absolute
OUTPUT_DB_PATH_ABS="${WIKI_WIKIMAPPER_DB:-${BASE_DIR}/${WIKIMAPPER_DB_REL}}"
if [[ "$OUTPUT_DB_PATH_ABS" != /* ]]; then
  OUTPUT_DB_PATH_ABS="$BASE_DIR/$OUTPUT_DB_PATH_ABS"
fi
# Ensure directory for output exists before potentially removing file
mkdir -p "$(dirname "$OUTPUT_DB_PATH_ABS")"


# Determine dump date/prefix from filename
DUMP_PREFIX_GUESS=$(basename "$WIKI_DUMP_FILENAME" | grep -oE '^[a-z]+wiki-[0-9]{8}|^[a-z]+wiki-latest' | sed 's/^[a-z]*wiki-//')
DUMP_PREFIX="${WIKI_DUMP_PREFIX:-${DUMP_PREFIX_GUESS:-latest}}"
WIKI_PREFIX="enwiki" # Assuming English Wikipedia

PAGE_SQL_FILENAME="${WIKI_PREFIX}-${DUMP_PREFIX}-page.sql.gz"
REDIRECT_SQL_FILENAME="${WIKI_PREFIX}-${DUMP_PREFIX}-redirect.sql.gz"
PAGE_PROPS_SQL_FILENAME="${WIKI_PREFIX}-${DUMP_PREFIX}-page_props.sql.gz"

PAGE_SQL="${DATA_DIR_ABS}/${PAGE_SQL_FILENAME}"
REDIRECT_SQL="${DATA_DIR_ABS}/${REDIRECT_SQL_FILENAME}"
PAGE_PROPS_SQL="${DATA_DIR_ABS}/${PAGE_PROPS_SQL_FILENAME}"

echo "--- Wikimapper Index Build Script ---"
echo "Project Base Dir:   ${BASE_DIR}"
echo "Data Directory:     ${DATA_DIR_ABS}"
echo "Output DB Path:     ${OUTPUT_DB_PATH_ABS}"
echo "Dump Name (Prefix): ${WIKI_PREFIX}-${DUMP_PREFIX}"
echo "Page SQL:           ${PAGE_SQL}"
echo "Redirect SQL:       ${REDIRECT_SQL}"
echo "Page Props SQL:     ${PAGE_PROPS_SQL}"

# --- Prerequisite Checks ---
if ! command -v wikimapper &> /dev/null; then
    echo "ERROR: wikimapper command not found. Please install wikimapper (e.g., pip install wikimapper)."
    exit 1
fi

if [[ ! -f "$PAGE_SQL" ]]; then
    echo "ERROR: Page SQL file not found: $PAGE_SQL"
    echo "Please ensure prerequisite SQL dumps are downloaded (e.g., using scripts/download_data.sh)."
    exit 1
fi
if [[ ! -f "$REDIRECT_SQL" ]]; then
    echo "ERROR: Redirect SQL file not found: $REDIRECT_SQL"
    exit 1
fi
if [[ ! -f "$PAGE_PROPS_SQL" ]]; then
    echo "ERROR: Page Props SQL file not found: $PAGE_PROPS_SQL"
    exit 1
fi

# --- Check if DB already exists ---
BUILD_NEEDED=true # Assume we need to build unless skipped
if [[ -f "$OUTPUT_DB_PATH_ABS" ]]; then
    echo "WARNING: Output database already exists: ${OUTPUT_DB_PATH_ABS}"
    if [[ -z "$SKIP_CONFIRM" ]]; then
        read -p "Do you want to overwrite it? (y/N): " confirm
        if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
            echo "Skipping index creation as database already exists."
            BUILD_NEEDED=false
        else
             echo "Removing existing database..."
             rm -f "$OUTPUT_DB_PATH_ABS"
        fi
    else
        echo "SKIP_CONFIRM is set. Overwriting existing database."
        echo "Removing existing database..."
        rm -f "$OUTPUT_DB_PATH_ABS"
    fi
else
    # DB doesn't exist, ensure target directory exists
    mkdir -p "$(dirname "$OUTPUT_DB_PATH_ABS")"
fi


# --- Run Wikimapper Create (Only if needed) ---
if [[ "$BUILD_NEEDED" == "true" ]]; then
    DUMP_NAME="${WIKI_PREFIX}-${DUMP_PREFIX}"
    echo "Starting wikimapper create process for dump '${DUMP_NAME}'..."
    echo "(Looking for SQL files in '${DATA_DIR_ABS}')"
    echo "(Outputting database to '${OUTPUT_DB_PATH_ABS}')"
    echo "(This may take a long time)"

    # Corrected command: Use --dumpdir and --target options
    echo "Running: wikimapper create --dumpdir \"${DATA_DIR_ABS}\" --target \"${OUTPUT_DB_PATH_ABS}\" \"${DUMP_NAME}\""
    if wikimapper create --dumpdir "$DATA_DIR_ABS" --target "$OUTPUT_DB_PATH_ABS" "$DUMP_NAME"; then
        echo "wikimapper create command finished successfully."
    else
        echo "ERROR: wikimapper create command failed."
        exit 1
    fi

    # --- Verification after Creation ---
    if [[ ! -f "$OUTPUT_DB_PATH_ABS" ]]; then
        echo "ERROR: Wikimapper index creation failed. Output file not found: ${OUTPUT_DB_PATH_ABS}"
        exit 1
    fi
    echo "Wikimapper index database created successfully: ${OUTPUT_DB_PATH_ABS}"
    echo "Index size: $(du -sh "$OUTPUT_DB_PATH_ABS" | cut -f1)"
else
    # DB existed and overwrite was not confirmed
    echo "Using existing database file: ${OUTPUT_DB_PATH_ABS}"
fi


# --- Verify Schema (Basic Check) - CORRECTED ---
echo "Verifying basic table structure..."
# *** CORRECTED Expected Column Names ***
EXPECTED_TABLE="mapping"
EXPECTED_COLS=("wikipedia_title" "wikidata_id")
# *** END CORRECTION ***

DB_SCHEMA_OK=true
if ! sqlite3 "$OUTPUT_DB_PATH_ABS" ".table" | grep -qw "$EXPECTED_TABLE"; then
    echo "ERROR: Expected table '$EXPECTED_TABLE' not found in the database."
    DB_SCHEMA_OK=false
else
    echo "Table '$EXPECTED_TABLE' found."
    # Use PRAGMA table_info for more reliable column checking
    TABLE_INFO=$(sqlite3 "$OUTPUT_DB_PATH_ABS" "PRAGMA table_info($EXPECTED_TABLE);")
    for COL in "${EXPECTED_COLS[@]}"; do
        if ! echo "$TABLE_INFO" | cut -d'|' -f2 | grep -qw "$COL"; then
            echo "ERROR: Expected column '$COL' not found in table '$EXPECTED_TABLE'."
            DB_SCHEMA_OK=false
        else
             echo "Column '$COL' found."
        fi
    done
    if [[ "$DB_SCHEMA_OK" == "false" ]]; then
        echo "Found Columns Info:"
        echo "$TABLE_INFO"
    fi
fi

if [[ "$DB_SCHEMA_OK" == "true" ]]; then
    echo "Basic database schema matches expectations for utils/mapping.py (after update)."
else
    echo "ERROR: Database schema verification failed. Check the output above. The mapping utility might need updating."
    exit 1 # Exit with error if schema is wrong
fi


echo "--- Wikimapper Index Build Script Finished ---"
exit 0
