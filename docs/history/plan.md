# Altera — historical project plan

> Historical design and implementation notes, retained for context. Configuration examples, file layouts, phase statuses and test counts below describe earlier versions and are not current setup instructions. For the current app use [the quickstart](../../README.md), [user guide](../README.md), [architecture](../ARCHITECTURE.md), [API reference](../API_REFERENCE.md) and [completed polish record](POLISH_PLAN.md). Reviewed 2026-10-26.

A fully local, private AI companion with personality, memory, voice, and emotional intelligence. Everything runs on your machine — no cloud APIs, no subscriptions, no data leaves your computer.

---

## Tech Stack (All Local)

| Component | Choice | Notes |
|-----------|--------|-------|
| LLM Backend | LM Studio (localhost:1234) | qwen3.6-35b-a3b |
| Framework | Python + Flask | Lightweight, familiar |
| Memory | ChromaDB (local vector DB) | Semantic search on disk |
| Speech-to-Text | faster-whisper (base model) | Local STT |
| Text-to-Speech | edge-tts (en-US-AriaNeural) | Free Microsoft TTS |
| Personality | System prompt + memory injection | Evolvable over time |

---

## Completed Phases

### ✅ Phase 1: Core Chat Engine & Personality

- Flask app at `localhost:5001` with streaming chat responses
- Personality-driven system prompt from `personality.yaml`
- Dark/light theme toggle (built-in)
- SQLite conversation storage (`data/conversations.db`)
- Typing indicator animation

### ✅ Phase 2: Memory System

- ChromaDB persistent vector store for semantic memory search
- Auto-extraction of facts, preferences, events from conversations
- Confidence scoring with fade-over-time for low-confidence memories
- Memory management UI at `/memory` — view/edit/delete entries
- Top-5 relevant memories injected into system prompt per response
- `user_profile.yaml` — structured facts about you (auto + manual)

### ✅ Phase 3: Voice Layer

- Local STT via faster-whisper (base model, configurable)
- TTS via edge-tts with en-US-AriaNeural voice
- Microphone button in sidebar with waveform visualization
- Auto-playback of Altera's responses
- TTS toggle in Settings panel

### ✅ Phase 4: Emotional Intelligence & Proactive Behavior

- Mood detection (positive/negative/neutral) from message content
- Mood trend tracking over configurable time window
- Emotional memory stored alongside factual memories
- Proactive check-in system with configurable interval
- Follow-up question generation based on past conversations
- Personality evolution — tracks communication style over time
- Relationship graph — extracts people and places from conversations
- Timeline view at `/timeline` — conversation history with mood annotations

### ✅ Phase 5: Polish & Deployment

- Mobile hamburger menu + bottom navigation bar
- Avatar breathing/glow/typing animations
- Spring-like staggered message slide-in animations
- Dark/light theme toggle (persisted in browser)
- 6 accent colors (Purple, Blue, Green, Pink, Orange, Teal) — persisted per-browser
- Privacy controls: export all data as JSON / delete everything (double-confirm)
- Comprehensive README with setup guide, config reference, troubleshooting

---

## Phase 6: Onboarding & First-Time Experience

**Goal**: Make the first interaction feel magical. Right now Altera just says "Hey there!" — she should know who you are from day one and set up herself.

### Steps

1. **Welcome flow / onboarding wizard**
   - First visit shows a guided setup instead of empty chat
   - Ask for your name, interests, communication preferences
   - Auto-populate `user_profile.yaml` with answers
   - Let you pick Altera's personality style (warm/casual vs professional/structured)
   - One-time voice setup: record a sample or choose TTS voice

2. **First conversation starter**
   - After onboarding, Altera opens with a personalized greeting using your name and interests
   - "Hey [name]! I noticed you're into [interests]. What would you like to talk about?"
   - Context-aware icebreakers based on time of day

3. **Empty state improvements**
   - Instead of blank chat, show 3-4 suggested conversation starters
   - Rotate suggestions so they don't get stale
   - "Try asking me about..." with varied topics (memories, personality, voice)

4. **Feature discovery**
   - Subtle tooltips or hints for new features (voice, memory, timeline)
   - First-time tips: "You can talk to me!" when microphone is available
   - Don't overwhelm — one hint at a time

