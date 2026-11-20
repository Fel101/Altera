"""Read-only, bounded dependency checks. No completion or embedding is generated."""
import hashlib
import logging
import threading
import time

LOGGER = logging.getLogger(__name__)


class HealthService:
    def __init__(self):
        self.lock, self.cache = threading.Lock(), None

    def check(self, session_id=None):
        from core.config import get_config
        from core.providers import get_provider_registry
        config = get_config()
        registry = get_provider_registry(config)
        provider, model = registry.get_active_provider(), registry.get_active_model()
        identity = (provider.base_url if provider else '', bool(provider and provider.enabled), model, hashlib.sha256(str(provider.api_key or '').encode()).hexdigest() if provider else '',
                    repr(config.get('voice')))
        with self.lock:
            if self.cache and self.cache[0] == identity and time.monotonic() - self.cache[1] < 10:
                llm = self.cache[2]
            else:
                llm = self._model(provider, model)
                self.cache = (identity, time.monotonic(), llm)
        result = {'llm': llm, 'checked_at': time.time()}
        voice = config.get('voice') or {}
        from core.profile import load_user_profile
        preference = (load_user_profile().get('preferences') or {}).get('voice_enabled', True)
        if not voice.get('tts_enabled'):
            result['voice'] = {'status': 'disabled', 'message': 'Speech is disabled in config.'}
        elif not preference:
            result['voice'] = {'status': 'disabled', 'message': 'Speech is switched off in Settings.'}
        else:
            from core import voice_service, tts
            state = voice_service.status(ensure_started=False)
            cfg = tts.settings(config)
            result['voice'] = {'status': voice_service.light(state['state']), 'message': state['message'],
                               'engine': cfg['engine'], 'cloud_fallback_enabled': cfg['cloud_fallback']}
            if cfg['cloud_fallback']:
                result['voice']['message'] += ' Cloud speech fallback is enabled.'
        try:
            from core.memory import get_memory_manager
            count = get_memory_manager().store.collection.count()
            if type(count) is not int or count < 0:
                raise ValueError('Invalid memory count')
            result['memory'] = {'status': 'ok', 'message': f'Memory store is readable ({count} entries).', 'count': count}
            if session_id:
                from core.datastore import get_datastore
                from core.knowledge import dirty
                store = get_datastore()
                info = store.get_session(session_id)
                if info and dirty(store, info.avatar_id or ''):
                    result['memory'].update(status='warning', message=f'{count} entries stored; derived knowledge for this companion is paused. Review memories to rebuild it.')
        except Exception:
            LOGGER.exception('The memory store could not be read for the health check')
            result['memory'] = {'status': 'error', 'message': 'Memory store could not be read.'}
        return result

    @staticmethod
    def _model(provider, model):
        if not provider or not provider.enabled:
            return {'status': 'disabled', 'message': 'No enabled provider is selected.'}
        client = None
        try:
            from openai import OpenAI
            client = OpenAI(base_url=provider.base_url, api_key=provider.api_key or 'not-needed', timeout=2., max_retries=0)
            response = client.models.list()
            models = [item.id for item in response.data]
            available = bool(model and model in models)
            return {'status': 'ok' if available else 'warning', 'reachable': True, 'model_listed': available,
                    'model': model, 'message': f'{model} is listed; inference has not been tested.' if available else
                    'Provider is reachable. Select a listed model in Settings.'}
        except Exception as error:
            return {'status': 'error', 'reachable': True if getattr(error, 'status_code', None) else None, 'model': model,
                    'message': 'Provider refused the configured credentials.' if getattr(error, 'status_code', None) in {401, 403} else
                    'Provider check failed. Verify its server, endpoint and credentials.'}
        finally:
            if client:
                try:
                    client.close()
                except Exception:
                    pass
