"""InferGrid configuration dataclasses.

Defines the full configuration surface for the orchestration layer:
models, ports, GPU budget, cache tiers, tenant defaults.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class ModelConfig:
    """Configuration for a single model."""

    model_id: str
    short_name: str = ""
    engine: str = "vllm"  # "vllm" or "sglang"
    tensor_parallel_size: int = 1
    gpu_memory_utilization: float = 0.85
    max_model_len: int = 8192
    dtype: str = "bfloat16"
    port: int = 0  # 0 = auto-assign
    extra_args: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.short_name:
            self.short_name = self.model_id.split("/")[-1].lower()


@dataclass
class CacheTierConfig:
    """Configuration for a single cache tier."""

    name: str  # "gpu", "cpu", "ssd"
    capacity_gb: float
    latency_ms: float  # approximate access latency


@dataclass
class CacheConfig:
    """KV cache tiering configuration."""

    tiers: list[CacheTierConfig] = field(default_factory=lambda: [
        CacheTierConfig(name="gpu", capacity_gb=40.0, latency_ms=0.01),
        CacheTierConfig(name="cpu", capacity_gb=128.0, latency_ms=0.5),
        CacheTierConfig(name="ssd", capacity_gb=512.0, latency_ms=5.0),
    ])
    eviction_frequency_weight: float = 0.7
    eviction_recency_weight: float = 0.3
    block_size_tokens: int = 16


@dataclass
class TenantDefaults:
    """Default resource budgets for new tenants."""

    max_concurrent_requests: int = 64
    rate_limit_rpm: int = 600  # requests per minute
    max_gpu_memory_gb: float = 40.0
    priority: int = 1  # higher = more priority


@dataclass
class InferGridConfig:
    """Top-level InferGrid configuration."""

    host: str = "0.0.0.0"
    port: int = 8080
    gpu_budget_fraction: float = 0.80
    models: list[ModelConfig] = field(default_factory=list)
    cache: CacheConfig = field(default_factory=CacheConfig)
    tenant_defaults: TenantDefaults = field(default_factory=TenantDefaults)
    engine_start_timeout_s: int = 300
    health_check_interval_s: int = 10
    log_level: str = "INFO"
    metrics_port: int = 9090

    @classmethod
    def from_yaml(cls, path: str | Path) -> InferGridConfig:
        """Load configuration from a YAML file.

        Args:
            path: Path to the YAML configuration file.

        Returns:
            Populated InferGridConfig instance.
        """
        path = Path(path)
        with open(path) as f:
            raw: dict[str, Any] = yaml.safe_load(f) or {}

        models = [
            ModelConfig(**m) for m in raw.get("models", [])
        ]
        cache_raw = raw.get("cache", {})
        cache_tiers = [
            CacheTierConfig(**t) for t in cache_raw.get("tiers", [])
        ]
        cache = CacheConfig(
            tiers=cache_tiers if cache_tiers else CacheConfig().tiers,
            eviction_frequency_weight=cache_raw.get(
                "eviction_frequency_weight", 0.7
            ),
            eviction_recency_weight=cache_raw.get(
                "eviction_recency_weight", 0.3
            ),
            block_size_tokens=cache_raw.get("block_size_tokens", 16),
        )
        tenant_raw = raw.get("tenant_defaults", {})
        tenant_defaults = TenantDefaults(**tenant_raw) if tenant_raw else TenantDefaults()

        return cls(
            host=raw.get("host", "0.0.0.0"),
            port=raw.get("port", 8080),
            gpu_budget_fraction=raw.get("gpu_budget_fraction", 0.80),
            models=models,
            cache=cache,
            tenant_defaults=tenant_defaults,
            engine_start_timeout_s=raw.get("engine_start_timeout_s", 300),
            health_check_interval_s=raw.get("health_check_interval_s", 10),
            log_level=raw.get("log_level", "INFO"),
            metrics_port=raw.get("metrics_port", 9090),
        )

    @classmethod
    def from_cli_args(
        cls,
        model_ids: list[str],
        gpu_budget: float = 0.80,
        port: int = 8080,
        engine: str = "vllm",
    ) -> InferGridConfig:
        """Build configuration from CLI arguments.

        Args:
            model_ids: List of HuggingFace model identifiers.
            gpu_budget: Fraction of GPU memory to use (0.0-1.0).
            port: API server port.
            engine: Default engine backend ("vllm" or "sglang").

        Returns:
            Populated InferGridConfig instance.
        """
        models = [
            ModelConfig(model_id=mid, engine=engine) for mid in model_ids
        ]
        return cls(
            port=port,
            gpu_budget_fraction=gpu_budget,
            models=models,
        )
