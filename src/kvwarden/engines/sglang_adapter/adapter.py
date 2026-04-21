"""SGLang engine adapter.

Manages an SGLang OpenAI-compatible server as a subprocess and proxies
requests to it via HTTP.

Usage:
    adapter = SGLangAdapter(model_id="meta-llama/Llama-3.1-8B-Instruct", port=8002)
    await adapter.start()
    result = await adapter.forward_request("/v1/chat/completions", payload)
    await adapter.stop()
"""

from __future__ import annotations

from kvwarden.engines.base import EngineAdapter


class SGLangAdapter(EngineAdapter):
    """Adapter that manages an SGLang server subprocess.

    Launches ``python -m sglang.launch_server`` and health-checks
    via ``/v1/models``.

    All lifecycle logic (start, stop, health_check, forward_request)
    is inherited from :class:`EngineAdapter`.

    Args:
        model_id: HuggingFace model identifier.
        port: Port for the SGLang server.
        gpu_memory_utilization: Fraction of GPU memory to use (maps to mem_fraction_static).
        tensor_parallel_size: Number of GPUs.
        dtype: Weight data type.
        max_model_len: Maximum sequence length (context length).
        extra_args: Additional CLI arguments passed to sglang.
    """

    engine_name = "SGLang"

    def _build_cmd(self) -> list[str]:
        """Build the SGLang server launch command.

        Returns:
            Command as a list of strings.
        """
        cmd = [
            "python",
            "-m",
            "sglang.launch_server",
            "--model-path",
            self.model_id,
            "--port",
            str(self.port),
            "--mem-fraction-static",
            str(self.gpu_memory_utilization),
            "--tp",
            str(self.tensor_parallel_size),
            "--context-length",
            str(self.max_model_len),
        ]
        if self.dtype != "auto":
            cmd.extend(["--dtype", self.dtype])
        cmd.extend(self.extra_args)
        return cmd