5. **Configuration wizard**
   - Optional guided setup for LM Studio connection, model selection
   - Auto-detect if LM Studio is running and connected
   - Show status indicators (LLM: ✅/❌, Voice: ✅/❌, Memory: ✅/❌)

### Deliverables
- Onboarding wizard for first-time visitors
- Personalized greeting using user profile data
- Suggested conversation starters on empty state
- Feature discovery hints (non-intrusive)
- System health indicators in UI

---

## Phase 7: PWA & Offline Support

**Goal**: Install Altera as an app on your phone or desktop. Works without internet.

### Steps

1. **PWA manifest**
   - `manifest.json` with app name, icons, theme colors
   - Service worker for offline caching of HTML/CSS/JS
   - "Add to Home Screen" prompt on mobile browsers

2. **Offline mode**
   - Cache all static assets via service worker
   - Queue messages when offline, send when reconnected
   - Show offline indicator in UI
   - Graceful degradation: if LM Studio is unreachable, show friendly message

3. **App-like experience**
   - Full-screen mode (no browser chrome)
   - Splash screen on launch
   - Haptic feedback on mobile for send/record actions
   - Push notification support (optional, requires backend)

4. **Desktop install**
   - Chrome/Edge: "Install Altera" banner in address bar
   - Windows: creates Start Menu shortcut + desktop icon automatically via PWA

### Deliverables
- Installable PWA with manifest.json and service worker
- Offline message queuing and sync on reconnect
- Mobile home screen icon + full-screen mode
- Desktop install support (Chrome/Edge)

---

## Phase 8: Advanced Memory & Knowledge

**Goal**: Altera becomes genuinely knowledgeable about you — not just remembering words, but understanding patterns.

### Steps

1. **Memory deduplication & consolidation**
   - Detect when multiple memories say the same thing
   - Merge and update confidence scores automatically
   - "You mentioned X before" → consolidate into single high-confidence entry

2. **Event timeline / life journal**
   - Extract significant events from conversations (birthdays, achievements, trips)
   - Chronological timeline of your life as Altera knows it
   - "On your birthday last month you said..." — proactive references

3. **Relationship depth scoring**
   - Track how often you mention each person/place
   - Update relationship closeness scores over time
   - "You haven't mentioned Sarah in a while — is everything okay?"

4. **Memory export formats**
   - JSON (current) + Markdown for readability
   - PDF summary of your life as Altera knows it
   - Import from existing notes/contacts

5. **Conversation search**
   - Full-text search across all conversations
   - Filter by date range, mood, topic keywords
   - "Show me everything we talked about last week"

### Deliverables
- Memory deduplication and consolidation engine
- Life event timeline extraction
- Relationship depth scoring
- Multiple export formats (JSON, Markdown, PDF)
- Conversation search with filters

---

## Phase 9: Multi-Conversation & Context Management

**Goal**: Better organization when you have many conversations going on.

### Steps

1. **Smart session naming**
   - Auto-generate descriptive names from conversation content
   - "Work stress" instead of "Default Chat" or date-based names
   - Manual rename capability in sidebar

2. **Conversation tags / categories**
   - Tag sessions: work, personal, creative, advice, etc.
   - Filter sidebar by tag
   - Auto-tag based on conversation content analysis

3. **Session merge/split**
   - Merge two related conversations into one thread
   - Split a long session at a natural break point
   - Preserve message ordering and timestamps

4. **Archive / hide old sessions**
   - Archive sessions older than N days or with low activity
   - Hidden sidebar section for archived sessions
   - Search still finds archived content

5. **Session sharing (local)**
   - Export a single conversation as Markdown
   - Share specific threads without exposing everything else
   - Import shared conversations back into Altera

### Deliverables
- Auto-naming of conversations from content analysis
- Tagging system with sidebar filtering
- Session merge/split/archive capabilities
- Single-conversation export and import

---

## Phase 10: Personality Deepening & Customization

**Goal**: Make Altera feel even more like a real person — with deeper personality, humor, and emotional range.

### Steps

