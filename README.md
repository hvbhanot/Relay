<p align="center">
  <img src="assets/relay-logo.png" alt="Relay" width="520">
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-blue.svg" alt="Python 3.11+">
</p>

# Relay

A local-first model router inspired by multi-model orchestration systems.

The default flow is:

1. **Plan locally**: a local model decomposes the user request into subtasks.
2. **Route conservatively**: each subtask runs on the local model unless policy says an online model is needed.
3. **Collect results**: subtask answers are gathered concurrently.
4. **Synthesize locally**: the local model writes the final answer from the collected work.

Online models are optional and are only used when:

- the task appears to require current/external knowledge,
- the local planner marks a subtask as needing online help,
- a subtask requires a capability you configure as cloud-only,
- or the local model self-reports low confidence and cloud fallback is enabled.

## Quick start

### 1. Run a local model with Ollama

Install Ollama separately, then pull a model, for example:

```bash
ollama pull llama3.1:8b
ollama serve
```

### 2. Start Relay (browser UI)

From the project folder:

```bash
chmod +x relay.sh   # first time only
./relay.sh
```

That opens the UI in your browser, usually at `http://127.0.0.1:8090`.

To start without auto-opening the browser:

```bash
./relay.sh serve --host 127.0.0.1 --port 8090
```

Or after installing the package locally:

```bash
pip install -e .
relay serve --open
```

### 3. Use the UI

Full usage docs live in the app at **`/docs`** (models, routing, the cloud pool,
web search, privacy, API). Highlights:

**Routing & privacy**

- per-request route toggle (Auto / Local / Cloud) and a Web search toggle in the composer
- privacy badge on every answer: green "Local only — never left your machine",
  amber "Via Ollama Cloud" (when the local model is an Ollama `-cloud` model),
  or blue "N of M subtasks via cloud"
- secret redaction before cloud: API keys, emails, SSNs, card numbers, and
  `password=…` values are masked before prompts leave for cloud models and
  restored in the answer locally (on by default)
- privacy modes and routing thresholds; a warning when the configured "local"
  model is actually an Ollama Cloud model
- thumbs up/down feedback that tunes the local-confidence threshold over time

**Chat**

- planner preview: edit, add, remove, and re-route subtasks before running
- Stop button for in-flight requests; regenerate the latest answer; edit-and-resend
- chat history saved locally with search, rename, delete, and Markdown export
- attachments (images + text files) that persist with history
- routing trace inspector with per-subtask reasons, sources, cost, and a
  retry button for failed subtasks
- syntax-highlighted code blocks with one-click copy

**Models**

- local providers: Ollama, LM Studio, llama.cpp server, vLLM (picker + ping)
- cloud model pool (OpenRouter): route each capability to a different model
- optional web search for current-info subtasks (Ollama web search + web_fetch)

Setup is saved to `relay.ui.json`. Chat history is saved to `relay.history.json`,
feedback to `relay.feedback.json`. All are gitignored. API keys are never written
to these files — they are kept in an encrypted local vault at `~/.relay/secrets.enc`.

If `relay.ui.json` is missing, Relay falls back to the legacy `modelrouter.ui.json` path.

### 4. Optional: configure with `.env`

The browser UI is the easiest path, but environment variables are still supported.
Copy `.env.example` to `.env` or export variables in your shell:

```bash
cp .env.example .env
```

OpenRouter example:

```bash
export RELAY_ENABLE_CLOUD=1
export RELAY_CLOUD_PROVIDER=openrouter
export OPENROUTER_API_KEY=sk-...
export OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
export OPENROUTER_MODEL=anthropic/claude-sonnet-4.6
```

Web search and redaction:

```bash
export RELAY_ENABLE_WEB_SEARCH=1   # fresh results for current-info subtasks
export OLLAMA_API_KEY=...          # free key from ollama.com/settings/keys
export RELAY_REDACT_CLOUD=1        # mask secrets before cloud calls (default)
```

Legacy `MODELROUTER_*` environment variables are still accepted.

## Routing policy

Important defaults:

- `cloud_enabled` defaults to `false`.
- `privacy_mode` defaults to `balanced`.
- `cloud_redaction` defaults to `true` (secrets are masked before cloud calls).
- current-events and source-requiring tasks are routed online only if cloud is enabled.
- sensitive-looking subtasks stay local unless you explicitly set `privacy_mode=permissive`.
- 👍/👎 feedback on fully-local answers nudges the escalation threshold (±0.15 max).

## API & CLI

The UI server doubles as an OpenAI-compatible endpoint, so any OpenAI client can
use the full plan → route → synthesize pipeline:

```bash
curl http://127.0.0.1:8090/v1/chat/completions \
  -H "content-type: application/json" \
  -d '{"model": "relay", "messages": [{"role": "user", "content": "hello"}]}'
```

