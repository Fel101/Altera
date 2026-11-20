"""
Altera — Provider Registry (Phase B: Model Selector + Multi-Provider Support)

Central registry of LLM providers with presets for LM Studio, Ollama, vLLM, OpenRouter, and custom.
"""

import logging
import requests
from typing import Any
from dataclasses import dataclass

LOGGER = logging.getLogger(__name__)


@dataclass
class ProviderConfig:
    """Configuration for an LLM provider."""
    name: str
    base_url: str
    api_key: str = ""
    models_endpoint: str = "/v1/models"
    default_model: str = ""
    enabled: bool = True

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "base_url": self.base_url,
            "api_key": self.api_key,
            "models_endpoint": self.models_endpoint,
            "default_model": self.default_model,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ProviderConfig":
        return cls(
            name=data.get("name", ""),
            base_url=data.get("base_url", ""),
            api_key=data.get("api_key", ""),
            models_endpoint=data.get("models_endpoint", "/v1/models"),
            default_model=data.get("default_model", ""),
            enabled=data.get("enabled", True),
        )


# Provider presets. A local server serves whatever the person has loaded, so the local presets name no model and the
# setup page asks for one from the server's own list. The cloud presets name one that the service is known to carry.
PROVIDER_PRESETS = {
    "lmstudio": ProviderConfig(
        name="LM Studio",
        base_url="http://localhost:1234/v1",
        api_key="lm-studio",
        models_endpoint="/models",  # Relative to base_url which already has /v1
        default_model="",
        enabled=True,
    ),
    "ollama": ProviderConfig(
        name="Ollama",
        base_url="http://localhost:11434/v1",
        api_key="ollama",
        models_endpoint="/models",
        default_model="",
        enabled=True,
    ),
    "vllm": ProviderConfig(
        name="vLLM",
        base_url="http://localhost:8000/v1",
        api_key="vllm",
        models_endpoint="/models",
        default_model="",
        enabled=True,
    ),
    # Needs an API key from platform.openai.com, which is billed per token and is NOT the same thing as a ChatGPT
    # Plus subscription: Plus covers the chatgpt.com app only and carries no API credential.
    "openai": ProviderConfig(
        name="OpenAI",
        base_url="https://api.openai.com/v1",
        api_key="",
        models_endpoint="/models",
        default_model="gpt-4o-mini",
        enabled=True,
    ),
    # One key, many models, including finetunes made for playing a character. The default is a general open-weight
    # model (Mistral Small 3.2): it answers without a long thinking pass, costs little, and is less likely than a frontier
    # assistant model to refuse the adult levels. Choose another in Settings.
    "openrouter": ProviderConfig(
        name="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        api_key="",
        models_endpoint="/models",
        default_model="mistralai/mistral-small-3.2-24b-instruct",
        enabled=True,
    ),
    "custom": ProviderConfig(
        name="Custom OpenAI-Compatible",
        base_url="",
        api_key="",
        models_endpoint="/v1/models",
        default_model="",
        enabled=True,
    ),
}


