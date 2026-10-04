# Job & Internship Application Assistant

A personal assistant that finds jobs and internships, scores them against your
profile with Google Gemini, picks the right resume, and fills in the application
form while you watch. **You** click Submit (unless you explicitly ask it not to).

It runs on your own computer, in a visible browser window, for your own
applications. It never stores or types your passwords, and it never invents
experience, skills or qualifications you don't have.

> **Build status:** Stage 1 of 5 is complete — config, database, Gemini client,
> and the browser launcher with manual login. Job discovery and form filling
> arrive in stage 2. The commands marked *(stage 1)* below work today.

---

## 1. Install Python

You need **Python 3.11 or newer**.

- **Windows:** download from [python.org/downloads](https://www.python.org/downloads/).
  On the first installer screen, **tick "Add python.exe to PATH"** before clicking Install.
- **macOS:** `brew install python@3.12`, or download from python.org.
- **Linux:** `sudo apt install python3 python3-venv python3-pip`

Check it worked — open a new terminal and run:

```
python --version
```

You should see `Python 3.11.x` or higher. (On macOS/Linux you may need `python3`
everywhere this README says `python`.)

## 2. Get the project set up

From the project folder:

```bash
# Create an isolated environment so this project's packages stay separate
python -m venv .venv

# Activate it  (do this every time you open a new terminal)
#   Windows PowerShell:
.venv\Scripts\Activate.ps1
#   Windows cmd.exe:
.venv\Scripts\activate.bat
#   macOS / Linux:
source .venv/bin/activate

# Install the libraries
pip install -r requirements.txt

# Download the browser Playwright will drive
playwright install chromium
```

If PowerShell refuses to run the activate script, run this once:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

## 3. Get a free Gemini API key

1. Go to [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey).
2. Sign in with your Google account.
3. Click **Create API key**, then copy it.
4. Copy `.env.example` to `.env` and paste the key in:

   ```
   GEMINI_API_KEY=AIza...your-key...
   GEMINI_MODEL=gemini-2.5-flash
   ```

The free tier is plenty for personal use. `GEMINI_MODEL` can be any Gemini model
id — Flash is the cheap, fast default and is all this tool needs.

**Never commit `.env`.** It's already in `.gitignore`.

## 4. Fill in your profile

Copy the example config and edit it:

```bash
# Windows
copy config.yaml.example config.yaml
# macOS / Linux
cp config.yaml.example config.yaml
```

Open `config.yaml` in any text editor and replace **every `[bracketed]`
placeholder** with your real details — name, email, phone, city, degree,
college, graduation year, expected stipend/salary, notice period, your
LinkedIn/GitHub/portfolio links, your target roles and locations, and your
skills.

Nothing about you lives anywhere else in the code. Everything the assistant says
on your behalf comes from this file.

While you're in there, set:

- `matching.match_threshold` — jobs scoring below this are skipped (default 70)
- `sources.*.enabled` — which job sites to use, plus the keywords and locations
  to search
- `limits.daily` — how many applications per site per day
- `limits.run_window` — the assistant only runs between these hours
  (default 09:00–21:00 Asia/Kolkata)

## 5. Prepare your resumes

You need at least three PDF resume variants. Put them in `./resumes/`:

```
resumes/frontend.pdf
resumes/data.pdf
resumes/general.pdf
```

Gemini reads each variant's `focus_keywords` from `config.yaml` and picks the
best fit for each job.

### Getting the public links

Many forms ask for a resume *link* rather than a file upload, so each variant
needs a shareable URL:

1. Upload the PDF to [Google Drive](https://drive.google.com).
2. Right-click the file → **Share**.
3. Under "General access", change **Restricted** to **Anyone with the link**,
   and leave the role as **Viewer**.
4. Click **Copy link**.
5. Paste it into the matching `public_link:` in `config.yaml`.

If you skip the "Anyone with the link" step, recruiters will hit a permission
wall and your application goes nowhere. Test each link in a private/incognito
window to be sure it opens.

## 6. Choose a browser mode

In `config.yaml` under `browser:`:

**`mode: "persistent"` (recommended to start)**
Playwright launches its own Chromium with a profile stored in
`./browser_profile`. You log in to the job sites once; the session is reused on
every later run. Nothing else to do.

**`mode: "cdp"`**
Attaches to a Chrome *you* start, so you get your real Chrome and its
extensions. Start it with the included script **before** running the assistant:

```bash
# Windows
start_chrome.bat
# macOS / Linux
./start_chrome.sh
```

That opens Chrome with `--remote-debugging-port=9222` and a dedicated profile in
`./chrome_bot_profile`, so your everyday Chrome windows and cookies are left
alone. **Leave that window open while the assistant runs**, and do your job-site
logins in it.

## 7. Verify your setup *(stage 1)*

```bash
python main.py --check
```

This prints a table checking your config, every resume file and link, the
database, the browser mode, and your Gemini API key. Fix any `FAIL` rows before
going further.

## 8. First-time manual login *(stage 1)*

```bash
python main.py --login
```

A browser window opens at each enabled site's login page and the terminal waits:

```
  LINKEDIN needs a manual login (attempt 1/3)
  Log in manually (email / Google / OTP), then press Enter.
```

Sign in **in the browser window** however you normally do — password, Google
sign-in, OTP — then press Enter in the terminal. The assistant re-checks and
moves to the next site.

**This program never sees, types or stores your password.** It only reuses the
browser session you created yourself. Every later run checks whether you're
still signed in and pauses for a manual login again if a session has expired,
rather than failing.

## 9. Running it

Always test a new site or a new setup with `--dry-run` first.

```bash
# Fill forms, screenshot everything, never submit
python main.py --dry-run

# DEFAULT: fill the form, highlight Submit, beep, and wait for you to click it.
# Press "s" in the terminal to skip a job instead.
python main.py --assist

# Fill the form, print a summary, ask y/n; the bot clicks Submit on "y"
python main.py --review

# Submit without asking - company career pages only (Greenhouse, Lever, ...)
# and only at match_score >= 80. Never used on LinkedIn, Naukri or Indeed.
python main.py --auto

# Only some sources
python main.py --site linkedin,internshala --dry-run

# Cap the whole run at 5 applications
python main.py --max 5 --assist
```

## 10. Seeing what happened *(stage 1)*

```bash
python main.py --report            # last 7 days, per-site totals and detail
python main.py --report-days 30    # a longer window
python main.py --export            # write applications.csv
```

Every run also writes:

- `logs/run_YYYY-MM-DD.log` — full detail of what happened
- `screenshots/` — a screenshot for every form, confirmation and problem
- `applications.csv` — the whole tracking database as a spreadsheet
- `drafts/` — email applications drafted for you to send yourself
- `followups.csv` — follow-up messages for applications with no reply

## Safety rules built in

- **Nothing is ever sent or submitted without you**, except in `--auto`, which is
  limited to company career pages above the score threshold.
- **Email applications are only ever drafted**, never sent.
- **Follow-ups are only ever drafted**, never sent.
- Gemini is instructed, on every single call, never to claim experience, skills,
  degrees or certifications that aren't in your `config.yaml`. When the honest
  answer isn't clear it picks the truthful one and flags the job
  `needs_review` for you to look at.
- Per-site daily limits, a 09:00–21:00 run window, and random human-paced delays
  (3–10s between actions, 45–120s between applications).
- On any CAPTCHA, "unusual activity" or rate-limit message: screenshot, stop that
  site for the rest of the day, carry on with the others.
- Every job is wrapped in its own error handling, so one bad form can't end the run.

## Project layout

```
main.py                 CLI entry point and run orchestration
config.py               Loads config.yaml + .env, validates your profile
config.yaml.example     Template for your profile - copy to config.yaml
.env.example            Template for secrets - copy to .env
db.py                   SQLite tracking, dedupe, CSV export
start_chrome.bat/.sh    Launch Chrome with remote debugging for CDP mode
browser/
  launcher.py           Persistent-profile and CDP browser startup
  login.py              Logged-out detection, manual-login pauses, CAPTCHA detection
  human.py              Human-paced delays, beeps, highlighting, screenshots
ai/
  gemini_client.py      Match scoring, screening answers, cover letters, emails, follow-ups
  schemas.py            Pydantic models every Gemini reply is validated against
sites/                  One module per job board (linkedin, internshala, naukri, indeed)
ats/                    One module per hiring system (greenhouse, lever, ashby, workday, generic)
email_drafts.py         Email-application drafting
```

## Troubleshooting

**"No config found at ..."** — you haven't copied `config.yaml.example` to
`config.yaml` yet. See step 4.

**"config.yaml still has placeholder values in ..."** — one of the `[bracketed]`
placeholders is still there. The message names each field.

**"GEMINI_API_KEY is not set"** — copy `.env.example` to `.env` and paste your
key. See step 3.

**"Could not reach Chrome at http://localhost:9222"** — you're in `cdp` mode but
Chrome isn't running with debugging enabled. Run `start_chrome.bat` (or
`./start_chrome.sh`) first and leave it open.

**A site keeps asking me to log in** — the site's page layout may have changed.
Check the newest screenshot in `screenshots/` and the log in `logs/` to see what
the assistant was looking at.

**It's refusing to run** — check the time. The default run window is 09:00–21:00
IST; set `limits.run_window.enforce: false` in `config.yaml` to bypass it.
