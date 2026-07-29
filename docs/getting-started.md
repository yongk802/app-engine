# Getting started

This guide takes you from **nothing installed** to **app-engine running with a
free local AI**. No prior git experience required.

There are three steps:

1. [Install git and get the code](#1-install-git-and-get-the-code)
2. [Run app-engine](#2-run-app-engine)
3. [Set up a free local AI (Qwen via Ollama)](#3-set-up-a-free-local-ai-qwen-via-ollama)

---

## 1. Install git and get the code

Git is the tool that downloads ("clones") this project and lets you pull
updates later. It's free.

### Install git

- **macOS** — open **Terminal** and run `git --version`. If git isn't
  installed, macOS offers to install the Command Line Tools; accept it.
  (Or install [Homebrew](https://brew.sh) and run `brew install git`.)
- **Windows** — download and install [Git for Windows](https://git-scm.com/download/win).
  This gives you **Git Bash**, a terminal where the commands below work exactly
  as written.
- **Linux** — use your package manager, e.g. `sudo apt install git` (Debian/Ubuntu)
  or `sudo dnf install git` (Fedora).

Confirm it works:

```bash
git --version
```

### Clone the repository

"Cloning" downloads a full copy of the project into a new folder (you can also
copy the URL from the project's GitHub page under the green **Code** button):

```bash
git clone https://github.com/yongk802/app-engine.git
cd app-engine
```

You now have all the code in a folder called `app-engine`.

### Getting updates later

When the project changes, pull the latest version from inside the folder:

```bash
git pull
```

> **New to git?** You only need `clone` and `pull` to *use* app-engine. If you
> want to *contribute changes back*, see [CONTRIBUTING.md](../CONTRIBUTING.md)
> for the fork → branch → commit → pull-request flow.

---

## 2. Run app-engine

app-engine needs [Python 3.10+](https://www.python.org/downloads/). Check with
`python3 --version` (Windows: `python --version`).

From inside the `app-engine` folder:

```bash
# create an isolated environment for the dependencies
python -m venv .venv

# activate it
#   macOS/Linux:
. .venv/bin/activate
#   Windows PowerShell:
.venv\Scripts\Activate.ps1

# install dependencies
python -m pip install -r requirements.txt

# start the engine, pointing it at a folder of apps
#   macOS/Linux:
APP_ENGINE_APPS_DIR=/path/to/apps python engine.py
#   Windows PowerShell:
$env:APP_ENGINE_APPS_DIR = "C:\path\to\apps"; python engine.py
```

Open **http://127.0.0.1:8770** in your browser. You'll see the launcher with
your apps in the sidebar. Click one to run it.

> An "app" is any folder containing an `app.json` manifest (and usually an
> `index.html`). Point `APP_ENGINE_APPS_DIR` at a directory whose subfolders are
> apps. See the [README](../README.md#appjson) for the manifest format.
>
> **Don't have any apps yet?** The reference companion collection is
> **personal-apps** — ready-made games, tools, and local-AI tutors built for
> this engine. Clone it and point `APP_ENGINE_APPS_DIR` at it. You can also
> write your own; all it takes is an `app.json`.

---

## 3. Set up a free local AI (Qwen via Ollama)

Chat-enabled apps use a local AI model. Everything runs on **your** computer —
prompts and responses never leave it, and there is no API key or cloud account.

app-engine uses **[Ollama](https://ollama.com/)** (a free local model runtime)
with **Qwen3** models. You don't have to install anything by hand: open
**⚙ Local AI settings** in the app sidebar and app-engine will:

1. check your memory, disk, and whether Ollama is already installed;
2. recommend a model — **Quality** (Qwen3 8B, ~5.2 GB) for computers with 16 GB+
   RAM, or **Compatibility** (Qwen3 1.7B, ~1.4 GB) for smaller machines;
3. open the official Ollama installer once you confirm the plan;
4. download the chosen model with a progress bar; and
5. enable chat only once the model is ready.

app-engine never downloads a model without your confirmation, and only offers to
remove models it downloaded itself.

> **Prefer the command line?** If you already use Ollama, you can pull a model
> yourself and app-engine will detect it:
>
> ```bash
> ollama pull qwen3:1.7b   # or qwen3:8b for the larger model
> ```

That's it — you now have app-engine running with a free, fully local AI.
