# Visual CUA execution mode

Retrace's tester ships four execution engines:

- `harness` — shells out to an external runner.
- `native` — Python HTTP runner with optional Playwright switch.
- `explore` — Playwright + accessibility-snapshot tool loop.
- `visual` — Playwright + screenshot/coordinate tool loop. **(this doc)**

Use `visual` when the accessibility tree is unreliable: canvas-heavy apps,
maps, video editors, custom shadow-DOM widgets, or bot-detection layers that
rewrite the DOM. Trade-offs:

- ✅ Works on apps where `explore` can't see anything useful.
- ❌ Requires a multimodal LLM (sends a screenshot every step).
- ❌ Slower per step (image roundtrip).
- ❌ No step caching — pixel coordinates aren't portable across themes,
  viewports, or layout changes.

## Tool surface

| Tool | Purpose |
| --- | --- |
| `goto(url)` | Navigate via the address bar. Use this instead of trying to click the browser chrome. |
| `click_at(x, y, button="left")` | Mouse click at viewport pixel coordinates. |
| `keyboard_type(text)` | Type text into whatever currently has focus. |
| `keyboard_press(key)` | Single key press (`Enter`, `Tab`, `Escape`, …). |
| `scroll(dx, dy)` | Wheel scroll. |
| `wait_ms(ms)` | Pause for async UI (clamped to 30s). |
| `screenshot()` | Force a fresh observation without changing state. |
| `finish(status, summary?)` | End the run. `status` ∈ {`success`, `blocked`, `needs_human`, `abandoned`}. |

The model receives a fresh screenshot before every decision plus the URL,
title, viewport size, and recent console events. It must respond with a
single JSON object: `{"tool": "...", "args": {...}, "rationale": "..."}`.

## Provider/model requirements

Visual mode sends a base64-encoded screenshot in the `user` message. The
LLM must:

1. **Accept image input.** Recommended models:
   - **Anthropic:** `claude-3-5-sonnet-20241022` or newer.
   - **OpenAI:** `gpt-4o`, `gpt-4o-mini`, `gpt-4-turbo`. (Older `gpt-3.5-*`
     models are text-only — they will silently drop the image.)
   - **OpenAI-compatible gateways:** verify the gateway forwards multimodal
     `content` arrays. Some Bedrock/proxy setups strip them.
2. **Return JSON.** Both `response_format: {"type":"json_object"}` (OpenAI)
   and "Return only a valid JSON object" (Anthropic) are sent automatically.
3. **Avoid pure-text "thinking" preambles.** The parser tolerates fenced
   JSON but a chatty preamble inflates token cost.

### Incompatible gateway modes

These configurations will silently fail or degrade:

- Gateways that downcast `content: [{type:"image_url"}, ...]` to plain text
  (some "OpenAI-compatible" wrappers do this for cost).
- Routers configured with a text-only routing rule (e.g. cheap-mode that
  forces `gpt-3.5-turbo`).
- Self-hosted llama.cpp / Ollama servers without a vision adapter loaded.

If you suspect content is being stripped, point Retrace at the upstream
provider directly and re-run.

## Selecting visual mode

Set `execution_engine: "visual"` on a tester spec, give it `app_url` and at
least one entry in `exploratory_goals`, and don't set `exact_steps` or
`assertions` (those are for `native`).

```yaml
execution_engine: visual
app_url: https://app.example.com
exploratory_goals:
  - Open the canvas editor
  - Add a text layer with the word "Retrace"
  - Save the document and confirm the toast appears
browser_settings:
  viewport: { width: 1280, height: 800 }
  visual_max_steps: 25
```

`browser_settings.visual_max_steps` (optional, default 20) caps how many
LLM round-trips a single run can make.

## Address-bar navigation pattern

Tools like `click_at(x, y)` only work inside the viewport. The browser's
address bar is *outside* the viewport, so the model can't click it. Use
`goto(url)` instead — it routes through `page.goto()` directly. The system
prompt explicitly instructs the model to prefer `goto` for any full-URL
navigation, so a well-behaved model will already do the right thing.

## Why coordinate caching is disabled

The native runner caches resolved URLs after a successful `get` so reruns
are faster. There is **no** equivalent cache for `click_at(x, y)` because:

- A 4px UI shift on a different render breaks the cached coords.
- Different viewports, themes, or A/B variants invalidate them silently.
- A bad replay gets you "click 0,0" — i.e. the top-left corner — with no
  loud error.

If portable coordinate caching becomes feasible (e.g. via persistent visual
landmarks), it can be added behind a feature flag. Until then, the rule is
simple: visual runs always start from a fresh screenshot.

## Debugging a visual run

Every run writes a `visual-trace.json` under `run.json`'s `artifacts/`
directory listing every step's tool call, observation, and (when available)
screenshot path. Inspect it to see where the model went off the rails.

Per-step screenshots are saved as `visual-step-NNN.png`. They're the same
images the LLM saw, so use them when the model's rationale and the
screenshot disagree.
