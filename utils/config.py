import os
import yaml
import logging
import re
from dotenv import load_dotenv
from typing import Dict, Any, Optional, Union, Set
from pydash import get as pydash_get  # Using pydash for safe nested access

logger = logging.getLogger(__name__)


# Custom Exception
class ConfigurationError(Exception):
    """Custom exception for configuration related errors."""

    pass


# --- Global cache for loaded configuration ---
_config: Optional[Dict[str, Any]] = None
_base_dir: Optional[str] = None  # Store project base directory

# Regex to find ${VAR_NAME} or ${VAR_NAME:-default} patterns
ENV_VAR_PATTERN = re.compile(r"\$\{\s*(\w+)\s*(?::-([^}]*))?\s*\}")
# Regex to find simple ${config.path.to.key} patterns
CONFIG_REF_PATTERN = re.compile(r"\$\{config\.([\w.]+)\}")


def get_base_dir() -> str:
    """Determines and returns the project base directory."""
    global _base_dir
    if _base_dir is None:
        # Assumes this script (utils/config.py) is one level down from project root
        _base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return _base_dir


def resolve_value(
    value: Any, config_dict: Dict[str, Any], visited_refs: Optional[Set[str]] = None
) -> Any:
    """
    Recursively resolve environment variables and config references in values.
    Handles nested resolution and basic cycle detection for config references.
    Preserves the type of the resolved value where possible.
    """
    if visited_refs is None:
        visited_refs = set()

    if isinstance(value, str):
        # --- Part 1: Resolve environment variables first ---
        resolved_env_value = value
        env_substituted = True
        while env_substituted:
            env_substituted = False
            next_val = ""
            last_end = 0
            for match in ENV_VAR_PATTERN.finditer(resolved_env_value):
                var_name = match.group(1)
                default_value_str = match.group(2)
                default_value = (
                    default_value_str.strip() if default_value_str is not None else None
                )

                env_val = os.environ.get(var_name)

                replacement = None
                if env_val is not None:
                    replacement = resolve_value(
                        env_val, config_dict, visited_refs.copy()
                    )
                elif default_value is not None:
                    replacement = resolve_value(
                        default_value, config_dict, visited_refs.copy()
                    )
                else:
                    logger.warning(
                        f"Environment variable '{var_name}' not set and no default provided. Resolving to empty string."
                    )
                    replacement = ""

                next_val += resolved_env_value[last_end : match.start()]
                if (
                    isinstance(replacement, (str, int, float, bool))
                    or replacement is None
                ):
                    next_val += str(replacement)
                else:
                    logger.warning(
                        f"Cannot substitute complex type {type(replacement)} from env var '{var_name}' into string '{resolved_env_value}'. Using string representation."
                    )
                    next_val += str(replacement)

                last_end = match.end()
                env_substituted = True

            next_val += resolved_env_value[last_end:]
            if not env_substituted:
                break
            resolved_env_value = next_val

        # --- Part 2: Resolve configuration references ---
        current_value = resolved_env_value
        config_substituted = True
        while config_substituted:
            config_substituted = False
            next_val_parts = []
            last_end = 0

            for match in CONFIG_REF_PATTERN.finditer(current_value):
                config_path = match.group(1)
                ref_str = f"config.{config_path}"

                if ref_str in visited_refs:
                    path_list = list(visited_refs)
                    cycle_str = " -> ".join(path_list + [ref_str])
                    logger.error(
                        f"Circular configuration reference detected: {cycle_str}"
                    )
                    raise ValueError(
                        f"Circular configuration reference detected involving '${{config.{config_path}}}'"
                    )

                next_val_parts.append(current_value[last_end : match.start()])

                config_val = pydash_get(config_dict, config_path)

                if config_val is not None:
                    new_visited = visited_refs.copy()
                    new_visited.add(ref_str)
                    try:
                        resolved_part = resolve_value(
                            config_val, config_dict, new_visited
                        )

                        if match.start() == 0 and match.end() == len(current_value):
                            return resolved_part  # Return actual object type

                        next_val_parts.append(str(resolved_part))
                        config_substituted = True
                    except ValueError as e:
                        logger.error(
                            f"Circular reference detected while resolving '${{config.{config_path}}}': {e}"
                        )
                        raise
                else:
                    logger.warning(
                        f"Configuration reference '${{config.{config_path}}}' not found. Leaving unresolved."
                    )
                    next_val_parts.append(match.group(0))

                last_end = match.end()

            next_val_parts.append(current_value[last_end:])
            next_value_str = "".join(next_val_parts)

            if not config_substituted or next_value_str == current_value:
                break
            current_value = next_value_str

        return current_value

    elif isinstance(value, dict):
        return {
            k: resolve_value(v, config_dict, visited_refs.copy())
            for k, v in value.items()
        }
    elif isinstance(value, list):
        return [resolve_value(item, config_dict, visited_refs.copy()) for item in value]
    else:
        return value


