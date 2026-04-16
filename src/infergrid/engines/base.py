"""Abstract base class for LLM engine adapters.

Both vLLM and SGLang adapters implement this interface, providing
a uniform way for the router to start/stop engines and forward
OpenAI-compatible requests.
"""

from __future__ import annotations

import abc
from typing import Any


class EngineAdapter(abc.ABC):
    """Base interface for engine adapters.

    Each adapter manages a single engine subprocess and proxies
    OpenAI-compatible HTTP requests to it.

    Args:
        model_id: HuggingFace model identifier.
        port: Port number for the engine's HTTP server.
        gpu_memory_utilization: Fraction of GPU memory the engine may use.
        tensor_parallel_size: Number of GPUs for tensor parallelism.
        extra_args: Additional CLI arguments for the engine process.
    """

    def __init__(
        self,
        model_id: str,
        port: int,
        gpu_memory_utilization: float = 0.85,
        tensor_parallel_size: int = 1,
        extra_args: list[str] | None = None,
    ) -> None:
        self.model_id = model_id
        self.port = port
        self.gpu_memory_utilization = gpu_memory_utilization
        self.tensor_parallel_size = tensor_parallel_size
        self.extra_args = extra_args or []
        self._healthy = False

    @property
    def base_url(self) -> str:
        """HTTP base URL for this engine instance."""
        return f"http://localhost:{self.port}"

    @abc.abstractmethod
    async def start(self, timeout_s: int = 300) -> None:
        """Start the engine subprocess and wait until it is ready.

        Args:
            timeout_s: Maximum seconds to wait for the engine to become healthy.

        Raises:
            TimeoutError: If the engine does not become healthy in time.
            RuntimeError: If the engine process exits unexpectedly.
        """

    @abc.abstractmethod
    async def stop(self) -> None:
        """Stop the engine subprocess gracefully."""

    @abc.abstractmethod
    async def health_check(self) -> bool:
        """Check whether the engine is alive and serving.

        Returns:
            True if the engine responds to a health probe.
        """

    @abc.abstractmethod
    async def forward_request(
        self,
        path: str,
        payload: dict[str, Any],
        stream: bool = False,
    ) -> Any:
        """Forward an OpenAI-compatible request to the engine.

        Args:
            path: API path (e.g. "/v1/chat/completions").
            payload: JSON request body.
            stream: Whether to stream the response.

        Returns:
            The JSON response dict, or an async generator of SSE chunks
            when streaming.
        """

    @property
    def is_healthy(self) -> bool:
        """Return the last-known health status."""
        return self._healthy

    def __repr__(self) -> str:
        status = "healthy" if self._healthy else "unhealthy"
        return (
            f"<{self.__class__.__name__} model={self.model_id!r} "
            f"port={self.port} {status}>"
        )