The `model` parameter can pin the route: `relay:local` forces local,
`relay:cloud` prefers cloud, plain `relay` uses auto routing.

Headless CLI: `relay ask "…"` (add `--stream` or `--json`), `relay plan "…"` to
preview routing without running, `relay api` for an API-only server.

## Multiple cloud models

When the cloud provider is **OpenRouter**, Relay routes each cloud-bound
subtask to a *different* model based on its capability.

Defaults (see `DEFAULT_CLOUD_MODEL_MAP` in `config.py`):

```text
reasoning / math / coding / high_stakes        -> anthropic/claude-opus-4.8
current_info / sources / large_context / vision -> openai/gpt-5.5
everything else (default)                      -> anthropic/claude-sonnet-4.6
```

Customize the map in the browser UI (OpenRouter → "Cloud model pool") or via the
`RELAY_CLOUD_MODEL_MAP` env var.

### Adding more cloud models

A subtask can declare several capabilities. The pool picks the model for the
**highest-priority** capability that has an entry in the map; if none match, it
uses the default (`OPENROUTER_MODEL`). The priority order is:

```text
high_stakes > reasoning > math > coding > large_context >
sources > current_info > vision > creative > general
```

The full set of capability keys you can map: `general`, `reasoning`, `coding`,
`math`, `current_info`, `sources`, `high_stakes`, `large_context`, `creative`,
`vision`.

**Option A — browser UI (easiest).** Add models and assign their roles directly
in the Setup panel:

1. Open the **Setup** panel in the browser UI.
2. Set **Cloud provider** to `openrouter`.
3. Set the **Default model** (the fallback used by any capability you don't map),
   e.g. `anthropic/claude-sonnet-4.6`.
4. Expand **Cloud model pool**. This is the *role → model* map: each key is a
   capability (the model's "role"), each value is an OpenRouter model slug.
   Add one line per model you want to specialize:

   ```json
   {
     "coding":    "anthropic/claude-opus-4.8",
     "reasoning": "anthropic/claude-opus-4.8",
     "current_info": "openai/gpt-5.5",
     "vision":    "google/gemini-2.5-pro"
   }
   ```

5. Click **Save settings** (the pill shows **saved**). Settings persist to
   `relay.ui.json`.

Anything you leave out of the map uses the **Default model**. To remove a model's
role, delete its line and save. Use any slug from
[openrouter.ai/models](https://openrouter.ai/models).

**Option B — env var.** `RELAY_CLOUD_MODEL_MAP` is a JSON object of
`capability -> model-slug`, merged *over* the defaults (so you only list what you
want to change). Use any model slug from [openrouter.ai/models](https://openrouter.ai/models):

```bash
export RELAY_CLOUD_PROVIDER=openrouter
export RELAY_CLOUD_MODEL_MAP='{
  "coding":   "anthropic/claude-opus-4.8",
  "creative": "openai/gpt-5.5",
  "vision":   "google/gemini-2.5-pro",
  "reasoning":"deepseek/deepseek-r1"
}'
# Default for any unmapped capability:
export OPENROUTER_MODEL=anthropic/claude-sonnet-4.6
```

Notes:

- Invalid JSON or non-string entries are ignored, so a typo never wipes the pool —
  it just falls back to the defaults.
- The capability map only applies when `RELAY_CLOUD_PROVIDER=openrouter`. Other
  providers use the single `OPENROUTER_MODEL` / default slug.
- To pin one model for *everything*, leave the map empty and set `OPENROUTER_MODEL`.

## Project layout

```text
relay.sh               start the browser UI
relay/
  webui.py             browser UI backend + OpenAI-compatible endpoint
  webui_assets/        browser UI HTML/CSS/JS and the /docs page
  orchestrator.py      plan -> route -> execute -> synthesize loop
  policy.py            local/cloud routing decisions
  cloud_pool.py        capability -> cloud model selection
  web_search.py        Ollama web search + web_fetch integration
  redaction.py         secret masking for cloud-bound prompts
  feedback.py          answer feedback -> confidence-threshold bias
  chat_history.py      saved conversations (rename/delete/attachments)
  secrets_store.py     encrypted local vault for API keys
  ui_config.py         UI-managed persistent setup
  config.py            environment config
  providers.py         Ollama and OpenAI-compatible providers

tests/
```

## Notes

Relay intentionally favors local execution and privacy over aggressively using cloud models.
UI-managed API keys are stored in an encrypted local vault (`~/.relay/secrets.enc`), never in
`relay.ui.json`; any keys found inline in an older `relay.ui.json` are migrated into the vault
on first load.

## License

Released under the [MIT License](LICENSE).