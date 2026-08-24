#!/usr/bin/env python3
"""
Web search utility using DuckDuckGo Search
"""

import argparse
import json
import logging
import sys
import time
from functools import wraps
from typing import Any

try:
    from ddgs import DDGS as _DDGS
except ImportError:  # pragma: no cover — ddgs may be absent in minimal installs
    try:
        from duckduckgo_search import DDGS as _DDGS  # type: ignore[assignment,no-redef]
    except ImportError:
        _DDGS = None  # type: ignore[assignment,no-redef]


def _resolve_ddgs():
    """Resolve the DDGS class from either ddgs or duckduckgo_search."""
    if _DDGS is None:
        _warn_ddgs_missing()
        raise ImportError("ddgs/duckduckgo_search not installed")
    return _DDGS


def _warn_ddgs_missing() -> None:
    """Lazily warn about missing ddgs package (called after logging is configured)."""
    logger.warning(
        "ddgs/duckduckgo_search not installed; web search features unavailable. "
        "Install with: pip install ddgs"
    )


# Note: ddgs >= 9.0 uses positional 'query' argument instead of 'keywords='


DEFAULT_MAX_RESULTS = 5
DEFAULT_REGION = "wt-wt"
DEFAULT_SAFESEARCH = "moderate"
DEFAULT_TIME_LIMIT = None
DEFAULT_TIMEOUT = 10
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_DELAY = 1.0
DDGS_TIMEOUT = 30  # Timeout for DuckDuckGo search API calls in seconds

logger = logging.getLogger(__name__)


class SearchError(Exception):
    """Base exception for search-related errors."""

    pass


class ValidationError(SearchError):
    """Exception for invalid input parameters."""

    pass


class NetworkError(SearchError):
    """Exception for network-related errors."""

    pass


def retry_with_backoff(
    max_retries: int = DEFAULT_MAX_RETRIES,
    delay: float = DEFAULT_RETRY_DELAY,
    retryable_exceptions: tuple = (ConnectionError, TimeoutError, OSError),
):
    """Decorator for retrying functions with exponential backoff.

    Args:
        max_retries: Maximum number of retry attempts
        delay: Initial delay between retries in seconds
        retryable_exceptions: Tuple of exception types that should trigger retries

    """

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception: Exception | None = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except retryable_exceptions as e:
                    last_exception = e
                    if attempt < max_retries:
                        wait_time = delay * (2**attempt)
                        logger.warning(
                            f"Attempt {attempt + 1} failed: {e}. Retrying in {wait_time:.1f}s..."
                        )
                        time.sleep(wait_time)
                    else:
                        logger.error(f"All {max_retries + 1} attempts failed. Last error: {e}")
                except Exception as e:  # broad catch intentional
                    # Non-retryable exception - fail immediately
                    logger.error(f"Non-retryable error: {e}")
                    raise
            if last_exception is not None:
                raise last_exception
            raise RuntimeError("Unexpected state: no exception but all retries exhausted")

        return wrapper

    return decorator


def validate_query(query: str) -> None:
    """Validate search query."""
    if not query or not isinstance(query, str):
        raise ValidationError("Query must be a non-empty string")
    if len(query) > 500:
        raise ValidationError("Query cannot exceed 500 characters")


def validate_max_results(max_results: int) -> None:
    """Validate max_results parameter."""
    if not isinstance(max_results, int) or max_results <= 0:
        raise ValidationError("max_results must be a positive integer")
    if max_results > 50:
        raise ValidationError("max_results cannot exceed 50")


def validate_safesearch(safesearch: str) -> None:
    """Validate safesearch parameter."""
    valid_safesearch = {"on", "moderate", "off"}
    if safesearch not in valid_safesearch:
        raise ValidationError(f"safesearch must be one of {valid_safesearch}")


def validate_timelimit(timelimit: str | None) -> None:
    """Validate timelimit parameter."""
    valid_timelimit = {None, "day", "week", "month", "year"}
    if timelimit not in valid_timelimit:
        raise ValidationError(f"timelimit must be one of {valid_timelimit}")


def validate_region(region: str) -> None:
    """Validate region parameter."""
    if not region or not isinstance(region, str):
        raise ValidationError("Region must be a non-empty string")