def _absolutize_paths(config: Dict[str, Any], base_directory: str):
    """Makes configured paths absolute relative to the base directory."""
    # Define sections and keys containing paths expected to be relative
    path_keys = {
        "paths": ["data_dir", "processing_dir", "output_dir", "wikimapper_db"],
        "logging": ["log_file_path"],
        "semantic_correction": ["class_assignment_rules_file"],
        "wikipedia_processing": ["stats_output_location"],
        "wikidata_ingestion": ["complex_path"],  # Path added here previously
    }

    for section, keys in path_keys.items():
        section_dict = config.get(section)
        if isinstance(section_dict, dict):
            for key in keys:
                path_value = section_dict.get(key)
                if (
                    isinstance(path_value, str)
                    and path_value
                    and not os.path.isabs(path_value)
                ):
                    if "${" in path_value:
                        logger.warning(
                            f"Path '{key}' in section '{section}' ('{path_value}') still contains variables before absolutizing. Ensure resolution order is correct."
                        )
                        continue
                    try:
                        abs_path = os.path.abspath(
                            os.path.join(base_directory, path_value)
                        )
                        section_dict[key] = abs_path
                        logger.debug(
                            f"Resolved relative path '{key}' in section '{section}' to absolute: {abs_path}"
                        )
                    except Exception as e:
                        logger.error(
                            f"Error making path absolute for {section}.{key}='{path_value}': {e}"
                        )

    # Ensure log directory exists after absolutizing path
    log_file_path = pydash_get(config, "logging.log_file_path")
    if log_file_path and isinstance(log_file_path, str):
        log_dir = os.path.dirname(log_file_path)
        if log_dir:
            try:
                os.makedirs(log_dir, exist_ok=True)
                logger.debug(f"Ensured log directory exists: {log_dir}")
            except OSError as e:
                logger.error(f"Failed to create log directory {log_dir}: {e}")


def load_config(
    config_path: str = "config/config.yaml",
    dotenv_path: Optional[str] = None,
    force_reload: bool = False,
    base_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Loads configuration from .env and a YAML file, resolving variables and references.
    """
    global _config, _base_dir
    if _config is not None and not force_reload:
        return _config

    if _base_dir is None or force_reload:
        _base_dir = base_dir or get_base_dir()

    effective_dotenv_path = dotenv_path
    if effective_dotenv_path and not os.path.isabs(effective_dotenv_path):
        effective_dotenv_path = os.path.join(_base_dir, effective_dotenv_path)
    try:
        loaded_env = load_dotenv(
            dotenv_path=effective_dotenv_path, override=False, verbose=True
        )
        if loaded_env:
            logger.info(
                f"Loaded environment variables from: {effective_dotenv_path or 'auto-detected .env'}"
            )
        else:
            logger.info("No .env file found or specified file was empty.")
    except Exception as e:
        logger.warning(f"Error loading .env file: {e}. Proceeding without .env.")

    abs_config_path = config_path
    if not os.path.isabs(abs_config_path):
        abs_config_path = os.path.join(_base_dir, abs_config_path)

    if not os.path.exists(abs_config_path):
        logger.critical(f"Configuration file not found at: {abs_config_path}")
        raise ConfigurationError(f"Configuration file not found: {abs_config_path}")

    try:
        with open(abs_config_path, "r", encoding="utf-8") as f:
            raw_config = yaml.safe_load(f)
        if raw_config is None:
            raw_config = {}
            logger.warning(f"Configuration file {abs_config_path} is empty.")
        if not isinstance(raw_config, dict):
            raise ConfigurationError(
                f"Configuration file {abs_config_path} did not parse into a dictionary."
            )
    except yaml.YAMLError as e:
        logger.critical(f"Error parsing YAML configuration file {abs_config_path}: {e}")
        raise ConfigurationError(f"Error parsing YAML configuration file: {e}") from e
    except Exception as e:
        logger.critical(f"Error reading configuration file {abs_config_path}: {e}")
        raise ConfigurationError(f"Error reading configuration file: {e}") from e

    try:
        resolved_config = resolve_value(raw_config, raw_config)
    except ValueError as e:
        logger.critical(f"Error resolving configuration values: {e}")
        raise

    try:
        _absolutize_paths(resolved_config, _base_dir)
    except Exception as e:
        logger.error(f"Error absolutizing paths in configuration: {e}", exc_info=True)

    # Assign to global cache *after* all processing
    _config = resolved_config
    logger.info(
        f"Configuration loaded and resolved successfully from {abs_config_path}"
    )
    return _config


def get_config() -> Dict[str, Any]:
    """
    Returns the cached configuration dictionary. Loads it if not already loaded.
    """
    # This check MUST happen before calling load_config
    if _config is None:
        logger.warning(
            "get_config() called before load_config(). Loading with default paths."
        )
        try:
            # load_config will set the _config global variable upon success
            load_config()
            if (
                _config is None
            ):  # Should not happen if load_config succeeded without error
                raise ConfigurationError(
                    "Configuration cache is still None after loading."
                )
        except Exception as e:
            logger.critical(
                f"Failed to load default configuration in get_config(): {e}",
                exc_info=True,
            )
            raise ConfigurationError("Configuration could not be loaded.") from e
    return _config


# Example Usage (no changes needed here)
if __name__ == "__main__":
    pass
