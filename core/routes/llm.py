"""
LLM Provider/Model Management Routes (Phase B: Model Selector + Multi-Provider Support)

Routes for managing LLM providers, models, and switching.
"""

from flask import Blueprint, request, jsonify
import logging

from core.constants import sanitized_error_response
from core.llm import get_provider_registry_instance
from core.providers import fetch_models_from_provider

bp = Blueprint('llm', __name__, url_prefix='/api/llm')
LOGGER = logging.getLogger(__name__)


def _internal_error():
    payload, status = sanitized_error_response("internal_error", 500)
    return jsonify(payload), status


@bp.route("/providers", methods=["GET"])
def list_providers():
    """List all configured providers with status."""
    try:
        registry = get_provider_registry_instance()
        providers = []

        for key, provider in registry.providers.items():
            providers.append({
                "key": key,
                "name": provider.name,
                "base_url": provider.base_url,
                "models_endpoint": provider.models_endpoint,
                "default_model": provider.default_model,
                "enabled": provider.enabled,
                "is_active": key == registry._active_provider_key,
                "active_model": registry.get_active_model() if key == registry._active_provider_key else None,
            })

        return jsonify({
            "providers": providers,
            "active_provider": registry._active_provider_key,
            "active_model": registry.get_active_model()
        })
    except Exception:
        LOGGER.exception("Error listing providers")
        return _internal_error()


@bp.route("/providers/<provider_key>/models", methods=["GET"])
def list_models(provider_key):
    """Fetch available models from a provider."""
    try:
        registry = get_provider_registry_instance()
        provider = registry.providers.get(provider_key)

        if not provider:
            return jsonify({"error": f"Unknown provider: {provider_key}"}), 400

        return jsonify({"provider": provider_key, "models": fetch_models_from_provider(provider)})
    except Exception:
        LOGGER.exception("Error listing models for %s", provider_key)
        return _internal_error()


@bp.route("/active", methods=["GET"])
def get_active():
    """Get current active provider + model."""
    try:
        registry = get_provider_registry_instance()
        active_provider = registry.get_active_provider()

        return jsonify({
            "provider": registry._active_provider_key,
            "model": registry.get_active_model(),
            "provider_name": active_provider.name if active_provider else None,
            "base_url": active_provider.base_url if active_provider else None,
        })
    except Exception:
        LOGGER.exception("Error getting active provider")
        return _internal_error()


@bp.route("/switch", methods=["POST"])
def switch_provider():
    """Switch provider and/or model."""
    try:
        # core/chat.py owns the engine, so this no longer reaches back into altera.py (that import cycle was why
        # each layer ended up with a chat engine of its own).
        from core.chat import refresh_chat_engine

        data = request.json or {}
        provider_key = data.get("provider")
        model = data.get("model")

        if not provider_key:
            return jsonify({"error": "provider is required"}), 400

        registry = get_provider_registry_instance()

        # Validate provider exists
        if provider_key not in registry.providers:
            return jsonify({"error": f"Unknown provider: {provider_key}"}), 400

        # Switch provider
        if not registry.set_active_provider(provider_key, model):
            return jsonify({"error": f"Failed to switch to provider: {provider_key}"}), 500

        # Rebuild the one shared engine: it re-reads config.yaml, refreshes the provider registry and the LLM client
        refresh_chat_engine()

        # Get fresh registry after refresh
        fresh_registry = get_provider_registry_instance()
        active_provider = fresh_registry.get_active_provider()
        return jsonify({
            "status": "ok",
            "provider": fresh_registry._active_provider_key,
            "model": fresh_registry.get_active_model(),
            "provider_name": active_provider.name if active_provider else None,
        })
    except Exception:
        LOGGER.exception("Error switching provider")
        return _internal_error()


@bp.route("/sampling", methods=["GET"])
def get_sampling():
    """Temperature, reply length and the optional sampler settings (core/sampling.py), and which this provider is not sent."""
    from core import sampling
    from core.config import get_config
    registry = get_provider_registry_instance()
    return jsonify(sampling.describe(get_config(), registry.get_active_provider_key())), 200, {"Cache-Control": "no-store"}


@bp.route("/sampling", methods=["PUT"])
def put_sampling():
    """{temperature?, max_tokens?, top_p?, min_p?, repetition_penalty?, presence_penalty?, frequency_penalty?}. An optional
    setting sent as null is cleared, so it is not sent at all. Saved to config.yaml; the next reply uses it."""
    from core import sampling
    from core.chat import refresh_chat_engine
    from core.config import get_config, save_config
    config = dict(get_config())
    try:
        config["llm"] = sampling.apply_app_changes(config.get("llm") or {}, request.get_json(silent=True))
    except sampling.SamplingError as error:
        return jsonify({"error": str(error)}), error.status, {"Cache-Control": "no-store"}
    save_config(config)
    refresh_chat_engine()
    registry = get_provider_registry_instance()
    return jsonify(sampling.describe(get_config(), registry.get_active_provider_key())), 200, {"Cache-Control": "no-store"}


@bp.route("/test-connection", methods=["POST"])
def test_connection():
    """Test connection to a provider."""
    try:
        data = request.json or {}
        provider_key = data.get("provider")
        base_url = data.get("base_url")
        api_key = data.get("api_key")

        if not provider_key:
            return jsonify({"error": "provider is required"}), 400

        registry = get_provider_registry_instance()
        provider = registry.providers.get(provider_key)

        test_url = base_url or (provider.base_url if provider else None)
        test_key = api_key or provider.api_key if provider else None

        if not test_url:
            return jsonify({"success": False, "error": "No base URL provided"})

        try:
            from openai import OpenAI
            client = OpenAI(base_url=test_url, api_key=test_key or "test")
            models = client.models.list()
            return jsonify({
                "success": True,
                "models_count": len(models.data),
                "models": [m.id for m in models.data[:10]],  # Return first 10
            })
        except Exception as e:
            LOGGER.info("Connection test failed: %s", e)
            return jsonify({"success": False, "error": "Connection test failed"})
    except Exception:
        LOGGER.exception("Error testing connection")
        return _internal_error()


@bp.route("/config", methods=["PUT"])
def update_provider_config():
    """Update provider configuration (for custom provider)."""
    try:
        data = request.json or {}
        provider_key = data.get("provider", "custom")

        registry = get_provider_registry_instance()

        if provider_key not in registry.providers:
            return jsonify({"error": f"Unknown provider: {provider_key}"}), 400

        provider = registry.providers[provider_key]

        # Update provider config
        if "base_url" in data:
            provider.base_url = data["base_url"]
        if "api_key" in data:
            provider.api_key = data["api_key"]
        if "default_model" in data:
            provider.default_model = data["default_model"]
        if "enabled" in data:
            provider.enabled = data["enabled"]

        # Save to config.yaml
        registry._save_to_config()

        return jsonify({"status": "ok", "provider": provider_key})
    except Exception:
        LOGGER.exception("Error updating provider config")
        return _internal_error()