class ProviderRegistry:
    """Registry for managing LLM provider configurations."""

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self.providers: dict[str, ProviderConfig] = {}
        self._active_provider_key: str = "lmstudio"
        self._active_model: str = ""
        self._load_from_config()

    def _load_from_config(self) -> None:
        """Load provider configs from config.yaml structure."""
        llm_cfg = self.config.get("llm", {})

        # Get active provider
        self._active_provider_key = llm_cfg.get("provider", "lmstudio")

        # Get active model
        self._active_model = llm_cfg.get("model", "")

        # Load per-provider overrides
        providers_cfg = llm_cfg.get("providers", {})

        for key, preset in PROVIDER_PRESETS.items():
            override = providers_cfg.get(key, {})
            self.providers[key] = ProviderConfig(
                name=preset.name,
                base_url=override.get("base_url", preset.base_url),
                api_key=override.get("api_key", preset.api_key),
                models_endpoint=override.get("models_endpoint", preset.models_endpoint),
                default_model=override.get("default_model", preset.default_model),
                enabled=override.get("enabled", preset.enabled),
            )

        # If model not set, use active provider's default
        if not self._active_model and self._active_provider_key in self.providers:
            self._active_model = self.providers[self._active_provider_key].default_model

    def get_provider(self, key: str) -> ProviderConfig | None:
        """Get provider config by key."""
        return self.providers.get(key)

    def get_active_provider(self) -> ProviderConfig | None:
        """Get the currently active provider config."""
        return self.providers.get(self._active_provider_key)

    def get_active_provider_key(self) -> str:
        """Get the active provider key."""
        return self._active_provider_key

    def get_active_model(self) -> str:
        """Get the currently active model."""
        return self._active_model

    def set_active_provider(self, key: str, model: str | None = None) -> bool:
        """Set the active provider and optionally the model."""
        if key not in self.providers:
            return False
        self._active_provider_key = key
        provider = self.providers[key]
        self._active_model = model or provider.default_model
        self._save_to_config()
        return True

    def set_active_model(self, model: str) -> None:
        """Set the active model (must be available on current provider)."""
        self._active_model = model
        self._save_to_config()

    def list_providers(self) -> list[dict[str, Any]]:
        """List all configured providers with their status."""
        result = []
        for key, provider in self.providers.items():
            result.append({
                "key": key,
                "name": provider.name,
                "base_url": provider.base_url,
                "models_endpoint": provider.models_endpoint,
                "default_model": provider.default_model,
                "enabled": provider.enabled,
                "is_active": key == self._active_provider_key,
                "active_model": self._active_model if key == self._active_provider_key else None,
            })
        return result

    def to_config_dict(self) -> dict:
        """Export current state to config.yaml compatible structure."""
        providers_dict = {}
        for key, provider in self.providers.items():
            providers_dict[key] = provider.to_dict()

        exported = {
            "provider": self._active_provider_key,
            "model": self._active_model,
            "providers": providers_dict,
            # Preserve other llm settings
            "temperature": self.config.get("llm", {}).get("temperature", 0.85),
            "max_tokens": self.config.get("llm", {}).get("max_tokens", 8192),
            "stream": self.config.get("llm", {}).get("stream", True),
        }
        # The optional sampler settings (core/sampling.py) survive a provider switch.
        if isinstance(self.config.get("llm", {}).get("sampling"), dict):
            exported["sampling"] = dict(self.config["llm"]["sampling"])
        return exported

    def _save_to_config(self) -> None:
        """Persist current provider/model selection to config.yaml (core.config owns the file and the cache)."""
        from core.config import get_config, save_config

        full_config = dict(get_config())
        llm_config = self.to_config_dict()
        full_config["llm"] = llm_config
        self.config["llm"] = llm_config
        save_config(full_config)


def fetch_models_from_provider(provider: ProviderConfig, timeout: int = 10) -> list[dict[str, Any]]:
    """Fetch available models from a provider's /v1/models endpoint."""
    if not provider.base_url:
        return []

    url = provider.base_url.rstrip("/") + provider.models_endpoint
    headers = {"Authorization": f"Bearer {provider.api_key}"} if provider.api_key else {}

    try:
        response = requests.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()
        data = response.json()

        # OpenAI-compatible format: { "data": [ { "id": "model-name", ... }, ... ] }
        models = data.get("data", [])
        return [{"id": m.get("id", ""), "object": m.get("object", "model"), "owned_by": m.get("owned_by", "")} for m in models]
    except requests.RequestException as error:
        # Usually just a model server that is not running yet: one line, not a traceback.
        LOGGER.warning("No model list from %s at %s: %s", provider.name, provider.base_url, _reason(error))
        return []
    except (ValueError, AttributeError, TypeError):
        LOGGER.warning("No model list from %s at %s: the reply was not an OpenAI-style model list",
                       provider.name, provider.base_url)
        return []


def _reason(error: requests.RequestException) -> str:
    if isinstance(error, requests.ConnectionError):
        return "nothing is answering there"
    if isinstance(error, requests.Timeout):
        return "it did not answer in time"
    if error.response is not None:
        return f"it answered HTTP {error.response.status_code}"
    return type(error).__name__


def get_provider_registry(config: dict | None = None) -> ProviderRegistry:
    """Factory function to get a ProviderRegistry instance."""
    return ProviderRegistry(config)
