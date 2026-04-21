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

from kvwarden.engines.base import EngineAdapter


class VLLMAdapter(EngineAdapter):
    """Adapter that manages a vLLM server subprocess.

    Launches ``python -m vllm.entrypoints.openai.api_server`` and
    health-checks via ``/v1/models``.

    All lifecycle logic (start, stop, health_check, forward_request)
    is inherited from :class:`EngineAdapter`.

    Args:
        model_id: HuggingFace model identifier.
        port: Port for the vLLM server.
        gpu_memory_utilization: Fraction of GPU memory to use.
        tensor_parallel_size: Number of GPUs.
        dtype: Weight data type (e.g. "bfloat16", "float16", "auto").
        max_model_len: Maximum sequence length.
        extra_args: Additional CLI arguments passed to vllm.
    """

    engine_name = "vLLM"

    def _build_cmd(self) -> list[str]:
        """Build the vLLM server launch command.

        Returns:
            Command as a list of strings.
        """
        cmd = [
            "python",
            "-m",
            "vllm.entrypoints.openai.api_server",
            "--model",
            self.model_id,
            "--port",
            str(self.port),
            "--gpu-memory-utilization",
            str(self.gpu_memory_utilization),
            "--tensor-parallel-size",
            str(self.tensor_parallel_size),
            "--dtype",
            self.dtype,
            "--max-model-len",
            str(self.max_model_len),
        ]
        cmd.extend(self.extra_args)
        return cmd
