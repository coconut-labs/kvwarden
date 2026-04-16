"""vLLM engine adapter.

Manages a vLLM OpenAI-compatible server as a subprocess and proxies
requests to it via HTTP.

Usage:
    adapter = VLLMAdapter(model_id="meta-llama/Llama-3.1-8B-Instruct", port=8001)
    await adapter.start()
    result = await adapter.forward_request("/v1/chat/completions", payload)
    await adapter.stop()
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator

import aiohttp

from infergrid.engines.base import EngineAdapter

logger = logging.getLogger(__name__)


class VLLMAdapter(EngineAdapter):
    """Adapter that manages a vLLM server subprocess.

    Launches ``python -m vllm.entrypoints.openai.api_server`` and
    health-checks via ``/v1/models``.

    Args:
        model_id: HuggingFace model identifier.
        port: Port for the vLLM server.
        gpu_memory_utilization: Fraction of GPU memory to use.
        tensor_parallel_size: Number of GPUs.
        dtype: Weight data type (e.g. "bfloat16", "float16", "auto").
        max_model_len: Maximum sequence length.
        extra_args: Additional CLI arguments passed to vllm.
    """

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
        super().__init__(
            model_id=model_id,
            port=port,
            gpu_memory_utilization=gpu_memory_utilization,
            tensor_parallel_size=tensor_parallel_size,
            extra_args=extra_args,
        )
        self.dtype = dtype
        self.max_model_len = max_model_len
        self._process: asyncio.subprocess.Process | None = None

    def _build_cmd(self) -> list[str]:
        """Build the vLLM server launch command.

        Returns:
            Command as a list of strings.
        """
        cmd = [
            "python", "-m", "vllm.entrypoints.openai.api_server",
            "--model", self.model_id,
            "--port", str(self.port),
            "--gpu-memory-utilization", str(self.gpu_memory_utilization),
            "--tensor-parallel-size", str(self.tensor_parallel_size),
            "--dtype", self.dtype,
            "--max-model-len", str(self.max_model_len),
        ]
        cmd.extend(self.extra_args)
        return cmd

    async def start(self, timeout_s: int = 300) -> None:
        """Start the vLLM server subprocess.

        Args:
            timeout_s: Maximum seconds to wait for the server to be ready.

        Raises:
            TimeoutError: Server did not become healthy in time.
            RuntimeError: Server process exited before becoming healthy.
        """
        cmd = self._build_cmd()
        logger.info("Starting vLLM server: %s", " ".join(cmd))

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Poll for health
        deadline = asyncio.get_event_loop().time() + timeout_s
        while asyncio.get_event_loop().time() < deadline:
            if self._process.returncode is not None:
                stderr = ""
                if self._process.stderr:
                    stderr_bytes = await self._process.stderr.read()
                    stderr = stderr_bytes.decode(errors="replace")[-2000:]
                raise RuntimeError(
                    f"vLLM process exited with code {self._process.returncode}: "
                    f"{stderr}"
                )

            if await self.health_check():
                self._healthy = True
                logger.info(
                    "vLLM server ready on port %d for model %s",
                    self.port, self.model_id,
                )
                return

            await asyncio.sleep(2.0)

        # Timed out -- kill the process
        await self.stop()
        raise TimeoutError(
            f"vLLM server did not become healthy within {timeout_s}s"
        )

    async def stop(self) -> None:
        """Stop the vLLM server subprocess."""
        if self._process is None:
            return

        logger.info("Stopping vLLM server on port %d", self.port)
        try:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                logger.warning("vLLM process did not exit, killing")
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
        except Exception:
            self._healthy = False
            return False

    async def forward_request(
        self,
        path: str,
        payload: dict[str, Any],
        stream: bool = False,
    ) -> dict[str, Any] | AsyncIterator[bytes]:
        """Forward an OpenAI-compatible request to the vLLM server.

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
