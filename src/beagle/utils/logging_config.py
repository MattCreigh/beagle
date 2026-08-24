import logging

# Modified logging for debugging during Copilot task execution
LOGGING_FORMATTER = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s (Module: %(module)s - Line: %(lineno)d)"
)


def get_debug_logger(logger_name):
    logger = logging.getLogger(logger_name)
    if not logger.handlers:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(LOGGING_FORMATTER)
        logger.addHandler(stream_handler)
        logger.setLevel(logging.DEBUG)  # Set DEBUG logging level for step tracing
    return logger


def debug_env_var(var_name, default=None):
    import os

    logger = get_debug_logger("EnvironmentTrace")
    variable = os.environ.get(var_name, default)
    # Mask sensitive values: show first 4 chars + "***" for values longer than 8 chars
    masked = str(variable)[:4] + "***" if variable and len(str(variable)) > 8 else variable
    logger.debug("Environment variable %s: %s", var_name, masked)
    return variable