1. **Personality depth layers**
   - Core traits (current) + mood-dependent variations
   - "She's playful when you're in a good mood, serious when you need advice"
   - Time-of-day personality shifts (morning energy vs evening calm)

2. **Inside joke system**
   - Track recurring topics and build shared references
   - Subtle callbacks to past conversations ("Like that time we...")
   - Evolve jokes naturally without forcing them

3. **Opinion formation**
   - Altera develops mild opinions on topics you discuss often
   - Not strong beliefs — just personality-consistent perspectives
   - Can change when presented with new information

4. **Advanced humor detection**
   - Recognize when you're joking vs serious
   - Match your humor style (sarcastic, dry, playful)
   - Know when to be funny and when to be supportive

5. **Personality templates**
   - Pre-built personality packs: "supportive friend", "curious interviewer", "creative collaborator"
   - Mix-and-match traits via config UI
   - Save/load custom personalities

### Deliverables
- Mood-dependent personality variations
- Inside joke tracking and natural callbacks
- Mild opinion formation on discussed topics
- Humor style matching
- Personality template system

---

## File Structure (historical snapshot)

```
Altera/
├── plan.md              # This file — project roadmap
├── README.md            # Setup guide, config reference, troubleshooting
├── requirements.txt     # Python dependencies
├── config.yaml          # App configuration (port, model, voice, privacy)
├── personality.yaml     # Altera's personality definition
├── user_profile.yaml    # Facts about you (auto + manual)
├── altera.py               # Main Flask application
├── core/
│   ├── chat.py          → ChatEngine + ChatContext (conversation flow)
│   ├── llm.py           → LLMClient (OpenAI wrapper + retry logic)
│   ├── providers.py     → Provider registry + multi-provider support
│   ├── prompts.py       → PromptBuilder + section classes
│   ├── datastore.py     → DataStore (SQLite accessors)
│   ├── memory.py        → ChromaDB memory manager + dedup
│   ├── emotion.py       → Emotion detection + mood tracking
│   ├── proactive.py     → Follow-up tracker + check-in gen
│   ├── relationships.py → Entity graph + personality evo
│   ├── events.py        → Life event extraction + timeline
│   ├── search.py        → FTS5 conversation search engine
│   ├── web_search.py    → Internet search (DuckDuckGo, etc.)
│   ├── export.py        → JSON/Markdown/PDF data export
│   ├── jokes.py         → Inside joke & reference tracking
│   ├── opinions.py      → Opinion tracking + humor matching
│   ├── templates.py     → Personality templates + mood adj.
│   ├── stt.py           → Speech-to-text (Whisper)
│   ├── tts.py           → Text-to-speech (edge-tts)
│   ├── profile.py       → User profile management
│   ├── config.py        → Shared LLM configuration
│   └── constants.py     → App-wide constants, error classes
├── data/
│   ├── conversations.db # SQLite conversation storage (5 tables)
│   └── chroma_db/       # ChromaDB persistent vector store
├── static/
│   ├── css/style.css    # All styles
│   ├── js/app.js        # Frontend logic
│   └── audio/tts/       # Cached TTS audio files
├── templates/
│   ├── index.html       # Main chat interface
│   ├── memory.html      # Memory management page
│   ├── timeline.html    # Conversation timeline with mood annotations
│   ├── knowledge.html   # Knowledge & dedup management
│   └── search.html      # Conversation search
└── tests/
```

---

## Design Principles

- **Privacy first**: Everything runs locally, no data leaves your machine
- **Modular**: Each component (LLM, memory, voice) can be swapped independently
- **Configurable**: Personality, voice, behavior all adjustable via YAML files
- **Transparent**: You can see and edit everything Altera remembers about you
- **Free**: No API keys, no subscriptions, no cloud costs

---

## Status at the time of the original plan

**Phase 6 COMPLETE.** Onboarding wizard, conversation starters, feature hints, and health indicators all verified (37/37 checks passed).

**Phase 7 COMPLETE.** PWA & Offline Support built: manifest.json with 6 icon sizes, service worker with cache-first/network-first strategies, IndexedDB offline message queue, install prompt support, offline indicator UI. Ready for browser testing.

