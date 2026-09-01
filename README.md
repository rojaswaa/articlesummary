# Article Summary

A web app that helps researchers run systematic literature reviews. It analyzes folders of PDF articles with AI (Google Gemini or local models), extracts bibliographies, searches academic databases, and syncs results with your Zotero library.

# This is a Code Vide Project. It is meant only to run locally.
---

## Installation Guide (no technical experience needed)

This app runs on your own computer. You'll use the **Terminal** (Mac) or **Command Prompt / PowerShell** (Windows) — a window where you type commands and press Enter. Copy each command below exactly, one at a time.

### Step 1 — Install Python

Python is the programming language this app runs on.

1. Go to https://www.python.org/downloads/
2. Click the big yellow **Download Python** button (version 3.11 or newer).
3. Open the downloaded file and follow the installer.
   - **Windows users:** on the first installer screen, check the box that says **"Add Python to PATH"** before clicking Install.

To check it worked, open Terminal (Mac: press Cmd+Space, type "Terminal") or Command Prompt (Windows: press the Windows key, type "cmd") and type:

```
python3 --version
```

You should see something like `Python 3.12.x`. (On Windows, if `python3` doesn't work, try `python`.)

### Step 2 — Download this project

If you have the green **Code** button open on GitHub:

1. Click **Code → Download ZIP**.
2. Unzip the file (double-click it).
3. Move the unzipped folder somewhere easy to find, like your Documents folder.

### Step 3 — Open the project folder in the terminal

In your terminal, type `cd ` (with a space after it), then drag the project folder from your file explorer into the terminal window, and press Enter. For example:

```
cd ~/Documents/article-summary
```

### Step 4 — Create a "virtual environment" and install the app

A virtual environment keeps this app's components separate from the rest of your computer. Run these commands one at a time:

**Mac / Linux:**
```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**Windows:**
```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

The last command downloads everything the app needs — it may take a few minutes.

> **Note:** if you plan to use the local (offline) OCR option, you also need the free Tesseract program: on Mac, install [Homebrew](https://brew.sh) then run `brew install tesseract`; on Windows, download it from https://github.com/UB-Mannheim/tesseract/wiki. You can skip this if you'll use Mistral OCR (cloud).

### Step 5 — Set up your keys

The app needs a few "API keys" — think of them as passwords that let it talk to AI services.

1. In the project folder, find the file named `.env.example`. Make a copy of it and rename the copy to exactly `.env` (nothing before the dot).
   - Easiest way, from the terminal: `cp .env.example .env` (Mac/Linux) or `copy .env.example .env` (Windows).
2. Open `.env` in any text editor (Notepad, TextEdit) and fill in:
   - **GEMINI_API_KEY** — free at https://aistudio.google.com/apikey (sign in with Google, click "Create API key", copy it).
   - **ZOTERO_USER_ID** and **ZOTERO_API_KEY** — at https://www.zotero.org/settings/keys (your user ID is shown on that page; click "Create new private key", allow library read/write). Only needed for Zotero features.
   - **DJANGO_SECRET_KEY** — any long random text. To generate one, run:
     ```
     python3 -c "import secrets; print(secrets.token_urlsafe(50))"
     ```
     and paste the result.
3. Save the file. You can also enter the AI/Zotero keys later inside the app's Settings page instead.

### Step 6 — Prepare the database and create your account

Still in the terminal (with the virtual environment active — you'll see `(.venv)` at the start of the line):

```
python manage.py migrate
python manage.py createsuperuser
```

The second command asks you to choose a username and password — this is what you'll use to log in.

### Step 7 — Start the app

```
python manage.py runserver
```

Leave this terminal window open, then open your web browser and go to:

**http://127.0.0.1:8000**

Log in with the username and password from Step 6. You're in!

To stop the app, click on the terminal window and press **Ctrl+C**.

### Starting the app again later

Next time, you only need Steps 3, the `activate` command from Step 4, and Step 7:

```
cd ~/Documents/article-summary
source .venv/bin/activate        # Windows: .venv\Scripts\activate
python manage.py runserver
```

---

## Troubleshooting

- **"python3: command not found"** — Python isn't installed or wasn't added to PATH. Redo Step 1; on Windows try `python` instead of `python3`.
- **"pip: command not found"** — make sure the virtual environment is active (you see `(.venv)` in the terminal).
- **The page won't load** — check the terminal running the server for red error text; make sure you typed the address exactly: `http://127.0.0.1:8000`.
- **AI analysis fails** — double-check your API key in `.env` or the in-app Settings page (no extra spaces or quotes around the key).
