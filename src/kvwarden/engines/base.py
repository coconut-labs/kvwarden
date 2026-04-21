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
import time
from typing import Any, AsyncIterator

import aiohttp

logger = logging.getLogger(__name__)


class EngineCircuitOpenError(Exception):
    """Raised when an engine's circuit breaker is open (too many recent timeouts).

    Router handlers should catch this and return HTTP 503 without re-attempting
    the engine call. Shedding fast is the point: it turns a 30-second
    per-request timeout storm into millisecond-scale errors.
    """


# Directory for per-engine subprocess logs. Overridden via KVWARDEN_ENGINE_LOG_DIR env.
_ENGINE_LOG_DIR = os.environ.get("KVWARDEN_ENGINE_LOG_DIR") or tempfile.gettempdir()


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
        # Single aiohttp session reused across forward_request calls. Opening a
        # new session+connector per request leaks FDs and defeats connection
        # pooling — a meaningful slice of Gate 0's 3h stall was spent dialing
        # new sockets to an engine that had already stopped responding.
        self._session: aiohttp.ClientSession | None = None
        # Circuit breaker state (R4). After `_CIRCUIT_THRESHOLD` consecutive
        # TimeoutErrors, the circuit opens for `_CIRCUIT_COOLDOWN_S` seconds.
        # forward_request raises EngineCircuitOpenError while open, without
        # touching the network.
        self._consecutive_timeouts = 0
        self._circuit_open_until: float = 0.0

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
        # Dev/test mode: reuse an already-running engine on self.port (e.g. the
        # mock engine from benchmarks/scripts/mock_engine.py). Skips subprocess
        # launch so the router can be exercised without a GPU.
        if os.environ.get("KVWARDEN_DEV_SKIP_ENGINE_LAUNCH"):
            logger.info(
                "%s dev mode: skipping subprocess launch, attaching to localhost:%d",
                self.engine_name,
                self.port,
            )
            # Wait briefly for the already-running engine to be healthy
            deadline = asyncio.get_event_loop().time() + timeout_s
            while asyncio.get_event_loop().time() < deadline:
                if await self.health_check():
                    self._healthy = True
                    logger.info(
                        "%s (mock) reachable on port %d for model %s",
                        self.engine_name,
                        self.port,
                        self.model_id,
                    )
                    return
                await asyncio.sleep(1.0)
            raise TimeoutError(
                f"{self.engine_name} dev-mode: no mock engine on port {self.port}"
            )

        cmd = self._build_cmd()
        logger.info("Starting %s server: %s", self.engine_name, " ".join(cmd))

        # Persist full stdout+stderr to a per-engine log file. vLLM's v1 engine
        # spawns its own worker subprocess whose stderr is lost if we read via
        # PIPE + trailing slice -- we must tee the full stream to disk so the
        # root cause survives for postmortem (e.g. "Numba needs NumPy <= 2.2").
        safe_id = re.sub(r"[^A-Za-z0-9._-]", "_", self.model_id)
        self._engine_log_path = os.path.join(
            _ENGINE_LOG_DIR,
            f"kvwarden_engine_{self.engine_name.lower()}_{safe_id}_p{self.port}.log",
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
                    self.engine_name,
                    self.port,
                    self.model_id,
                )
                return

            await asyncio.sleep(2.0)

        # Timed out -- kill the process
        await self.stop()
        raise TimeoutError(
            f"{self.engine_name} server did not become healthy within {timeout_s}s"
        )

    async def stop(self) -> None:
        """Stop the engine subprocess gracefully and close the shared session."""
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

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

    # Asymmetric timeouts for router-to-engine calls (Gate 0.5 fix).
    # total: upper bound on any single engine call. Set below the harness /
    #   router-to-client total so router surfaces failure before the client
    #   gives up.
    # sock_connect: TCP connect budget.
    # sock_read: idle budget — max time between bytes. This is the one that
    #   turns a silent vLLM hang from a 300s stall into a ~30s fast failure.
    _ENGINE_TOTAL_TIMEOUT_S = 240
    _ENGINE_CONNECT_TIMEOUT_S = 10
    _ENGINE_IDLE_TIMEOUT_S = 30
    # Circuit breaker parameters (R4).
    _CIRCUIT_THRESHOLD = 3  # consecutive timeouts before opening
    _CIRCUIT_COOLDOWN_S = 60.0  # seconds the circuit stays open

    def _get_session(self) -> aiohttp.ClientSession:
        """Return the shared session, creating it on first use.

        One session per adapter means connection pooling works and the FD
        footprint stays bounded at high request counts.
        """
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(
                total=self._ENGINE_TOTAL_TIMEOUT_S,
                sock_connect=self._ENGINE_CONNECT_TIMEOUT_S,
                sock_read=self._ENGINE_IDLE_TIMEOUT_S,
            )
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    def _check_circuit(self) -> None:
        """Raise EngineCircuitOpenError if the circuit is currently open."""
        if self._circuit_open_until and time.monotonic() < self._circuit_open_until:
            raise EngineCircuitOpenError(
                f"{self.engine_name} circuit open for {self.model_id}: "
                f"{self._consecutive_timeouts} consecutive timeouts, "
                f"cooldown {self._circuit_open_until - time.monotonic():.1f}s remaining"
            )
        # Cooldown elapsed — give it one chance to recover. The next success
        # resets _consecutive_timeouts; another failure re-opens the circuit.

    def _note_timeout(self) -> None:
        """Record a router-to-engine TimeoutError; open circuit if threshold hit."""
        self._consecutive_timeouts += 1
        if self._consecutive_timeouts >= self._CIRCUIT_THRESHOLD:
            self._circuit_open_until = time.monotonic() + self._CIRCUIT_COOLDOWN_S
            self._healthy = False
            logger.warning(
                "%s circuit OPEN for %s: %d consecutive timeouts, cooldown %.0fs",
                self.engine_name,
                self.model_id,
                self._consecutive_timeouts,
                self._CIRCUIT_COOLDOWN_S,
            )

    def _note_success(self) -> None:
        """Record a successful request; close circuit if it was cooling down."""
        if self._consecutive_timeouts > 0 or self._circuit_open_until:
            logger.info(
                "%s circuit RESET for %s (was %d consecutive timeouts)",
                self.engine_name,
                self.model_id,
                self._consecutive_timeouts,
            )
        self._consecutive_timeouts = 0
        self._circuit_open_until = 0.0

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
            asyncio.TimeoutError: on total/connect/idle timeout.
            aiohttp.ClientError: on connection or HTTP errors.
        """
        self._check_circuit()

        url = f"{self.base_url}{path}"
        if stream:
            payload["stream"] = True

        session = self._get_session()

        if stream:
            # Return the async generator directly; it uses the shared session
            # and does NOT close it on completion (session lifetime == adapter
            # lifetime). The generator notes success/timeout internally.
            return self._stream_response(session, url, payload)

        try:
            async with session.post(url, json=payload) as resp:
                result = await resp.json()
        except (asyncio.TimeoutError, aiohttp.ServerTimeoutError):
            self._note_timeout()
            raise
        self._note_success()
        return result

    async def _stream_response(
        self,
        session: aiohttp.ClientSession,
        url: str,
        payload: dict[str, Any],
    ) -> AsyncIterator[bytes]:
        """Stream SSE chunks from the engine over the shared session."""
        try:
            async with session.post(url, json=payload) as resp:
                async for chunk in resp.content.iter_any():
                    yield chunk
        except (asyncio.TimeoutError, aiohttp.ServerTimeoutError):
            self._note_timeout()
            raise
        else:
            self._note_success()

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
