from dataclasses import dataclass


@dataclass
class RateLimitConfig:
    """Configuration for rate limiting."""

    requests_per_second: float = 1.0
    burst_size: int = 10
    tokens_per_request: int = 1000

    def __post_init__(self) -> None:
        """Validate configuration parameters."""
        if self.requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        if self.burst_size <= 0:
            raise ValueError("burst_size must be positive")
        if self.tokens_per_request <= 0:
            raise ValueError("tokens_per_request must be positive")
