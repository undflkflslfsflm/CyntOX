# Install CyntOX on Windows

In PowerShell, paste:

```powershell
irm https://raw.githubusercontent.com/undflkflslfsflm/CyntOX/main/install.ps1 | iex
```

Start an interactive session from any folder with:

```text
cyntox run
```

Use the same simple form for other tasks: `cyntox check`, `cyntox test`, `cyntox fix`, `cyntox jobs`, and `cyntox help`. Queue work with `cyntox ask "review this repo"`; inspect it with `cyntox show JOB_ID`. Quote task text and paths when they contain spaces. See the [command shortcuts](RUNBOOK.md#command-shortcuts) for the complete list.

`cyntox chat` and the original command forms remain supported. Launch options pass through, for example `cyntox run --version`.

## What setup does

The Windows installer clones `main` into `%LOCALAPPDATA%\CyntOX\app`. It finds or installs Git, Python 3.12, Node.js 22 or newer, and Ollama; prepares the project's locked Python and JavaScript dependencies; and initializes local runtime storage. Package installation uses [Windows Package Manager](https://learn.microsoft.com/en-us/windows/package-manager/winget/install). Missing WinGet requires installing Microsoft's App Installer first. Prerequisite installers may request Windows elevation. No Docker installation is required to start the assistant.

The installer registers a `cyntox.cmd` shim under `%LOCALAPPDATA%\CyntOX\bin`, adds that directory to the current and persistent user PATH, and records dependency locations so the command also works outside the installation directory. It does not change the machine PATH or PowerShell execution policy. If another already-open terminal cannot find the command, reopen the terminal application.

The assistant's primary project remains the installed checkout. Running the command from a different directory does not grant it automatic access to that directory. Existing tool and privacy boundaries apply.

Setup can access GitHub, package registries, and Hugging Face to download dependencies and weights. Normal assistant operation remains local with the existing network policy. Qwythos/AirLLM remains an explicit optional setup; see [its guide](AIRLLM_QWYTHOS.md).

## Model preparation

An existing `cyntox:latest` model is reused without replacement. Fresh installations use the public base-model source recorded in `config/install-model.lock.json`, verify the downloaded file's exact size and SHA-256, and apply `config/Modelfile.cyntox` for identity and safety defaults. The full canonical behavior prompt remains supplied at runtime.

The model download is approximately 17 GB. Allow at least 50 GiB free on the model-download and Ollama-storage volumes for download and import. Downloads are resumable, and a failed download or import stops setup without printing a success message. The downloaded GGUF is retained under `%LOCALAPPDATA%\CyntOX\models` for retry/reuse. Runtime performance depends on available memory and GPU capacity; the installer does not promise that every Windows PC can run this model well.

The [pinned public base GGUF](https://huggingface.co/huihui-ai/Huihui-Qwen3.8-27B-abliterated-GGUF/blob/246becd5a88523e21c58e848e791955719e9d1c9/Huihui-Qwen3.8-27B-abliterated-UD-Q4_K_XL.gguf) contains the same weights, tokenizer, and template as the historical local CyntOX blob. The whole-file hashes differ only because two GGUF metadata entries are ordered differently; hashing the local file with those entries reordered reproduced the public source hash. Both hashes are recorded in the installer lock. The installer replaces the portable Modelfile's source with the checksum-verified local file before import; the unpinned HF alias in the human-readable Modelfile is not used for installer downloads.

## Rerun or use an existing checkout

Rerunning the one-line command fast-forwards a clean installation on `main`, then repeats setup checks. If the destination contains another repository, local changes, or another branch, setup stops and preserves the files. Dependencies and already prepared models are reused. Failed partial environments produce an actionable error rather than being deleted automatically.

To set up a checkout you already downloaded, run from that checkout:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 -SourcePath .
```

To register an already prepared checkout without downloading dependencies or models:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 -SourcePath . -RegisterOnly
```

With `-File`, reopen the terminal application afterward so it receives the updated user PATH. The piped one-line installer also updates the current PowerShell session immediately.

Developers can still use `scripts/bootstrap.ps1` directly and run `cyntox.cmd` from the checkout without registering a command. Bash/WSL users retain `scripts/bootstrap.sh`; this one-line installer targets Windows.
