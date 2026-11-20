"""
Altera — Web Search Module (Phase C: Internet Search Capabilities)

Provides web search functionality through multiple providers:
- DuckDuckGo (free, no API key)
- SerpAPI / Google Custom Search (requires API key)
- OpenWeatherMap (weather-specific, free tier)
- Wikipedia (fact-checking, free)
"""

import logging
import re
import requests
from dataclasses import dataclass
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

LOGGER = logging.getLogger(__name__)


@dataclass
class SearchResult:
    """A single search result."""
    title: str
    url: str
    snippet: str
    source: str  # Provider name


@dataclass
class SearchConfig:
    """Configuration for web search."""
    enabled: bool = True
    default_provider: str = "duckduckgo"
    providers: dict[str, dict] = None
    max_results: int = 5
    timeout_seconds: int = 10

    def __post_init__(self):
        if self.providers is None:
            self.providers = {
                "duckduckgo": {"enabled": True},
                "serpapi": {"enabled": False, "api_key": ""},
                "openweathermap": {"enabled": True, "api_key": ""},
                "wikipedia": {"enabled": True},
            }


class WebSearcher:
    """Unified web search interface with multiple provider backends."""

    def __init__(self, config: SearchConfig | None = None):
        self.config = config or SearchConfig()

        # Create session with retry logic
        self._session = requests.Session()
        retry = Retry(
            total=2,
            backoff_factor=1,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "POST"]
        )
        adapter = HTTPAdapter(max_retries=retry)
        self._session.mount('https://', adapter)
        self._session.mount('http://', adapter)

        # Simple in-memory cache
        self._cache: dict[str, list[SearchResult]] = {}
        self._cache_ttl = 300  # 5 minutes
        self._cache_timestamps: dict[str, float] = {}

    def _get(self, provider, url, *, params, headers=None):
        return self._session.get(url, params=params, headers=headers, timeout=self.config.timeout_seconds)

    def search(self, query: str, provider: str | None = None, max_results: int | None = None) -> list[SearchResult]:
        """Search the web using the specified or default provider."""
        if not self.config.enabled:
            return []

        provider = provider or self.config.default_provider
        max_results = max_results or self.config.max_results

        # Check if provider is enabled
        if not isinstance(provider, str) or provider not in {"duckduckgo", "serpapi", "openweathermap", "wikipedia"}:
            return []
        provider_config = self.config.providers.get(provider, {})
        if not isinstance(provider_config, dict) or not provider_config.get("enabled", False):
            return []

        # Check cache
        cache_key = f"{provider}:{query}:{max_results}"
        import time
        now = time.time()
        if cache_key in self._cache:
            if now - self._cache_timestamps.get(cache_key, 0) < self._cache_ttl:
                LOGGER.debug("Web search cache hit for provider %s", provider)
                return self._cache[cache_key]

        try:
            if provider == "duckduckgo":
                results = self._search_duckduckgo(query, max_results)
            elif provider == "serpapi":
                results = self._search_serpapi(query, max_results)
            elif provider == "openweathermap":
                results = self._search_openweathermap(query, max_results)
            elif provider == "wikipedia":
                results = self._search_wikipedia(query, max_results)
            else:
                results = []
        except Exception:
            # Network exceptions may include the full query URL and API key.
            LOGGER.warning("Search failed for provider %s.", provider)
            results = []

        # Cache results
        if results:
            self._cache[cache_key] = results
            self._cache_timestamps[cache_key] = now
            # Limit cache size
            if len(self._cache) > 100:
                oldest_key = min(self._cache_timestamps, key=self._cache_timestamps.get)
                del self._cache[oldest_key]
                del self._cache_timestamps[oldest_key]

        return results

    def _search_duckduckgo(self, query: str, max_results: int) -> list[SearchResult]:
        """Search using DuckDuckGo HTML scraping."""
        url = "https://html.duckduckgo.com/html/"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }

        response = self._get('duckduckgo', url, params={'q': query}, headers=headers)
        response.raise_for_status()

        if BeautifulSoup is None:
            raise ImportError("BeautifulSoup is required for DuckDuckGo search. Please install 'bs4'.")

        soup = BeautifulSoup(response.text, "html.parser")

        results = []
        # Try modern DDG selectors first, fall back to legacy
        for result in soup.select('[data-testid="result"], .result')[:max_results]:
            title_elem = result.select_one('[data-testid="result-title-a"], .result__title')
            snippet_elem = result.select_one('[data-testid="result-snippet"], .result__snippet')

            if title_elem:
                title = title_elem.get_text(strip=True)
                url = title_elem.get("href", "") if title_elem.name == "a" else ""
                snippet = snippet_elem.get_text(strip=True) if snippet_elem else ""
                results.append(SearchResult(
                    title=title,
                    url=url,
                    snippet=snippet,
                    source="duckduckgo"
                ))

        return results

    def _search_serpapi(self, query: str, max_results: int) -> list[SearchResult]:
        """Search using SerpAPI (Google search)."""
        api_key = self.config.providers.get("serpapi", {}).get("api_key", "")
        if not api_key:
            return []

        params = {
            "q": query,
            "api_key": api_key,
            "num": max_results,
            "engine": "google"
        }

        response = self._get('serpapi', "https://serpapi.com/search", params=params)
        response.raise_for_status()
        data = response.json()

        results = []
        for item in data.get("organic_results", [])[:max_results]:
            results.append(SearchResult(
                title=item.get("title", ""),
                url=item.get("link", ""),
                snippet=item.get("snippet", ""),
                source="serpapi"
            ))

        return results

    def _search_openweathermap(self, query: str, max_results: int) -> list[SearchResult]:
        """Search for weather using OpenWeatherMap."""
        api_key = self.config.providers.get("openweathermap", {}).get("api_key", "")
        if not api_key:
            # Try to extract location from query and use a free approach
            return self._search_weather_fallback(query, max_results)

        # Extract location from query (simple heuristic)
        location = self._extract_location(query)
        if not location:
            return []

        # Current weather
        url = "https://api.openweathermap.org/data/2.5/weather"
        params = {"q": location, "appid": api_key, "units": "metric"}

        response = self._get('openweathermap', url, params=params)
        if response.status_code != 200:
            return []

        data = response.json()
        weather = data.get("weather", [{}])[0]
        main = data.get("main", {})
        wind = data.get("wind", {})

        title = f"Weather in {location}"
        snippet = (
            f"{weather.get('description', '').title()}, "
            f"{main.get('temp', 0):.1f}°C (feels like {main.get('feels_like', 0):.1f}°C), "
            f"Humidity: {main.get('humidity', 0)}%, "
            f"Wind: {wind.get('speed', 0)} m/s"
        )

        return [SearchResult(
            title=title,
            url=f"https://openweathermap.org/city/{data.get('id', '')}",
            snippet=snippet,
            source="openweathermap"
        )]

    def _search_weather_fallback(self, query: str, max_results: int) -> list[SearchResult]:
        """Fallback weather search without API key - use DuckDuckGo."""
        return self.search(f"weather {query}", provider="duckduckgo", max_results=max_results)

    def _extract_location(self, query: str) -> str | None:
        """Extract location from weather query."""
        # Simple patterns for location extraction
        patterns = [
            r"weather in (.+)",
            r"what'?s the weather (?:in|for|at) (.+)",
            r"forecast (?:for|in) (.+)",
        ]
        query_lower = query.lower()
        for pattern in patterns:
            match = re.search(pattern, query_lower)
            if match:
                return match.group(1).strip().rstrip("?.")
        return None

    def _search_wikipedia(self, query: str, max_results: int) -> list[SearchResult]:
        """Search Wikipedia."""
        # Use Wikipedia opensearch API
        params = {
            "action": "opensearch",
            "search": query,
            "limit": max_results,
            "namespace": 0,
            "format": "json"
        }

        response = self._get('wikipedia', "https://en.wikipedia.org/w/api.php", params=params)
        response.raise_for_status()
        data = response.json()

        results = []
        titles = data[1] if len(data) > 1 else []
        snippets = data[2] if len(data) > 2 else []
        urls = data[3] if len(data) > 3 else []

        for i, title in enumerate(titles[:max_results]):
            snippet = snippets[i] if i < len(snippets) else ""
            url = urls[i] if i < len(urls) else f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}"
            results.append(SearchResult(
                title=title,
                url=url,
                snippet=snippet,
                source="wikipedia"
            ))

        return results


def create_web_searcher(config: dict | None = None) -> WebSearcher:
    """Factory function to create WebSearcher from config dict."""
    if not config:
        return WebSearcher()

    ws_config = config.get("web_search", config) or config.get("web_search", {})
    search_config = SearchConfig(
        enabled=ws_config.get("enabled", True),
        default_provider=ws_config.get("default_provider", "duckduckgo"),
        providers=ws_config.get("providers", {}),
        max_results=ws_config.get("max_results", 5),
        timeout_seconds=ws_config.get("timeout_seconds", 10),
    )
    return WebSearcher(search_config)
