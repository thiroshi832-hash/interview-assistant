# AetherStack Interview Assistant

Live interview helper. Load your resume, click **Start interview**, the app listens to the conversation and Claude streams suggested answers in your voice on screen.

## Two modes

| Mode | What it captures | When to use |
|---|---|---|
| **Same laptop** | Mic + WASAPI loopback (system audio) — both digitally clean | You run the interview AND the assistant on one machine. Hide the assistant window from any screen share. |
| **Helper laptop** (recommended) | Single microphone — hears the candidate directly and the interviewer through the interview-computer's speakers | A second laptop sits next to the interview computer. Nothing is installed on the interview computer. |

The mode picker appears on launch. Speaker detection is automatic in both modes — no enrollment, no calibration.

## LLM provider

Pick **Anthropic Claude** or **OpenAI GPT** in the first-launch dialog. Both SDKs are bundled in the .exe; you only need the API key for whichever you choose. The provider preference and key are persisted to `%USERPROFILE%\.interview_assistant\config.json` so you only do this once.

Defaults:
- **Anthropic**: `claude-opus-4-7` (deep mode also Opus 4.7), effort `low` for low-latency live answers
- **OpenAI**: `gpt-4o-mini` (fast), `gpt-4o` for the deep-mode hotkey

Change models any time by editing the config file.

## Install (Windows)

```powershell
cd "C:\repositories\interview assistant"
python -m venv .venv
.venv\Scripts\Activate.ps1
.\install.ps1
```

(`install.ps1` runs `pip install -r requirements.txt` and then installs `resemblyzer` with `--no-deps` to avoid pulling in `webrtcvad`, which has no Windows wheel and requires MSVC build tools. The webrtcvad code path inside resemblyzer is never reached by this app.)

Set your API key — **either** Anthropic Claude **or** OpenAI GPT. You pick the provider in the app's first-launch dialog, so just paste it there. (Optionally you can also export it as an env var, which the app will pick up automatically.)

```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."   # for Claude
# or
$env:OPENAI_API_KEY = "sk-..."          # for GPT
```

## Run (from source — no build needed while developing)

```powershell
.\run.bat
```

(equivalent to `python app.py`). The companion Sender app (helper-network mode) has its own `.\run_sender.bat`. Both run straight from source — no PyInstaller step required to test a change.

## Build a distributable `.exe`

```powershell
.\build.bat
```

(equivalent to `pyinstaller --noconfirm app.spec`). `.\build_sender.bat` builds the Sender app from `sender.spec`.

Output: `dist\aetherstack-interview-assistant\` (onedir build, no Python install needed to run).

First launch of the .exe takes ~10–20 s while it extracts to a temp dir; subsequent launches are faster. Model weights (Whisper, silero-vad, Resemblyzer) are still downloaded to the user's home dir on first use — they are not bundled.

1. Pick a mode.
2. Load your resume (PDF / DOCX / TXT) and optionally paste the job description.
3. In same-laptop mode, click **Audio devices…** if the interviewer is coming through a non-default speaker/headset.
4. Click **Start interview**.
5. Watch the live transcript on the left and the suggested answer on the right.

### During the interview

| Button / Key | Effect |
|---|---|
| **Ctrl + Space** | Force an answer for the latest interviewer turn |
| **Regenerate** | Same as Ctrl+Space |
| **Shorter** | Re-answer in 2 sentences max |
| **More technical** | Re-answer with concrete technical detail |
| **Deeper (Opus)** | Re-answer with Claude Opus 4.7 (slower, richer) |
| **Stop** | Stop listening and tear down audio |

## How it works

```
audio source → silero VAD → faster-whisper STT
                                    │
                          ┌─────────┴─────────┐
                          │ same-laptop:      │ helper-laptop:
                          │   speaker known   │   Resemblyzer diarize
                          │   from stream     │   + AutoLabeler scores
                          │                   │     question-rate, length,
                          │                   │     first-to-speak → labels
                          └─────────┬─────────┘
                                    ▼
                          rolling transcript
                                    │
                          question detector ──► Claude (cached resume
                                                       + rolling Q&A,
                                                       streaming)
                                    │
                                    ▼
                                  UI
```

Resume + job role are sent in a single cached system block — first answer pays the cache-write premium, every subsequent answer reads from cache at ~10% input cost and ~50% lower TTFT.

## Config

Edit `~/.interview_assistant/config.json` after first run (or just change defaults in [config.py](config.py)):

```jsonc
{
  "model": "claude-opus-4-7",          // switch to "claude-sonnet-4-6" for lower latency
  "deep_model": "claude-opus-4-7",
  "effort": "low",                      // low | medium | high | max — keeps live answers fast
  "whisper_model": "small.en",          // tiny.en / base.en / small.en / medium.en / large-v3
  "whisper_compute": "int8",            // int8 (CPU) / float16 (GPU) / float32
  "mic_device_index": null,              // null = default microphone
  "loopback_device_index": null,         // null = default Windows output loopback
  "vad_silence_ms": 1200,
  "question_silence_ms": 2500
}
```

## Helper-laptop setup tips

- Place the helper-laptop mic where it can clearly pick up both the candidate (across the desk) and the interview computer's speakers.
- The interview computer must use **speakers, not headphones** — otherwise the helper can't hear the interviewer.
- Quality upgrade: run a 3.5 mm cable from the interview computer's headphone-out into the helper's line-in. Then switch to **same-laptop mode** (it'll use that as the loopback equivalent), which gives you clean dual streams without diarization.

## Repo layout

```
audio/
  source.py        # AudioSource abstract base
  dual_stream.py   # same-laptop: mic + WASAPI loopback
  single_mic.py    # helper-laptop: single mic
  diarizer.py      # Resemblyzer-based online speaker embedding + clustering
  auto_labeler.py  # cluster ID → "candidate" / "interviewer"
  _pcm.py          # mono / 16k / int16 conversion
pipeline/
  vad.py           # silero-vad segmenter (per-speaker)
  stt.py           # faster-whisper wrapper
  transcript.py    # thread-safe rolling history
  question_detector.py
  types.py         # Turn
ui/
  main_window.py
  mode_picker.py
  setup_view.py
  interview_view.py
  style.py
app.py             # main entry point — wires everything together
claude_client.py   # streaming, prompt-cached resume + role
resume_loader.py   # PDF / DOCX / TXT
config.py
smoke_test.py      # offline tests (transcript / question detector / auto labeler)
```

The old OCR-based files (`server.py`, `client.py`, `rendezvous_server.py`) are unrelated to this app — leave or remove them as you prefer.

## What's not built (yet)

- TTS (read the answer into an earbud).
- The "type the answer into a chat field" path from the old `client.py` — could be added back as an optional sink.
- Tunnel/rendezvous for a true two-machine helper setup (the current helper-laptop mode is single-process and doesn't need it).
