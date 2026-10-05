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
11. [Gmail setup](#11-gmail-setup)
12. [Cold email outreach](#12-cold-email-outreach)
13. [The follow-up](#13-the-follow-up)
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

**Cold email outreach** (separate command): for each job you matched, finds up
to three contacts — HR, founder and co-founder — from public sources, writes a
different email for each of them, shows you every one for approval, and sends
from your own Gmail. One follow-up after six days if nobody replies. If a
company replies, all outreach to them stops automatically.

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

## 11. Gmail setup

Outreach sends from your own Gmail through the official API, using OAuth. **Your
Gmail password is never asked for, never stored, and SMTP with a password is
never used.**

The app requests two permissions and nothing wider:

| Scope | Why |
|---|---|
| `gmail.send` | to send your outreach |
| `gmail.readonly` | to search your own mailbox for replies and bounce notices |

It never asks for `gmail.modify` or full account access, so it cannot delete or
alter anything in your mailbox. There's a test that fails if those scopes are
ever added.

### Creating the Google Cloud project

1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Click the project dropdown at the top → **New Project**. Name it anything
   (e.g. "job-assistant") and click **Create**. Make sure it's selected
   afterwards.
3. Go to **APIs & Services → Library**, search for **Gmail API**, open it and
   click **Enable**.
4. Go to **APIs & Services → OAuth consent screen**.
   - User type: **External**, then **Create**.
   - App name: anything. User support email: your own address.
   - Developer contact: your own address. **Save and continue**.
   - **Scopes:** skip this page — the app requests its scopes at runtime.
   - **Test users:** click **Add users** and add **your own Gmail address**.
     This matters: an unpublished app only works for listed test users.
   - **Save and continue**.
5. Go to **APIs & Services → Credentials → Create Credentials → OAuth client ID**.
   - Application type: **Desktop app**. Name it anything. **Create**.
6. Click **Download JSON** on the credential you just created, and save it in
   the project folder as **`credentials.json`**.

### The one-time consent

```bash
python main.py --gmail-auth
```

A browser window opens. You'll see a **"Google hasn't verified this app"**
warning — that's expected for your own unpublished project. Click
**Advanced → Go to ... (unsafe)**. It's your project, your credentials and your
mailbox.

Approve the two permissions. The refresh token is cached in `token.json` and you
won't be asked again. The command confirms which account was authorised.

`credentials.json` and `token.json` are both in `.gitignore`. **Never commit
them.**

### Optional: Hunter.io email finder

Off by default. If you have a [Hunter.io](https://hunter.io) key, put it in
`.env` as `HUNTER_API_KEY=...` and set `outreach.use_hunter: true` in
`config.yaml`. Only results with **confidence ≥ 85** are ever used — anything
less is discarded rather than risked.

## 12. Cold email outreach

For each job that scored at or above your threshold, this finds up to three
people at the company — HR, founder and co-founder — writes a different email
for each, and sends from your Gmail after you approve it.

### Where contacts come from

Tried in this order:

| # | Source | Trust |
|---|---|---|
| 1 | `inputs/contacts.csv` — contacts **you** supply | treated as verified |
| 2 | An address written into the job post | high |
| 3 | The company's careers / contact page | high |
| 4 | The about / team page, for founder and co-founder | medium |
| 5 | Hunter.io, if you enabled it | only at confidence ≥ 85 |

### Two rules that are never broken

**Email addresses are never guessed.** No `firstname@company.com`, no
`first.last@company.com`. A guessed address bounces, gets you flagged as spam,
and burns your real name with that company. If no valid work email is found, the
attempt is logged as `no_email_found` and that person is skipped.

**Personal addresses are never used.** Gmail, Outlook, Yahoo and the rest are
rejected even when published on the team page. So are `noreply@`, `support@`,
`sales@`, `legal@` and similar — mailboxes where no hiring human reads.

### Set up your lists

```bash
# Windows
copy inputs\contacts.example.csv inputs\contacts.csv
copy inputs\companies.example.csv inputs\companies.csv
copy inputs\blocklist.example.txt inputs\blocklist.txt
# macOS / Linux
cp inputs/contacts.example.csv inputs/contacts.csv
cp inputs/companies.example.csv inputs/companies.csv
cp inputs/blocklist.example.txt inputs/blocklist.txt
```

**`inputs/contacts.csv`** — people you already know about. These are used
first and trusted without further checks:

```csv
company,name,role,email,source_url,notes
Fixture Labs,Priya Nair,hr,priya.nair@fixturelabs.example,https://x/careers,Met at a meetup
Fixture Labs,Arjun Mehta,founder,arjun@fixturelabs.example,,Spoke at a conference
```

`role` is `hr`, `founder` or `cofounder`. Anything in `notes` is given to Gemini
as context — put something true and specific there and the emails get noticeably
better.

**`inputs/companies.csv`** — companies to approach even without a job post.

**`inputs/job_urls.txt`** — careers pages or job posts, one per line.

**`inputs/blocklist.txt`** — addresses or whole domains that must never be
emailed. A bare domain blocks everyone there.

### Queue, review and send

```bash
# DEFAULT. Find contacts, write the emails, queue them, then show you each
# one: y to send, n to skip, e to edit, q to stop.
python main.py --email-queue

# Only use inputs/contacts.csv - never opens a browser to go looking
python main.py --email-queue --no-discover

# Send everything already queued, without asking
python main.py --email-auto

# Preview the real emails in your own inbox first - every send is
# redirected to this one address
python main.py --email-auto --email-test-to you@gmail.com

# Cap a batch
python main.py --email-queue --max 5
```

In `--email-queue`, each email is shown in full with the recipient, their type,
the role, the resume variant and the subject, then:

```
  Send this? [y]es / [n]o / [e]dit / [q]uit:
```

`e` lets you rewrite the subject and body in the terminal, shows you the result,
and asks again before sending.

### What each recipient gets

| Recipient | Length | Tone and ask |
|---|---|---|
| **HR / recruiter** | 90–150 words | Formal and direct. "I'm applying for X, here's why I fit, resume attached." Asks to be considered. |
| **Founder** | 80–110 words | Shorter. Shows you understand what they're building, focuses on how you'd help them ship. Asks for a 10-minute call or a pointer to the right person. |
| **Co-founder** | 80–110 words | Tailored to their function — a CTO gets technical depth, a COO/CMO gets execution and results. |

Every email has a subject under 9 words containing the role name, one specific
detail about the company or role, two or three of your real skills, the resume
link, the PDF attached, and your name, phone and LinkedIn in the signature.

### Sending rules

All enforced in code, every one covered by a test:

| Rule | Default |
|---|---|
| Sending window | Mon–Fri, 09:30–12:30 IST |
| Daily cap | 25 emails, **never above 40** whatever the config says |
| Gap between sends | random 3–8 minutes |
| Per person | 1 email, ever (plus the single follow-up) |
| Per company | 3 people max — HR, founder, co-founder |
| Order | HR first; founder and co-founder a day later |
| Same address again | never inside 60 days |
| Reply check | your Gmail is searched for a reply from that company's domain **immediately before every send** |
| If they replied | all outreach to that company stops, queued emails included |
| If it bounces | that address is never used again |
| Blocklist | honoured at both queue and send time |

Queueing never sends. Sending outside the window simply doesn't happen — the
emails stay queued for the next run.

### Replies and bounces

```bash
python main.py --email-sync
```

Reads your mailbox, marks companies that replied (stopping further outreach to
them), and blacklists any address that bounced. This also runs automatically
before each individual send.

## 13. The follow-up

**Exactly one follow-up, ever.** Six days after the first email, if nobody
replied, one short 3–4 line follow-up goes to the **HR contact only**, in the
same Gmail thread. It says plainly that it's the last time you'll reach out.
There is never a second one — the config value is clamped to 1, so it can't be
turned into a sequence.

```bash
python main.py --email-followups                  # queue and review them
python main.py --email-followups --email-auto     # queue and send
```

Founders and co-founders never get a follow-up. Neither does a company that
replied.

## 14. Seeing what happened

```bash
python main.py --report            # applications: last 7 days, per-site totals
python main.py --report-days 30    # a longer window
python main.py --email-report      # outreach: sent, replied, bounced, reply rate
python main.py --contacts-report   # every contact found, and where it came from
python main.py --export            # write applications.csv
```

`--email-report` covers the last 30 days and shows the reply rate, counted
against delivered mail. `--contacts-report` also lists companies where no work
email could be found, so you know where to go looking yourself.

Every run also writes:

| Path | What's in it |
|---|---|
| `logs/run_YYYY-MM-DD.log` | Full detail of what happened |
| `screenshots/` | Every form, confirmation and problem |
| `applications.csv` | The whole tracking database as a spreadsheet |
| `outbox/` | A readable copy of every queued and sent email |
| `outbox/outreach.csv` | The outreach ledger: who, when, status, Gmail message id |

At the end of each run you get a per-site summary table: found, applied,
drafted, needs review, skipped, failed.

## 15. Safety rules built in

**Nothing is submitted without you**, except in `--auto`, which is limited to
company career pages scoring 80+, and refuses any form flagged for review.

**Email is sent only with your approval.** `--email-queue`, the default, shows
you every email and waits for a yes. `--email-auto` sends what you already
queued and reviewed. Sending lives in one module behind one explicit switch;
a test asserts that switch is passed from exactly one place in the codebase.

**Outreach stops itself.** A reply from a company ends all outreach to them. A
bounce blacklists that address permanently. Exactly one follow-up is possible,
ever. The daily cap cannot exceed 40 however the config is edited.

**No passwords.** Logins are always manual. Any field whose label looks like a
password, OTP, PIN or CVV is refused outright, even if something else would have
matched it.

**No invented credentials.** On every Gemini call the prompt forbids claiming
experience, skills, degrees, certifications or availability that aren't in your
`config.yaml`. When the honest answer isn't clear, it picks the truthful one,
marks itself unconfident, and the job is flagged `needs_review` for you.

**No guessed email addresses, and no personal ones.** Only an address you
supplied or one the company published is used.

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
outreach.py             Outreach queue, review loop, sending, the follow-up
contacts.py             Finding HR / founder / co-founder from public sources
gmail_client.py         Gmail OAuth, sending, reply and bounce detection
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
inputs/                 Your contact and target lists (examples committed)
tests/                  311 tests, all offline
```

## 17. Running the tests

```bash
pip install pytest
python -m pytest
```

The suite is fully offline: form filling runs against saved HTML fixtures in
`tests/fixtures/`, Gemini is replaced by `tests/mock_gemini.py`, Gmail and
Hunter.io by `tests/mock_gmail.py`, and the test browser context **aborts any
request that isn't a local file** — so a test can never touch a real job site or
send a real email. (That guard caught a real bug where relative links were being
resolved against the live site.)

The Gmail mock records what would have been sent, so the real sending code —
every gate, the window, the caps, the reply check, the threading — is genuinely
exercised without a single message leaving the machine.

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

**"Gmail needs a one-time browser consent"** — run `python main.py --gmail-auth`
once. If it fails, check that `credentials.json` is in the project folder and
that your own address is listed as a test user on the OAuth consent screen.

**Outreach says "no_email_found" for everything** — the companies don't publish
addresses. Add the right people to `inputs/contacts.csv` yourself, or enable
Hunter.io. The assistant will not guess addresses, by design.

**Nothing sends** — check the time. Cold email only goes out Mon–Fri
09:30–12:30 IST. Your emails stay queued; run `--email-auto` inside the window,
or pass `--ignore-run-window` for a one-off.

**"Nothing is due to send today"** — founder and co-founder emails are
deliberately scheduled a day after the HR one, so they're not all at once. Run
again tomorrow.
