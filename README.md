# Screen AI Assistant

A Windows desktop app (CustomTkinter GUI) that:

1. **Captures** your screen (full screen or a region you draw).
2. **Sends** the screenshot to a vision-capable AI model.
3. **Reads** the AI's answer.
4. **Clicks** an input field and **types** the answer for you — optionally clicking a submit button afterwards.

Test individual steps on the **Test tab**, then chain them into a repeatable **Workflow**.

---

## Requirements

- Windows 10/11
- Python 3.12 (already installed via winget as `Python.Python.3.12`)
- An API key for OpenAI (or any OpenAI-compatible vision endpoint)

## Quick start

From the project folder in PowerShell:

```powershell
./run.ps1
```

The first run creates a virtual environment (`.venv`) and installs dependencies automatically, then launches the app.

### Manual setup (alternative)

```powershell
$py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
& $py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

## Configuration

Set your API key in **either** place:

- The **Settings** tab in the app (click *Save settings* to persist it to `config.json`), **or**
- A `.env` file — copy `.env.example` to `.env` and fill it in.

| Setting | Meaning |
|---|---|
| `OPENAI_API_KEY` | Your API key |
| `OPENAI_MODEL` | Vision model, e.g. `gpt-4o-mini`, `gpt-4o` |
| `OPENAI_BASE_URL` | Optional. For OpenAI-compatible providers (Azure, OpenRouter, local) |
| `AI_PROVIDER` | Optional. `openai`, `anthropic`, `gemini`, `openrouter`, or `custom` |

### AI providers

The **Settings → AI provider** dropdown chooses which service receives the
screenshots (used by *Capture + ask AI*, *AI: find & click*, and *Remember
screen value*):

| Provider | Notes | Example model |
|---|---|---|
| **OpenAI** | Standard API | `gpt-4o`, `gpt-4o-mini` |
| **Anthropic (Claude)** | Native Claude API | `claude-3-5-sonnet-latest` |
| **Google Gemini** | Preset OpenAI-compatible endpoint | `gemini-1.5-flash` |
| **OpenRouter** | Preset endpoint; access many models | `openai/gpt-4o-mini` |
| **Custom (OpenAI-compatible)** | Set your own Base URL (Azure, local, …) | any |

Each provider remembers its **own API key and model**, so you can switch
between them freely. For Gemini/OpenRouter the endpoint is preset — just paste
the key. Pick a **vision-capable** model for image steps.

**Not sure of the model name?** Enter your API key and click **↻ Fetch** next to
the Model field — it lists the models your key can actually use, so you can pick
a valid one from the dropdown (model names change over time, so a hard-coded
name can return a 404). You can also type a model name manually.

## How to use

1. **Settings tab** — enter your API key and model, then *Save settings*.
2. **Test tab → Capture area** — keep *full screen*, or click *Select region…* and drag a box around the area the AI should read. **Capture now (preview)** shows what the AI will see.
3. **Test a step** — pick a step type, configure it in the dialog, then click **▶ Test this step** to run that one action immediately and confirm it works (click lands, key is sent, AI reads correctly).
4. Once your steps behave, recreate them on the **Workflow tab** to chain them together.

The captured image, the AI's answer, and a running log are shown at the bottom.

## Workflows (multi-step automation)

The **Workflow tab** lets you chain many actions into a repeatable sequence —
e.g. *click a button → wait → capture & ask AI → type the answer → press Enter →
scroll → click → …* — and run it on a loop, with human-like delays.

### Step types

| Step | What it does |
|---|---|
| **Click** | Left/right/middle click (1–3×) at a fixed point or the current cursor |
| **Move mouse** | Move the cursor to a point |
| **Scroll** | Scroll up/down by an amount (negative = down) |
| **Type text** | Type literal text (optionally clearing the field first) |
| **Press key / hotkey** | `enter`, `tab`, `esc`, `ctrl+a`, `ctrl+shift+s`, … |
| **Capture + ask AI** | Screenshot the region, ask the AI, store the answer (optionally type it) |
| **Type AI answer** | Type the most recent AI answer at a point/field |
| **AI: find & click** | Describe an element; the AI locates it in the screenshot and clicks it |
| **AI check — stop if condition fails** | Capture the screen and ask the AI to verify a condition (e.g. "a green success message is visible"). If it isn't met (after the configured re-check attempts), the workflow **stops** — or, if you enable **Run restart workflow** on that step, it runs the **Restart** tab steps once and then starts the main workflow again from step 1 |
| **Crop image → paste to window** | Crops a screen rectangle (manually picked **or located by the AI from your description**), copies it to the clipboard as a real **image**, clicks the destination field, and pastes with Ctrl+V (optional key after) |
| **AI read → paste to other window** | One step: capture → AI reads (your prompt) → copy to clipboard → click the destination field (focuses the right window) → optional clear → Ctrl+V → optional key |
| **Remember clipboard** | Save the current clipboard text into a named memory slot |
| **Remember screen value (AI)** | Read a value off the screen with the AI and save it into a named memory slot |
| **Type remembered value** | Type a previously saved memory slot at a point/field |
| **Wait** | Pause for a fixed or random number of seconds |

### Building a workflow

1. Go to the **Workflow** tab.
2. Pick a step type from **➕ Add step…** — a dialog opens for its settings.
   For click/type targets, use **🎯 Pick** to left-click the spot on screen.
3. Reorder with ▲ / ▼, edit with ✎, remove with ✕, or toggle a step on/off
   with its checkbox.
4. Set **repeat ×N** to run the whole sequence multiple times (saved to
   `config.json` when you change it, run the workflow, or close the app).
5. **▶ Run workflow** to start, **⏹ Stop** to cancel. **💾 Save** persists steps.

### Restart workflow (recovery after a failed AI check)

When an **AI check — stop if condition fails** step does not pass, you may need
to click away a dialog, go back, or retry before running the main sequence
again. Use the **Restart** tab for that recovery sequence:

1. Go to the **Restart** tab and add the recovery steps (same step types as the
   main workflow).
2. On the failing **AI check** step in the main workflow, enable **When check
   fails, run restart workflow then restart main workflow from step 1**.
3. **💾 Save** on both tabs (or close the app — settings auto-save).

When the check fails during a run, the app runs the restart steps once, then
starts the **main workflow from step 1**, and can repeat that recovery cycle
without limit while the run continues. If the checkbox is off, behaviour is
unchanged: the workflow stops.

Use **▶ Run restart** on the Restart tab to test the recovery steps alone.

### AI: find & click

Instead of hard-coding coordinates, you can describe a target (e.g. *"the blue
Submit button"*, *"the X close icon top-right"*) and let the vision model find
it. The app screenshots the **Test-tab capture area**, asks the AI for the
element's position, maps it back to screen coordinates, and clicks.

Tips for best results:

- Choose a **capture region** that tightly frames the area (smaller = more
  accurate localisation than full screen). This is by far the biggest factor —
  locating a small button in a full-screen shot is unreliable.
- Be specific in the description (colour, text, position).
- Accuracy depends on the model — `gpt-4o` / Claude `sonnet` localise better
  than `gpt-4o-mini`.
- Keep **High-accuracy “AI: find & click”** on (Settings). It does a second
  *zoom-in* pass around the first guess for much better precision on small
  targets (uses one extra API call). Turn it off for speed.
- After locating, the **preview shows a red crosshair** where the AI pointed, so
  you can verify before trusting it.
- Works best at **100% display scaling**; high-DPI scaling can offset clicks.

### Remembering values (clipboard → memory)

To carry a value (like a copied task ID) across later steps:

1. **AI: find & click** (or **Click**) the app's *copy* button — it puts the
   value on the clipboard.
2. **Remember clipboard** → store it under a name, e.g. `task_id`.
3. Later, **Type remembered value** with name `task_id` to type it into a field
   (pick an input point, or click the field first).

If there's **no copy button**, use **Remember screen value (AI)** instead of
steps 1–2: it screenshots the capture area, asks the AI to read the value you
describe (e.g. *"the task ID, reply only the number"*), and stores the text
under your chosen name. Then **Type remembered value** types it later.

Memory lasts for the whole run, so the value survives even if the clipboard is
overwritten by other steps in between. The raw AI reply for *find & click* is
written to the log, which helps when tuning descriptions.

### Human-like behaviour

On the **Settings** tab, *Human-like behaviour* adds:

- randomized **delay between steps** (configurable min–max),
- small random **mouse-position jitter** and variable move durations,
- variable **per-character typing speed**,
- large scrolls broken into smaller chunks.

Turn it off for fast, deterministic runs.

## Remote desktops (AnyDesk / RDP / Parsec) and games

Remote-desktop tools and many games **ignore ordinary synthetic keystrokes**
(virtual-key events). They only forward **hardware scan-code** input. If your
keys (e.g. *End*, *Enter*) or scrolling do nothing on a remote screen, this is
why.

The app sends scan-code input by default via `pydirectinput`. Make sure
**Settings → "Hardware scan-code input"** is enabled (it is by default). Then:

1. Keep the **remote window focused** and the **mouse cursor inside it** while
   the workflow runs.
2. For typed answers/keys to land, the remote app's input field must have focus —
   add a **Click** step on the field first.

If something still doesn't register, try toggling the setting off/on, or run the
app **as Administrator** (some remote clients run elevated, and input only flows
between processes at the same elevation level).

### Capturing stays invisible to the remote side

The app follows a **read-locally, act-remotely** model — the remote AnyDesk user
cannot detect that you are capturing:

- **Screenshots are taken from your own local monitor** with `mss` (an ordinary
  local screen grab of the AnyDesk window). Nothing is sent over the connection,
  there's no flicker or notification, and AnyDesk has no hook into local OS
  screenshots.
- The app **never presses PrintScreen** and **never uses AnyDesk's own capture**
  feature — either of those *would* be visible remotely.
- **Capturing never moves the mouse or changes focus.** *Capture + ask AI*,
  *AI: find & click* (including the zoom-in refine pass), and *Remember screen
  value* send nothing to the remote.
- The **only remote-visible actions are the clicks/keys** you add as steps (the
  cursor moves to a spot and clicks). Keep mouse-only steps to a minimum if you
  want the smallest possible footprint.

## Safety

- **Fail-safe:** slam your mouse pointer into any corner of the screen to instantly abort an in-progress automated click/type (this also stops a running workflow).
- Disabled steps and empty AI answers are skipped automatically.
- Use responsibly and only where automating input is permitted.

## Project layout

```
auto-app/
├── main.py            # entry point
├── run.ps1            # one-click setup + launch
├── requirements.txt
├── .env.example
└── src/
    ├── config.py      # settings + workflow load/save
    ├── screen.py      # screen capture + region selection overlay
    ├── automation.py  # click, type, scroll, keys, coordinate picking
    ├── ai_client.py   # OpenAI-compatible vision client
    ├── workflow.py    # step model + execution engine
    └── gui.py         # CustomTkinter UI (Test / Workflow / Settings tabs)
```
