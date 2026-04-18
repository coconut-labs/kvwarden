"""Base class for LLM engine adapters.

Both vLLM and SGLang adapters inherit from EngineAdapter, which provides
the shared logic for starting/stopping the subprocess, health-checking,
and forwarding OpenAI-compatible requests.  Subclasses only need to
implement ``_build_cmd()`` and set ``engine_name``.
"""

from __future__ import annotations

import abc
import asyncio
import logging
import os
import re
import tempfile
from typing import Any, AsyncIterator

import aiohttp

logger = logging.getLogger(__name__)

# Directory for per-engine subprocess logs. Overridden via INFERGRID_ENGINE_LOG_DIR env.
_ENGINE_LOG_DIR = os.environ.get("INFERGRID_ENGINE_LOG_DIR") or tempfile.gettempdir()


class EngineAdapter(abc.ABC):
    """Base class for engine adapters.

    Each adapter manages a single engine subprocess and proxies
    OpenAI-compatible HTTP requests to it.

    Subclasses must set ``engine_name`` and implement ``_build_cmd()``.

    Args:
        model_id: HuggingFace model identifier.
        port: Port number for the engine's HTTP server.
        gpu_memory_utilization: Fraction of GPU memory the engine may use.
        tensor_parallel_size: Number of GPUs for tensor parallelism.
        dtype: Weight data type (e.g. "bfloat16", "float16", "auto").
        max_model_len: Maximum sequence length.
        extra_args: Additional CLI arguments for the engine process.
    """

    engine_name: str  # e.g. "vLLM" or "SGLang" -- set by subclasses

    def __init__(
        self,
        model_id: str,
        port: int,
        gpu_memory_utilization: float = 0.85,
        tensor_parallel_size: int = 1,
        dtype: str = "auto",
        max_model_len: int = 8192,
        extra_args: list[str] | None = None,
    ) -> None:
        self.model_id = model_id
        self.port = port
        self.gpu_memory_utilization = gpu_memory_utilization
        self.tensor_parallel_size = tensor_parallel_size
        self.dtype = dtype
        self.max_model_len = max_model_len
        self.extra_args = extra_args or []
        self._healthy = False
        self._process: asyncio.subprocess.Process | None = None

    @property
    def base_url(self) -> str:
        """HTTP base URL for this engine instance."""
        return f"http://localhost:{self.port}"

    # ── Abstract: subclasses must implement ────────────────────────

    @abc.abstractmethod
    def _build_cmd(self) -> list[str]:
        """Build the engine server launch command.

        Returns:
            Command as a list of strings.
        """

    # ── Lifecycle ──────────────────────────────────────────────────

    async def start(self, timeout_s: int = 300) -> None:
        """Start the engine subprocess and wait until it is ready.

        Args:
            timeout_s: Maximum seconds to wait for the engine to become healthy.

        Raises:
            TimeoutError: If the engine does not become healthy in time.
            RuntimeError: If the engine process exits unexpectedly.
        """
        cmd = self._build_cmd()
        logger.info("Starting %s server: %s", self.engine_name, " ".join(cmd))

        # Persist full stdout+stderr to a per-engine log file. vLLM's v1 engine
        # spawns its own worker subprocess whose stderr is lost if we read via
        # PIPE + trailing slice -- we must tee the full stream to disk so the
        # root cause survives for postmortem (e.g. "Numba needs NumPy <= 2.2").
        safe_id = re.sub(r"[^A-Za-z0-9._-]", "_", self.model_id)
        self._engine_log_path = os.path.join(
            _ENGINE_LOG_DIR,
            f"infergrid_engine_{self.engine_name.lower()}_{safe_id}_p{self.port}.log",
        )
        # Truncate on each start to keep failures scoped to the current attempt.
        engine_log = open(self._engine_log_path, "w", buffering=1)
        logger.info("%s engine log: %s", self.engine_name, self._engine_log_path)

        # Force unbuffered Python in the child so crash-time stderr actually hits
        # disk before the process dies (block buffering otherwise swallows the
        # last few KB — exactly where the root cause tends to be).
        child_env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=engine_log,
            stderr=engine_log,
            env=child_env,
        )
        engine_log.close()  # subprocess has its own dup'd fd; our handle is safe to drop

        # Poll for health
        deadline = asyncio.get_event_loop().time() + timeout_s
        while asyncio.get_event_loop().time() < deadline:
            if self._process.returncode is not None:
                tail = ""
                try:
                    with open(self._engine_log_path, "r", errors="replace") as f:
                        tail = f.read()[-10_000:]
                except OSError:
                    pass
                raise RuntimeError(
                    f"{self.engine_name} process exited with code "
                    f"{self._process.returncode}. "
                    f"Full log: {self._engine_log_path}. "
                    f"Last 10KB:\n{tail}"
                )

            if await self.health_check():
                self._healthy = True
                logger.info(
                    "%s server ready on port %d for model %s",
                    self.engine_name, self.port, self.model_id,
                )
                return

            await asyncio.sleep(2.0)

        # Timed out -- kill the process
        await self.stop()
        raise TimeoutError(
            f"{self.engine_name} server did not become healthy within {timeout_s}s"
        )

    async def stop(self) -> None:
        """Stop the engine subprocess gracefully."""
        if self._process is None:
            return

        logger.info("Stopping %s server on port %d", self.engine_name, self.port)
        try:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                logger.warning("%s process did not exit, killing", self.engine_name)
                self._process.kill()
                await self._process.wait()
        except ProcessLookupError:
            pass  # already dead

        self._process = None
        self._healthy = False

    async def health_check(self) -> bool:
        """Probe the /v1/models endpoint.

        Returns:
            True if the server responds with HTTP 200.
        """
        url = f"{self.base_url}/v1/models"
        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    healthy = resp.status == 200
                    self._healthy = healthy
                    return healthy
        except Exception as exc:
            logger.debug("Health check failed for %s: %s", self.model_id, exc)
            self._healthy = False
            return False

    async def forward_request(
        self,
        path: str,
        payload: dict[str, Any],
        stream: bool = False,
    ) -> dict[str, Any] | AsyncIterator[bytes]:
        """Forward an OpenAI-compatible request to the engine.

        Args:
            path: API path (e.g. "/v1/chat/completions").
            payload: JSON request body.
            stream: If True, return an async iterator of SSE chunks.

        Returns:
            JSON response dict, or async byte iterator for streaming.

        Raises:
            aiohttp.ClientError: On connection or HTTP errors.
        """
        url = f"{self.base_url}{path}"
        if stream:
            payload["stream"] = True

        timeout = aiohttp.ClientTimeout(total=300)
        session = aiohttp.ClientSession(timeout=timeout)

        try:
            if stream:
                return self._stream_response(session, url, payload)
            else:
                async with session.post(url, json=payload) as resp:
                    result = await resp.json()
                    await session.close()
                    return result
        except Exception:
            await session.close()
            raise

    async def _stream_response(
        self,
        session: aiohttp.ClientSession,
        url: str,
        payload: dict[str, Any],
    ) -> AsyncIterator[bytes]:
        """Stream SSE chunks from the engine.

        Args:
            session: aiohttp session (caller manages lifetime).
            url: Full request URL.
            payload: JSON body.

        Yields:
            Raw SSE bytes from the engine.
        """
        try:
            async with session.post(url, json=payload) as resp:
                async for chunk in resp.content.iter_any():
                    yield chunk
        finally:
            await session.close()

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