**Phase 8 COMPLETE.** Advanced Memory & Knowledge built and verified:
- ✅ Memory deduplication engine (Jaccard similarity + substring matching) with review UI — scan, merge individual groups or all at once
- ✅ Life event timeline extraction (pattern-based + LLM-enhanced) for birthdays, achievements, trips, work, relationships, health, holidays
- ✅ Relationship depth scoring (mention frequency + recency bonus + context richness) with proactive insights ("You haven't mentioned X in Y days")
- ✅ Multi-format export: JSON, Markdown report, PDF/HTML summary — all with status feedback UI
- ✅ Conversation search with FTS5 full-text search, mood/date/session filters
- ✅ Knowledge page at /knowledge with stats grid, dedup section, and export status
- ✅ Search page at /search with quick filters (happy moments, stressed moments, people, places)
- Bug fix: Added `get_memory_manager()` function to core/memory.py (was missing, causing import errors on dedup APIs)

**Phase 9 COMPLETE.** Multi-Conversation & Context Management built and verified:
- ✅ Smart session naming — auto-generate descriptive names from conversation content via `/api/sessions/auto-name/<id>`
- ✅ Conversation tags/categories — tag sessions with work/personal/creative/advice, filter sidebar by tag (stored as JSON in `tags` column)
- ✅ Session archive / hide old sessions — archive or unarchive sessions, toggle view between active and archived via "Show Archived" button
- ✅ Single-conversation export to Markdown — download individual conversation threads with timestamps
- ✅ Session merge capability — merge another session into the current session (target session is archived after merge)
- ✅ Database schema updated to include `tags` and `archived` columns in sessions table, plus FTS5 virtual table for message search

**Phase 10 COMPLETE.** Personality Deepening & Customization built and verified:
- ✅ Mood-dependent personality variations — adjusts responses based on user's detected emotion (happy, sad, anxious, excited, etc.) with intensity scaling
- ✅ Inside joke system — tracks recurring topics, inside jokes, and shared references from past conversations to provide natural callbacks
- ✅ Mild opinion formation — AI develops consistent perspectives on frequently discussed topics that evolve over time
- ✅ Humor style matching — recognizes when user is joking vs serious and matches their humor style (sarcastic, dry, playful, witty, observational)
- ✅ Personality template system — pre-built personality packs ("supportive friend", "curious interviewer", "creative collaborator") with ability to save/load custom personalities
- ✅ API endpoints for all Phase 10 features: `/api/personality/templates`, `/api/jokes/callbacks`, `/api/humor/style`, `/api/opinions/user`, and more
- ✅ Updated system prompt integration to include inside joke callbacks, humor style matching, and mood-dependent personality variations

**Next recommended phase:** Future enhancements or user-requested features based on the completed roadmap.

Phase 11. Session Persistence — Return to Previous Session ✅ COMPLETED

### Goal
When the user opens Altera (new browser tab, page reload, or new chat session), automatically resume the most recent active conversation instead of creating a blank new chat.

### Implementation Summary

#### Backend Changes
1. **Added `last_active_session` tracking** in `core/datastore.py`:
   - Added `LAST_ACTIVE_SESSION` to `ProfileKeys` class
   - Added `get_last_active_session()` and `set_last_active_session(session_id)` methods

2. **New endpoint: `GET /api/sessions/last-active`** in `core/routes/sessions.py`:
   - Returns the most recent non-archived session ID and name
   - Returns `null` if none exists

3. **New endpoint: `PUT /api/profile/last-session`** in `core/routes/core_api.py`:
   - Updates the last active session ID when user switches sessions

#### Frontend Changes (`static/js/app.js`)
1. **On page load**: Added `loadLastActiveSession()` called in `DOMContentLoaded`:
   - Calls `GET /api/sessions/last-active`
   - If session exists → sets `currentSessionId`, loads messages
   - If no session → shows welcome screen (original behavior)

2. **On session switch**: `switchSession()` now calls `PUT /api/profile/last-session` to persist the active session

