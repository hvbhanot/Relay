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
./relay.sh --host 127.0.0.1 --port 8090
```

Or after installing the package locally:

```bash
pip install -e .
relay --open
```

### 3. Use the UI

The browser UI includes:

- Ollama setup and model picker
- local / cloud ping
- planner preview before running
- chat history saved locally
- routing trace inspector
- cloud model pool (OpenRouter)
- privacy mode and routing thresholds

Setup is saved to `relay.ui.json`. Chat history is saved to `relay.history.json`. Both are gitignored because they may contain API keys.

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

Legacy `MODELROUTER_*` environment variables are still accepted.

## Routing policy

Important defaults:

- `cloud_enabled` defaults to `false`.
- `privacy_mode` defaults to `balanced`.
- current-events and source-requiring tasks are routed online only if cloud is enabled.
- sensitive-looking subtasks stay local unless you explicitly set `privacy_mode=permissive`.

## Multiple cloud models

When the cloud provider is **OpenRouter**, Relay routes each cloud-bound
subtask to a *different* model based on its capability.

Defaults (see `DEFAULT_CLOUD_MODEL_MAP` in `config.py`):

```text
reasoning / math / coding / high_stakes -> anthropic/claude-opus-4.8
current_info / sources / large_context  -> openai/gpt-5.5
everything else (default)               -> anthropic/claude-sonnet-4.6
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
  webui.py             browser UI backend
  webui_assets/        browser UI HTML/CSS/JS
  orchestrator.py      plan -> route -> execute -> synthesize loop
  policy.py            local/cloud routing decisions
  cloud_pool.py        capability -> cloud model selection
  chat_history.py      saved conversations
  ui_config.py         UI-managed persistent setup
  config.py            environment config
  providers.py         Ollama and OpenAI-compatible providers

tests/
```

## Notes

Relay intentionally favors local execution and privacy over aggressively using cloud models.
UI-managed API keys are stored as plain text in `relay.ui.json`; keep that file private.

## License

Released under the [MIT License](LICENSE).