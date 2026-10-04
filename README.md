# Job & Internship Application Assistant

A personal assistant that finds jobs and internships, scores them against your
profile with Google Gemini, picks the right resume, and fills in the application
form while you watch. **You** click Submit, unless you explicitly tell it
otherwise.

It runs on your own computer, in a visible browser window, for your own
applications. It never stores or types your passwords, it never invents
experience or qualifications you don't have, and it never sends an email by
itself.

---

## Contents

1. [What it does](#1-what-it-does)
2. [Install Python](#2-install-python)
3. [Set up the project](#3-set-up-the-project)
4. [Get a free Gemini API key](#4-get-a-free-gemini-api-key)
5. [Fill in your profile](#5-fill-in-your-profile)
6. [Prepare your resumes](#6-prepare-your-resumes)
7. [Choose a browser mode](#7-choose-a-browser-mode)
8. [Verify your setup](#8-verify-your-setup)
9. [First-time manual login](#9-first-time-manual-login)
10. [Applying to jobs](#10-applying-to-jobs)
11. [Cold email outreach](#11-cold-email-outreach)
12. [Gmail drafts (optional)](#12-gmail-drafts-optional)
13. [Follow-ups](#13-follow-ups)
14. [Seeing what happened](#14-seeing-what-happened)
15. [Safety rules built in](#15-safety-rules-built-in)
16. [Project layout](#16-project-layout)
17. [Running the tests](#17-running-the-tests)
18. [Troubleshooting](#18-troubleshooting)

---

## 1. What it does

**Finds jobs** on LinkedIn, Internshala, Naukri and Indeed, using the keywords
and locations you configure.

**Scores each one** with Gemini against your real profile, and skips anything
below your threshold — logging why, so you can check its judgement.

**Routes to the best apply route.** If a posting links out to the company's own
careers page, it follows the link and applies there — Greenhouse, Lever, Ashby,
SmartRecruiters, Workday, or a best-effort generic filler. It only uses a job
board's own flow (like LinkedIn Easy Apply) when there's no external page. If
the posting says to email a resume, it drafts the email for you.

**Fills the form**: your details from `config.yaml`, the resume variant Gemini
picked uploaded to the file input, your resume link in any resume/portfolio
field, and a tailored cover letter under 150 words. Screening questions go to
Gemini with your profile and the job description attached — and if the honest
answer isn't clear, it picks the truthful one and flags the job for you.

**Waits for you.** By default it fills everything, highlights the Submit button,
beeps, prints a summary of what it filled, and stops. You decide.

**Cold outreach** (separate command): drafts personal introduction emails to
companies that aren't advertising, from a list you supply. Drafts only — never
sends.

**Tracks everything** in SQLite, with CSV export, screenshots, dated logs and a
7-day report.

---

## 2. Install Python

You need **Python 3.11 or newer**.

- **Windows:** download from [python.org/downloads](https://www.python.org/downloads/).
  On the first installer screen, **tick "Add python.exe to PATH"** before clicking Install.
- **macOS:** `brew install python@3.12`, or download from python.org.
- **Linux:** `sudo apt install python3 python3-venv python3-pip`

Check it worked — open a **new** terminal and run:

```
python --version
```

You should see `Python 3.11.x` or higher. On macOS/Linux you may need `python3`
everywhere this README says `python`.

## 3. Set up the project

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

## 4. Get a free Gemini API key

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

## 5. Fill in your profile

```bash
# Windows
copy config.yaml.example config.yaml
# macOS / Linux
cp config.yaml.example config.yaml
```

Open `config.yaml` and replace **every `[bracketed]` placeholder** with your
real details — name, email, phone, city, degree, college, graduation year,
expected stipend/salary, notice period, your LinkedIn/GitHub/portfolio links,
your target roles and locations, and your skills.

Nothing about you lives anywhere else in the code. Everything the assistant says
on your behalf comes from this file, and Gemini is told on every single call
never to go beyond it.

While you're in there, set:

| Setting | What it does |
|---|---|
| `matching.match_threshold` | Skip jobs scoring below this (default 70) |
| `matching.auto_threshold` | `--auto` only submits at/above this (default 80) |
| `matching.dedupe_days` | Same company + title inside this window is a duplicate (default 30) |
| `sources.*.enabled` | Which job sites to use, plus keywords and locations |
| `limits.daily` | Applications per site per day |
| `limits.run_window` | Only run between these hours (default 09:00–21:00 IST) |
| `limits.delays` | Human-paced pauses between actions and applications |

## 6. Prepare your resumes

You need at least three PDF resume variants in `./resumes/`:

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
   leaving the role as **Viewer**.
4. Click **Copy link**.
5. Paste it into the matching `public_link:` in `config.yaml`.

If you skip the "Anyone with the link" step, recruiters hit a permission wall and
your application goes nowhere. **Test each link in a private/incognito window.**

## 7. Choose a browser mode

In `config.yaml` under `browser:`:

**`mode: "persistent"` (recommended to start)**
Playwright launches its own Chromium with a profile in `./browser_profile`. You
log in to the job sites once; the session is reused every later run.

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

## 8. Verify your setup

```bash
python main.py --check
```

This prints a table checking your config, every resume file and link, the
database, the browser mode, and your Gemini API key — the only command that
makes a real API call. Fix any `FAIL` rows before going further.

## 9. First-time manual login

```bash
python main.py --login
```

A browser window opens at each enabled site's login page and the terminal waits:

```
  LINKEDIN needs a manual login (attempt 1/3)
  Log in manually (email / Google / OTP), then press Enter.
```

Sign in **in the browser window** however you normally do — password, Google
sign-in, OTP — then press Enter in the terminal.

**This program never sees, types or stores your password.** It only reuses the
browser session you created yourself. Every later run checks whether you're
still signed in and pauses for a manual login again if a session expired, rather
than failing.

## 10. Applying to jobs

**Always test a new setup with `--dry-run` first.**

```bash
# Fill forms, screenshot everything, never submit
python main.py --dry-run

# DEFAULT: fill the form, highlight Submit, beep, wait for you to click it.
# Press "s" in the terminal to skip a job, "q" to abandon it.
python main.py --assist

# Fill the form, print a summary, ask y/n; the bot clicks Submit on "y"
python main.py --review

# Submit without asking - company career pages only, score >= auto_threshold.
# Never used on LinkedIn, Naukri or Indeed.
python main.py --auto

# Only some sources
python main.py --site linkedin,internshala --dry-run

# Cap the whole run at 5 applications
python main.py --max 5 --assist

# Fill one specific URL, to test a filler against a single form
python main.py --url "https://boards.greenhouse.io/company/jobs/123" --dry-run
```

### What each mode does with Submit

| Mode | Fills the form | Clicks Submit | Notes |
|---|---|---|---|
| `--dry-run` | yes | **never** | Screenshots every form |
| `--assist` *(default)* | yes | **you do** | Highlights Submit, beeps, waits up to 10 min |
| `--review` | yes | on your `y` | Shows a summary first |
| `--auto` | yes | yes | Career pages only, score ≥ 80, refuses anything flagged for review |

In assist mode, once you click Submit the assistant detects the confirmation
page and logs the job as applied. If no confirmation appears within 10 minutes it
logs `needs_review` instead of guessing.

## 11. Cold email outreach

For companies that aren't advertising a role. This replaces the old
apply-by-email and follow-up steps with a proper outreach workflow.

### Set up your target list

```bash
# Windows
copy inputs\companies.example.csv inputs\companies.csv
copy inputs\job_urls.example.txt inputs\job_urls.txt
# macOS / Linux
cp inputs/companies.example.csv inputs/companies.csv
cp inputs/job_urls.example.txt inputs/job_urls.txt
```

**`inputs/companies.csv`** — explicit targets:

```csv
company,email,contact_name,role,website,notes
Fixture Labs,careers@fixturelabs.example,,Frontend Developer Intern,https://fixturelabs.example,Met their engineer at a meetup
```

Only `company` is required, but without an `email` the row is logged as
`needs_email` and skipped. `notes` is passed to Gemini as context, so put
anything true and specific there — it makes the email much better.

**`inputs/job_urls.txt`** — one careers page or job post per line. Each page is
opened and read for the company name, the role, and any address the company has
published. Lines starting with `#` are ignored.

### Draft the emails

```bash
python main.py --outreach              # draft for every target
python main.py --outreach --max 5      # cap this batch
```

Drafts land in `./outbox/` as readable text files, with a ledger at
`outbox/outreach.csv`. **Nothing is sent.** Read each one, edit it, and send it
yourself.

### Important: addresses are never guessed

The assistant only uses an address **you supplied** or one the company
**published on its own page**. It will not construct `firstname@company.com`.
Guessed addresses bounce, get flagged as spam, and burn your real name with that
company. Addresses like `noreply@`, `support@`, `sales@` and `legal@` are
rejected outright; `careers@`, `jobs@`, `hiring@` and similar are preferred.

A company with no published address is recorded as `needs_email` so you can go
and find the right person yourself.

### Outreach limits

| Setting | Default | What it does |
|---|---|---|
| `outreach.dedupe_days` | 90 | Never contact the same company twice inside this window |
| `outreach.max_followups` | 2 | Depth of the follow-up ladder (0 disables) |
| `limits.daily.outreach` | 20 | Cold emails drafted per day |
| `outreach.outbox_dir` | `./outbox` | Where drafts and ledgers go |

### Outreach follow-ups

```bash
python main.py --outreach-followups
python main.py --outreach-report
```

Six days after the last contact, a follow-up is drafted. The second and final
one says explicitly that it's the last — then that company is left alone. Still
drafts only.

## 12. Gmail drafts (optional)

Off by default. When enabled, every email the assistant writes is *also* created
in your Gmail Drafts folder, with the resume attached, so you can send it from
your phone.

It requests only the **compose** scope, and no code in this project reaches
Gmail's send endpoint — there's a test that asserts it.

### Setting it up

1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Click the project dropdown at the top → **New Project**. Name it anything
   (e.g. "job-assistant") and click **Create**.
3. With that project selected, go to **APIs & Services → Library**, search for
   **Gmail API**, open it and click **Enable**.
4. Go to **APIs & Services → OAuth consent screen**.
   - User type: **External**, then **Create**.
   - App name: anything. User support email: your own address.
   - Developer contact: your own address. Save and continue.
   - **Scopes:** skip this page (the app requests its scope at runtime).
   - **Test users:** click **Add users** and add **your own Google address**.
     This matters — an unpublished app only works for listed test users.
   - Save and continue.
5. Go to **APIs & Services → Credentials → Create Credentials → OAuth client ID**.
   - Application type: **Desktop app**. Name it anything. Click **Create**.
6. Click **Download JSON** on the credential you just made. Save it in the
   project folder as **`credentials.json`**.
7. In `config.yaml`, set:

   ```yaml
   email:
     gmail_api: true
   ```

8. The next time the assistant writes an email, a browser window opens asking you
   to allow draft access. Approve it once; the token is cached in
   `./data/gmail_token.json` and later runs are silent.

You'll see a "Google hasn't verified this app" warning. That's expected for your
own unpublished app — click **Advanced → Go to ... (unsafe)**. It's your project,
your credentials and your data.

`credentials.json` and the token file are both in `.gitignore`. Never commit
them.

## 13. Follow-ups

For jobs you actually applied to (not cold outreach):

```bash
python main.py --followups
```

Six days after an application with no reply, a Gemini-drafted 3-line follow-up is
added to `followups.csv`, marked for email or LinkedIn depending on the source.
Each application is queued once. Skipped and failed jobs never get one.
**Nothing is sent.**

## 14. Seeing what happened

```bash
python main.py --report            # last 7 days, per-site totals and detail
python main.py --report-days 30    # a longer window
python main.py --outreach-report   # cold outreach activity
python main.py --export            # write applications.csv
```

Every run also writes:

| Path | What's in it |
|---|---|
| `logs/run_YYYY-MM-DD.log` | Full detail of what happened |
| `screenshots/` | Every form, confirmation and problem |
| `applications.csv` | The whole tracking database as a spreadsheet |
| `drafts/` | Email applications, for you to send |
| `outbox/` | Cold outreach drafts and ledgers |
| `followups.csv` | Follow-up messages for applications with no reply |

At the end of each run you get a per-site summary table: found, applied,
drafted, needs review, skipped, failed.

## 15. Safety rules built in

**Nothing is submitted without you**, except in `--auto`, which is limited to
company career pages scoring 80+, and refuses any form flagged for review.

**Nothing is ever emailed.** Email applications, cold outreach and follow-ups
are all drafts. Gmail integration only ever creates a draft.

**No passwords.** Logins are always manual. Any field whose label looks like a
password, OTP, PIN or CVV is refused outright, even if something else would have
matched it.

**No invented credentials.** On every Gemini call the prompt forbids claiming
experience, skills, degrees, certifications or availability that aren't in your
`config.yaml`. When the honest answer isn't clear, it picks the truthful one,
marks itself unconfident, and the job is flagged `needs_review` for you.

**No guessed email addresses** for outreach.

**Rate limits and pacing.** Per-site daily limits, a 09:00–21:00 IST run window,
and random human-paced delays (3–10s between actions, 45–120s between
applications).

**Backs off when challenged.** On any CAPTCHA, "unusual activity" or rate-limit
message: screenshot, stop that site for the rest of the day, carry on with the
others.

**Fails safely.** Every job is wrapped in its own error handling, so one bad form
can't end a run. Unknown multi-step forms are screenshotted and flagged rather
than half-submitted. Workday stops at its account wall instead of filling a
five-step form it can't finish.

**Watchable.** The browser is always headed. There is no headless mode.

## 16. Project layout

```
main.py                 CLI entry point, reports, run orchestration
config.py               Loads config.yaml + .env, validates your profile
config.yaml.example     Template for your profile - copy to config.yaml
.env.example            Template for secrets - copy to .env
db.py                   SQLite tracking, dedupe, CSV export
models.py               Job, FillReport, ApplyOutcome, ApplyTarget
limits.py               Run window, daily caps, run cap
runner.py               The per-job workflow and the four submit modes
interact.py             Terminal interaction for assist and review mode
email_drafts.py         Email applications and application follow-ups
outreach.py             Cold email outreach and its follow-up ladder
gmail_client.py         Gmail OAuth and draft creation (never sends)
start_chrome.bat/.sh    Launch Chrome with remote debugging for CDP mode
browser/
  launcher.py           Persistent-profile and CDP browser startup
  login.py              Logged-out detection, manual login, CAPTCHA detection
  human.py              Human-paced delays, beeps, highlighting, screenshots
ai/
  gemini_client.py      Scoring, screening answers, cover letters, emails
  schemas.py            Pydantic models every Gemini reply is validated against
sites/
  base.py               JobSource base class: search / get_job_details / apply
  linkedin.py  internshala.py  naukri.py  indeed.py
ats/
  base.py               ATSFiller base class (fillers can never submit)
  fields.py             The shared form-filling engine
  detect.py             Works out which hiring system a page runs
  greenhouse.py  lever.py  ashby.py  smartrecruiters.py  workday.py  generic.py
inputs/                 Your outreach target lists (examples committed)
tests/                  231 tests, all offline
```

## 17. Running the tests

```bash
pip install pytest
python -m pytest
```

The suite is fully offline: form filling runs against saved HTML fixtures in
`tests/fixtures/`, Gemini is replaced by `tests/mock_gemini.py`, and the test
browser context **aborts any request that isn't a local file** — so a test can
never touch a real job site. (That guard caught a real bug where relative links
were being resolved against the live site.)

`tests/test_spec_compliance.py` locks in the documented defaults and the safety
rules above, so they can't be loosened by accident.

## 18. Troubleshooting

**"No config found at ..."** — copy `config.yaml.example` to `config.yaml`. See
step 5.

**"config.yaml still has placeholder values in ..."** — one of the `[bracketed]`
placeholders is still there. The message names each field.

**"GEMINI_API_KEY is not set"** — copy `.env.example` to `.env` and paste your
key. See step 4.

**"Could not reach Chrome at http://localhost:9222"** — you're in `cdp` mode but
Chrome isn't running with debugging enabled. Run `start_chrome.bat` (or
`./start_chrome.sh`) first and leave it open.

**"Outside the run window"** — the default window is 09:00–21:00 IST. Either wait,
set `limits.run_window.enforce: false` in `config.yaml`, or pass
`--ignore-run-window` for a one-off.

**A site keeps asking me to log in** — job boards change their markup often, and
the login check errs on the side of asking. Check the newest screenshot in
`screenshots/` and the log in `logs/` to see what the assistant was looking at.

**"no job cards found - selectors may have changed"** — the same cause. The
selectors live at the top of each file in `sites/`, each with several fallbacks;
add the current one and it'll work again.

**A job was skipped and I disagree** — run `python main.py --report`. The skip
reason includes Gemini's explanation and any red flags. Adjust
`matching.match_threshold` if it's being too strict.

**Everything comes back `needs_review`** — usually a missing resume PDF or an
unreachable resume link. Run `python main.py --check` first.
