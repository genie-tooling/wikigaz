import logging
import logging.handlers
import sys
import os
import json
from typing import Dict, Any, Optional, cast

# Use try-except for conditional import, useful if jsonlogger is optional
try:
    from pythonjsonlogger import jsonlogger

    _has_jsonlogger = True
except ImportError:
    _has_jsonlogger = False

    # Define a dummy class if jsonlogger is not installed but JSON format is requested
    # Provides basic JSON structure but lacks advanced features of jsonlogger
    class JsonFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            log_record: Dict[str, Any] = {
                "timestamp": self.formatTime(record, self.datefmt),
                "level": record.levelname,
                "name": record.name,
                "message": record.getMessage(),  # Ensure message is formatted
            }
            # Add exception info if available
            if record.exc_info:
                # formatException can return multi-line string, which isn't ideal for JSON value
                # Try to format it concisely
                log_record["exception"] = self.formatException(record.exc_info).split(
                    "\n", 1
                )[
                    0
                ]  # Just first line?
                # Alternatively, store the full traceback as a string property
                # log_record['exc_info'] = self.formatException(record.exc_info)
            # Add stack info if available
            if record.stack_info:
                log_record["stack_info"] = self.formatStack(record.stack_info)
            # Add extra fields if passed via extra={}
            extra_fields = {
                k: v
                for k, v in record.__dict__.items()
                if k
                not in {
                    "args",
                    "asctime",
                    "created",
                    "exc_info",
                    "exc_text",
                    "filename",
                    "funcName",
                    "levelname",
                    "levelno",
                    "lineno",
                    "module",
                    "msecs",
                    "message",
                    "msg",
                    "name",
                    "pathname",
                    "process",
                    "processName",
                    "relativeCreated",
                    "stack_info",
                    "thread",
                    "threadName",
                }
            }
            if extra_fields:
                log_record["extra"] = extra_fields

            # Use default json encoder
            try:
                return json.dumps(log_record, ensure_ascii=False)
            except TypeError:
                # Handle non-serializable data in extra fields gracefully
                log_record.pop("extra", None)  # Remove problematic extra fields
                return json.dumps(log_record, ensure_ascii=False)


# Import get_config and pydash_get carefully for standalone execution
try:
    from .config import get_config, pydash_get
except ImportError:
    # Allow running script directly for testing/example
    # This requires config.py to be in the same directory or python path
    try:
        from config import get_config, pydash_get
    except ImportError:
        # Fallback if config cannot be imported at all
        def get_config():
            return {}

        def pydash_get(cfg, key, default=None):
            return default