3. **New chat button**: `createNewChat()` creates a fresh session (doesn't resume)

#### Files Modified
- `core/datastore.py` — Added profile key and accessor methods
- `core/routes/sessions.py` — Added `/api/sessions/last-active` endpoint
- `core/routes/core_api.py` — Added `/api/profile/last-session` endpoint
- `static/js/app.js` — Added `loadLastActiveSession()`, updated `switchSession()`

---

Phase 12. Internet Search Capabilities ✅ COMPLETED

### Goal
Enable Altera to search the web for real-time information: weather, current events, fact-checking, general knowledge queries.

### Architecture
- **Local-first**: All LLM inference, memory, voice remain local
- **Web search as opt-in tool**: LLM calls `web_search` function when needed
- **Multiple free providers**: DuckDuckGo, Wikipedia, OpenWeatherMap (optional key), SerpAPI (optional key)
- **User controls**: Enable/disable in Settings

### Implementation Summary

#### Backend Changes

1. **New module: `core/web_search.py`**:
   - `SearchResult` dataclass: `{title, url, snippet, source}`
   - `SearchConfig` dataclass for configuration
   - `WebSearcher` class with multiple providers:
     - **DuckDuckGo** — HTML scraping (free, no key)
     - **SerpAPI** — Google search (requires API key)
     - **OpenWeatherMap** — Weather-specific (free tier, optional key)
     - **Wikipedia** — OpenSearch API (free)
   - Unified `search(query, provider?, max_results?)` interface

2. **Function Calling Integration in `core/chat.py`**:
   - Added `WEB_SEARCH_TOOL` schema (OpenAI-compatible function definition)
   - Modified `_call_llm()` and `_call_llm_stream()` to pass `tools=[WEB_SEARCH_TOOL]`
   - Added `web_searcher` instance initialized from config

3. **Prompt Updates in `core/prompts.py`**:
   - Added `ToolsSection` class describing available tools
   - System prompt now includes guidance on WHEN to search:
     - Current weather, latest news, fact-checking, real-time data
     - NOT for general knowledge, personal conversation, creative tasks

4. **Configuration (`config.yaml`)**:
   ```yaml
   web_search:
     enabled: true
     default_provider: "duckduckgo"
     providers:
       duckduckgo:
         enabled: true
       serpapi:
         enabled: false
         api_key: ""
       openweathermap:
         enabled: true
         api_key: ""
       wikipedia:
         enabled: true
     max_results: 5
     timeout_seconds: 10
   ```

5. **ChatEngine Updates**:
   - `PromptBuilder` now receives `config` to build `ToolsSection`
   - Uses `get_prompt_builder()` singleton with config

#### Files Created/Modified
- **Created**: `core/web_search.py`
- **Modified**: `core/chat.py` — Function calling integration, web_searcher init
- **Modified**: `core/prompts.py` — Added ToolsSection, updated PromptBuilder
- **Modified**: `config.yaml` — Added web_search configuration
- **Modified**: `core/llm.py` — `LLMClient.chat()` accepts `tools` parameter

---

Phase 13. LM Studio Model Selector + Multi-Provider Support ✅ COMPLETED

### Goal
Allow users to:
- Switch between models loaded in LM Studio without restarting Altera
- Configure and use other OpenAI-compatible providers (Ollama, vLLM, OpenRouter, custom)
- See available models and select active one from Settings UI

### Implementation Summary

#### Backend Changes

1. **New module: `core/providers.py`** (and integrated into `core/llm.py`):
   - `ProviderConfig` dataclass for provider settings
   - `PROVIDER_PRESETS` dict with 5 presets:
     - `lmstudio` (localhost:1234)
     - `ollama` (localhost:11434)
     - `vllm` (localhost:8000)
     - `openrouter` (api.openrouter.ai)
     - `custom` (user-defined)
   - `ProviderRegistry` class for managing providers, active provider/model

2. **Enhanced `core/llm.py`**:
   - `LLMClient.from_config()` now reads from provider registry
   - Added `refresh_from_config()` for runtime config changes
   - Added `update_config()` for dynamic updates
   - Global `ProviderRegistry` instance with thread-safe access

3. **New API endpoints in `core/routes/llm.py`**:
   - `GET /api/llm/providers` — List all providers with status
   - `GET /api/llm/providers/<key>/models` — Fetch models from provider
   - `GET /api/llm/active` — Get current active provider + model
   - `POST /api/llm/switch` — Switch provider/model: `{provider, model}`
   - `POST /api/llm/test-connection` — Test connection to provider
   - `PUT /api/llm/config` — Update provider config (base_url, api_key, etc.)

4. **Configuration (`config.yaml`)**:
   ```yaml
   llm:
     provider: "lmstudio"
     model: "qwen3.6-35b-a3b"
     temperature: 0.85
     max_tokens: 8192
     stream: true
     providers:
       lmstudio:
         base_url: "http://localhost:1234/v1"
         api_key: "lm-studio"
         enabled: true
       ollama:
         base_url: "http://localhost:11434/v1"
         api_key: "ollama"
         enabled: true
       vllm:
         base_url: "http://localhost:8000/v1"
         api_key: "vllm"
         enabled: true
       openrouter:
         base_url: "https://openrouter.ai/api/v1"
         api_key: ""
         enabled: true
       custom:
         base_url: ""
         api_key: ""
         enabled: true
   ```

5. **Registered new blueprint in `altera.py`**:
   - `from core.routes.llm import bp as llm_bp`
   - `app.register_blueprint(llm_bp)`

#### Frontend Changes

1. **Settings UI in `templates/index.html`**:
   - New "🤖 Model & Provider" section
   - Provider dropdown (LM Studio, Ollama, vLLM, OpenRouter, Custom)
   - "Test Connection" button
   - Model dropdown (populated from provider's `/v1/models`)
   - Custom provider inputs (base URL, API key, default model)
   - "Apply & Save" button
   - Status indicators (🟢 Connected / 🔴 Disconnected / 🟡 Testing...)

2. **JavaScript in `static/js/app.js`**:
   - `loadProviderSettings()` — Fetches providers, populates dropdowns
   - `onProviderChange()` — Switches provider, fetches models
   - `refreshModels()` — Calls `/api/llm/providers/<key>/models`
   - `testProviderConnection()` — Calls test endpoint
   - `applyProviderModel()` — Calls switch endpoint, saves custom config
   - `toggleCustomProviderSection()` — Shows/hides custom fields

#### Files Created/Modified
- **Created**: `core/providers.py` (provider registry logic)
- **Created**: `core/routes/llm.py` (API endpoints)
- **Modified**: `core/llm.py` — Dynamic provider/model switching, singleton registry
- **Modified**: `config.yaml` — New nested provider structure
- **Modified**: `altera.py` — Register llm blueprint
- **Modified**: `templates/index.html` — Settings UI for provider/model
- **Modified**: `static/js/app.js` — Frontend logic for provider/model management

---

## Implementation Priority & Dependencies

| Phase | Feature | Duration | Dependencies | Status |
|-------|---------|----------|--------------|--------|
| A | Session Persistence | ~1-2 days | Independent | ✅ Done |
| B | Model Selector | ~2-3 days | Independent | ✅ Done |
| C | Web Search | ~3-4 days | Depends on B (function calling) | ✅ Done |

---

## File Changes Summary

### New Files
- `core/providers.py` — Provider registry & presets
- `core/web_search.py` — Web search abstraction layer
- `core/routes/llm.py` — LLM provider/model endpoints

### Modified Files
- `config.yaml` — New `llm.providers` structure, `web_search` section
- `core/llm.py` — Dynamic model/provider switching, tool support
- `core/chat.py` — Function calling integration, web_searcher init
- `core/prompts.py` — ToolsSection for system prompt
- `core/datastore.py` — `last_active_session` profile key
- `core/routes/sessions.py` — `/api/sessions/last-active` endpoint
- `core/routes/core_api.py` — `/api/profile/last-session` endpoint
- `altera.py` — Register llm blueprint
- `templates/index.html` — Model/Provider settings UI
- `static/js/app.js` — Session persistence, provider/model management

---

## Testing Checklist

### Session Persistence ✅
- [x] Fresh browser tab → loads last conversation
- [x] Page reload (F5) → same session, messages preserved
- [x] New chat button → creates fresh session
- [x] Sidebar session switch → updates "last active"
- [x] Multiple tabs → each independent, last-write-wins

### Model Selector ✅
- [x] LM Studio: list models, switch, chat works
- [x] Ollama: configure, test connection, switch, chat works
- [x] OpenRouter: add API key, list models, switch, chat works
- [x] Custom provider: enter URL/key, test, switch, chat works
- [x] Config persists across restarts
- [x] Invalid config shows clear error, falls back gracefully

### Web Search ✅
- [x] Weather query "What's the weather in Indianapolis?" → searches, returns current weather
- [x] "Latest news on AI" → searches, returns recent articles
- [x] "Fact check: did X happen?" → searches Wikipedia/news, cites sources
- [x] "Population of Tokyo" → searches Wikipedia, returns answer
- [x] Search disabled in settings → no search, uses training knowledge only
- [x] Citations shown in Altera's response
- [x] Provider fallback (DuckDuckGo → Wikipedia → SerpAPI)

---

## Post-Launch Fixes (Applied After Verification)

### Fix #1: Duplicate ProviderRegistry Removal
**Issue**: `core/llm.py` contained ~200 lines of duplicate `ProviderRegistry`, `ProviderConfig`, `PROVIDER_PRESETS`, `get_provider_registry_instance`, `reset_provider_registry` definitions that clashed with the canonical implementation in `core/providers.py`.

**Fix**: Removed all duplicate code from `core/llm.py`; now imports exclusively from `core.providers`.

### Fix #2: Config Persistence for Provider/Model Changes
**Issue**: `ProviderRegistry._save_active_provider()` and `_save_active_model()` only updated in-memory `self.config`, not `config.yaml`. Changes lost on restart.

**Fix**: 
- Added `_save_to_config()` method in `core/providers.py` that writes full provider state to `config.yaml` using `yaml.safe_dump()`
- Called from `set_active_provider()` and `set_active_model()`
- Added `/api/llm/config` PUT endpoint in `core/routes/llm.py` for custom provider config

### Fix #3: Web Search Reliability Improvements
**Issues**: 
- DuckDuckGo HTML parsing used fragile legacy selectors (`.result`, `.result__title`)
- No retry logic on transient failures (5xx, timeouts)
- No request timeout on DuckDuckGo (used config timeout but `requests.post` default infinite)
- No caching → repeated identical queries hit providers every time

**Fixes** in `core/web_search.py`:
- Added `requests.Session` with `HTTPAdapter` + `Retry` (2 retries, backoff, 5xx status)
- Modern DDG selectors: `[data-testid="result"]`, `[data-testid="result-title-a"]` with legacy fallback
- All providers use session with explicit timeout
- In-memory LRU cache (5 min TTL, max 100 entries) keyed by `provider:query:max_results`

### Fix #4: Function Calling Capability Detection
**Issue**: `WEB_SEARCH_TOOL` passed to ALL LLM calls, even models that don't support function calling (causes errors or ignored tools).

**Fix** in `core/chat.py`:
- Added `_supports_tools` cache flag on `ChatEngine`
- Added `_detect_function_calling_support()` that probes model with a trivial tool call on first use
- Only passes `tools=[WEB_SEARCH_TOOL]` if supported; otherwise `tools=None`

### Fix #5: Session Persistence Race Condition
**Issue**: `DOMContentLoaded` handler called `loadMessages(currentSessionId)` BEFORE `loadLastActiveSession()` resolved, causing messages to load with null session_id then reload.

**Fix** in `static/js/app.js`:
```javascript
// Before (race condition):
const hasLastSession = await loadLastActiveSession();
loadSessions();
loadMessages(currentSessionId);  // currentSessionId might still be null!

// After (sequential):
const hasLastSession = await loadLastActiveSession();
loadSessions();
if (currentSessionId) {
    loadMessages(currentSessionId);
} else {
    clearMessages();  // Show welcome screen
}
```

---

### Fix #6: API Response Structure Mismatch

**Issue**: `GET /api/llm/providers` returned a flat array of providers, but `loadProviderSettings()` in `static/js/app.js` expected an object with `providers`, `active_provider`, and `active_model` fields. This caused the frontend to fail when populating the provider dropdown.

**Fix**: Updated the endpoint to return the expected structure:
```python
return jsonify({
    "providers": providers,
    "active_provider": registry._active_provider_key,
    "active_model": registry.get_active_model()
})
```

### Fix #7: Missing `tools` Parameter in LLMClient

**Issue**: `ChatEngine` passed `tools=WEB_SEARCH_TOOL` to `llm_client.chat()`, but `LLMClient.chat()`, `_chat_with_retry()`, and `_chat_stream()` did not accept this parameter, causing `TypeError` at runtime.

**Fix**: Added `tools=None` parameter to all three methods in `core/llm.py`, passing it to the OpenAI API when provided.

---

### Fix #8: Model Dropdown Not Populated on Load

**Issue**: `loadProviderSettings()` only set the active model as a single option in the dropdown instead of fetching all available models via `refreshModels()`. This resulted in "No models found" being displayed even when the test connection showed 17 models.

**Fix**: Modified `loadProviderSettings()` to call `await refreshModels()` after setting the provider, which fetches the full model list from `/api/llm/providers/<key>/models`. Also preserved the active model selection before the refresh.

---

### Fix #9: Provider Models Endpoint Doubled Path

**Issue**: Provider presets in `core/providers.py` used `models_endpoint: "/v1/models"` but `base_url` already ended with `/v1` (e.g., `http://localhost:1234/v1`), resulting in requests to `http://localhost:1234/v1/v1/models` instead of `http://localhost:1234/v1/models`. This caused `/api/llm/providers/<key>/models` to return empty lists.

**Fix**: Changed `models_endpoint` in all presets from `/v1/models` to `/models` since `base_url` already includes `/v1`.

---

### Fix #10: ChatEngine Holds Stale LLMClient Reference

**Issue**: `ChatEngine.__init__` creates `self.llm_client = LLMClient.from_config(config)` which captures a snapshot of the client at initialization. When `/api/llm/switch` was called, it refreshed the LLMClient singleton but ChatEngine still held the old reference, causing chats to use the original `qwen3.6-35b-a3b` model instead of the newly selected `gemma-4-e4b-uncensored-hauhaucs-aggressive`.

**Fix**: Changed `ChatEngine.llm_client` to a property that always returns the current singleton from `LLMClient.get_instance()`. Added `refresh_chat_engine()` function and `_refresh_chat_engine()` in `altera.py` to refresh both the LLMClient and ChatEngine singletons when switching providers.

---

### Fix #11: Startup Crash on Nested Config Access

**Issue**: `altera.py` startup code and `system_health()` route tried to access `CONFIG['llm']['model']` directly, but the new nested provider config structure uses `CONFIG['llm']['providers']['lmstudio']['default_model']` instead.

**Fix**: Updated both locations to use `get_provider_registry()` to get active model and provider info. Fixed startup print statements and health check endpoint.

---

### Fix #12: config.yaml Nested llm Structure

**Issue**: config.yaml had a duplicate `llm:` key (`llm:\n  llm:\n    provider: ...`) causing the provider registry to fail reading `provider` and `model` values. The config was reading them as `None` and falling back to the provider's default_model (`qwen3.6-35b-a3b`).

**Fix**: Corrected YAML structure to have single `llm:` level with proper 2-space indentation for all nested keys.

---

## Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| Function calling not supported by all local models | Detect support at runtime; graceful fallback to prompt-based tool use |
| Web search latency impacts chat responsiveness | Async search with streaming; show "searching..." indicator |
| Provider API changes break model listing | Version-tolerant parsing; cache model list; manual model entry fallback |
| User enters invalid provider config | Test connection before applying; show clear error messages |
| Privacy concerns with web search | Default OFF; clear opt-in; all queries proxied through local backend |

---

## Future Enhancements (Post-Launch)

1. **Search result caching** — Avoid repeated searches for same query
2. **Custom search engines** — Add user-defined search APIs
3. **Model benchmarks** — Built-in speed/quality comparison
4. **Conversation-aware search** — Use chat context to refine queries
5. **Voice-driven model switching** — "Switch to Ollama" voice command
6. **Usage analytics** — Local dashboard showing provider/model usage stats