@retry_with_backoff(
    max_retries=DEFAULT_MAX_RETRIES,
    delay=DEFAULT_RETRY_DELAY,
    retryable_exceptions=(ConnectionError, TimeoutError, OSError, NetworkError),
)
def _perform_search(
    query: str, region: str, safesearch: str, timelimit: str | None, max_results: int
) -> list[dict[str, Any]]:
    """Perform the actual search with retry logic.

    Args:
        query: Search query string
        region: Search region code
        safesearch: Safe search setting
        timelimit: Time limit for results
        max_results: Maximum number of results to return

    Returns:
        List of search result dictionaries with title, href, and body fields

    """
    ddgs_class = _resolve_ddgs()
    results: list[dict[str, Any]] = []
    with ddgs_class(timeout=DDGS_TIMEOUT) as ddgs:
        search_results = ddgs.text(
            query,
            region=region,
            safesearch=safesearch,
            timelimit=timelimit,
            max_results=max_results,
        )

        for result in search_results:
            results.append(
                {
                    "title": result.get("title", ""),
                    "href": result.get("href", ""),
                    "body": result.get("body", ""),
                }
            )
    return results


def search(
    query: str,
    max_results: int = DEFAULT_MAX_RESULTS,
    region: str = DEFAULT_REGION,
    safesearch: str = DEFAULT_SAFESEARCH,
    timelimit: str | None = DEFAULT_TIME_LIMIT,
) -> str:
    """
    Perform a web search using DuckDuckGo and return results as JSON string.

    Args:
        query: Search query string (max 500 characters)
        max_results: Maximum number of results to return (1-50, default: 5)
        region: Search region code (default: 'wt-wt' for worldwide)
        safesearch: Safe search setting - 'on', 'moderate', or 'off' (default: 'moderate')
        timelimit: Time limit for results - 'day', 'week', 'month', 'year', or None (default: None)

    Returns:
        JSON string containing search results with 'title', 'href', and 'body' for each result.
        On error, returns JSON with 'error', 'query', and 'results' fields.

    Raises:
        ValidationError: If input parameters are invalid
        NetworkError: If search fails after retries (logged and returned as JSON error)

    """
    logger.info(
        f"Starting search for query: '{query[:50]}"
        f"{'...' if len(query) > 50 else ''}' "
        f"with max_results={max_results}"
    )

    # Validate all inputs
    validate_query(query)
    validate_max_results(max_results)
    validate_safesearch(safesearch)
    validate_timelimit(timelimit)
    validate_region(region)

    try:
        results = _perform_search(query, region, safesearch, timelimit, max_results)
        logger.info(f"Search completed successfully with {len(results)} results")
        return json.dumps({"results": results, "count": len(results)}, ensure_ascii=False, indent=2)
    except ValidationError:
        # Validation errors should be re-raised, not caught and converted to JSON
        raise
    except NetworkError as e:
        logger.error(f"Network error during search: {e}")
        return json.dumps({"error": str(e), "query": query, "results": []})
    except Exception as e:  # broad catch intentional
        logger.exception(f"Unexpected error during search: {e}")
        return json.dumps({"error": f"Unexpected error: {e!s}", "query": query, "results": []})


def setup_logging(verbose: bool = False) -> None:
    """Configure logging based on verbosity level."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Perform web searches using DuckDuckGo",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s "python programming"
  %(prog)s "AI news" -m 10 -r us-en
  %(prog)s "climate change" -t year -v
        """,
    )

    parser.add_argument("query", nargs="?", help="Search query string")
    parser.add_argument(
        "max_results",
        nargs="?",
        type=int,
        default=DEFAULT_MAX_RESULTS,
        help=f"Maximum results to return (default: {DEFAULT_MAX_RESULTS})",
    )
    parser.add_argument(
        "-r",
        "--region",
        default=DEFAULT_REGION,
        help=f"Search region (default: {DEFAULT_REGION})",
    )
    parser.add_argument(
        "-s",
        "--safesearch",
        default=DEFAULT_SAFESEARCH,
        choices=["on", "moderate", "off"],
        help=f"Safe search level (default: {DEFAULT_SAFESEARCH})",
    )
    parser.add_argument(
        "-t",
        "--timelimit",
        choices=["day", "week", "month", "year"],
        help="Time limit for results",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable verbose (debug) logging"
    )
    parser.add_argument(
        "--json-error",
        action="store_true",
        help="Output errors as JSON instead of stderr",
    )

    return parser.parse_args()


def main():
    """Command line interface for web search"""
    args = parse_args()

    setup_logging(verbose=args.verbose)

    if not args.query:
        logger.error("No query provided")
        logger.info("Usage: python web_search.py <query> [max_results]")
        logger.info("Example: python web_search.py 'python programming' 10")
        sys.exit(1)

    try:
        results = search(
            query=args.query,
            max_results=args.max_results,
            region=args.region,
            safesearch=args.safesearch,
            timelimit=args.timelimit,
        )
        logger.info(results)
    except ValidationError as e:
        if args.json_error:
            logger.info(json.dumps({"error": str(e)}))
        else:
            logger.info(f"Validation error: {e}")
        sys.exit(1)
    except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
        if args.json_error:
            logger.info(json.dumps({"error": f"Unexpected error: {e!s}"}))
        else:
            logger.info(f"Unexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