class CustomJsonFormatter(
    jsonlogger.JsonFormatter if _has_jsonlogger else JsonFormatter
):
    """
    Custom JSON formatter that standardizes field names and formats.
    Inherits from jsonlogger.JsonFormatter if available, otherwise uses basic fallback.
    """

    def add_fields(
        self,
        log_record: Dict[str, Any],
        record: logging.LogRecord,
        message_dict: Dict[str, Any],
    ):
        # Call parent method first if available (handles merging message_dict)
        if _has_jsonlogger:
            super(CustomJsonFormatter, self).add_fields(
                log_record, record, message_dict
            )

        # Ensure standard fields are present and consistently named
        # Use formatTime for consistent timestamp formatting
        log_record["timestamp"] = self.formatTime(record, self.datefmt)
        log_record["level"] = record.levelname  # Use level name (e.g., INFO)
        log_record["name"] = record.name  # Logger name

        # Ensure 'message' field contains the formatted log message
        # The parent jsonlogger formatter usually handles this via rename_fields
        # If using fallback, ensure getMessage() is captured correctly.
        if "message" not in log_record:
            log_record["message"] = record.getMessage()

        # Clean up potentially redundant fields added by default formatters
        # that might be duplicated by jsonlogger or our standardization
        for field in [
            "asctime",
            "levelname",
            "filename",
            "funcName",
            "lineno",
            "module",
            "pathname",
        ]:
            log_record.pop(field, None)

        # Ensure exception/stack info are included if present
        if record.exc_info and "exc_info" not in log_record:
            log_record["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info and "stack_info" not in log_record:
            log_record["stack_info"] = self.formatStack(record.stack_info)


def setup_logging(config: Optional[Dict[str, Any]] = None):
    """
    Configures logging based on the provided or globally loaded configuration.

    Sets up console and rotating file handlers. Supports 'text' and 'json' formats.
    Ensures log directories exist and handles potential errors gracefully.

    Args:
        config: Optional configuration dictionary. If None, loads global config.
    """
    if config is None:
        try:
            config = get_config()
        except Exception as e:
            # Basic fallback logging if config loading fails entirely
            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s - %(levelname)s - [FallbackLogger] - %(message)s",
            )
            logging.error(
                f"Failed to load configuration for logging setup: {e}. Using basic console logging."
            )
            return

    # --- Get Logging Configuration ---
    log_config = config.get("logging", {})
    log_level_str = str(pydash_get(log_config, "level", "INFO")).upper()
    log_format_str = str(pydash_get(log_config, "format", "text")).lower()
    log_file_path = pydash_get(
        log_config, "log_file_path"
    )  # Path should be absolute after config load
    max_bytes = int(
        pydash_get(log_config, "max_bytes", 10 * 1024 * 1024)
    )  # Default 10MB
    backup_count = int(pydash_get(log_config, "backup_count", 5))

    log_level = getattr(logging, log_level_str, logging.INFO)

    # --- Get Root Logger and Clear Existing Handlers ---
    # This prevents duplicate logs if setup_logging is called multiple times
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)  # Set level on root logger

    if root_logger.hasHandlers():
        logging.debug("Removing existing logging handlers.")
        for handler in root_logger.handlers[:]:
            try:
                handler.flush()
                handler.close()
            except Exception as e:
                sys.stderr.write(f"Warning: Error closing handler {handler}: {e}\n")
            root_logger.removeHandler(handler)

    # --- Define Formatters ---
    # Consistent timestamp format
    date_format = "%Y-%m-%d %H:%M:%S"
    # Basic text format string
    text_format_string = "%(asctime)s - %(levelname)s - [%(name)s] - %(message)s"

    formatter: logging.Formatter
    if log_format_str == "json":
        if not _has_jsonlogger:
            logging.warning(
                "JSON log format requested, but 'python-json-logger' not installed. Using basic JSON fallback."
            )
        # Standardize field names using CustomJsonFormatter
        # Define the format string for jsonlogger base fields
        json_format_string = "%(timestamp)s %(level)s %(name)s %(message)s"
        formatter = CustomJsonFormatter(json_format_string, datefmt=date_format)
    else:  # Default to text
        formatter = logging.Formatter(text_format_string, datefmt=date_format)

    # --- Console Handler ---
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # --- File Handler (Rotating) ---
    file_handler: Optional[logging.handlers.RotatingFileHandler] = None
    if log_file_path and isinstance(log_file_path, str):
        try:
            # Directory existence should be ensured by config loader's _absolutize_paths
            log_dir = os.path.dirname(log_file_path)
            # Ensure log dir exists one last time ( belt-and-suspenders )
            if log_dir and not os.path.exists(log_dir):
                os.makedirs(log_dir, exist_ok=True)

            # Use RotatingFileHandler for log rotation
            file_handler = logging.handlers.RotatingFileHandler(
                log_file_path,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
                delay=True,  # Delay file opening until first log message (safer on init)
            )
            file_handler.setLevel(log_level)
            file_handler.setFormatter(formatter)
            root_logger.addHandler(file_handler)
            # Use standard logging mechanism, not print
            logging.info(
                f"Logging initialized. Level: {log_level_str}, Format: {log_format_str}, File: {log_file_path}"
            )

        except Exception as e:
            # Use standard logging mechanism for errors
            logging.error(
                f"Failed to configure file logging to {log_file_path}: {e}",
                exc_info=True,
            )
            logging.info("Logging to console only.")
            if file_handler:  # Clean up if handler was created but add failed
                try:
                    file_handler.close()  # Attempt close before remove
                except Exception:
                    pass
                root_logger.removeHandler(file_handler)
    else:
        logging.info(
            f"Logging initialized. Level: {log_level_str}, Format: {log_format_str}, Console only (no log file path configured)."
        )


# Example Usage
if __name__ == "__main__":
    # Assume config.py is runnable and creates dummy files for this example
    try:
        from config import load_config  # Try importing again for standalone execution

        cfg = load_config(
            config_path="config_temp_log.yaml",
            dotenv_path=".env_temp_log",
            force_reload=True,
        )
        setup_logging(config=cfg)

        # Test logging
        print("\n--- Testing Logging ---")
        logger = logging.getLogger("ExampleApp")  # Get a specific logger
        logger.debug("This is a debug message.", extra={"user": "test", "id": 1})
        logger.info("This is an info message.")
        logger.warning("This is a warning.")
        logger.error("This is an error.")
        logger.critical("This is critical.")
        try:
            x = 1 / 0
        except ZeroDivisionError:
            logger.exception("Caught an exception!")

        print("\nCheck console output and logs/example.log for messages.")

    except Exception as e:
        print(f"\n*** Logging Example Failed: {e}")
        logging.exception("Error during logging example:")
    finally:
        # Clean up dummy files created by config.py example
        base = os.path.dirname(os.path.abspath(__file__))  # Assume utils dir
        env_file = os.path.join(base, ".env_temp_log")
        cfg_file = os.path.join(base, "config_temp_log.yaml")
        log_file = os.path.join(base, "../logs/example.log")  # Relative to base
        if os.path.exists(env_file):
            os.remove(env_file)
        if os.path.exists(cfg_file):
            os.remove(cfg_file)
        # Careful cleanup of logs - requires closing handlers first
        root_logger = logging.getLogger()
        for handler in root_logger.handlers[:]:
            try:
                if isinstance(
                    handler, logging.FileHandler
                ) and handler.baseFilename == os.path.abspath(log_file):
                    handler.flush()
                    handler.close()
                    root_logger.removeHandler(handler)
                    print(f"Closed handler for {log_file}")
            except Exception as e:
                print(f"Error closing log handler: {e}")
        if os.path.exists(log_file):
            os.remove(log_file)
        # Add removal of backup log files if needed
        print("Cleanup attempt complete.")
