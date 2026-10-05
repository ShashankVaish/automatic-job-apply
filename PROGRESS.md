# Build progress

Overnight unattended build log. **Everything in both specs is implemented and
tested. Start with the [Morning checklist](#morning-checklist) at the bottom.**

Last updated: 2026-10-05, end of the overnight run.

---

## Status at a glance

| Stage | Scope | State |
|---|---|---|
| 1 | Config, database, Gemini client, browser launcher + manual login | DONE |
| 2 | Internshala + Greenhouse + Lever end to end in `--dry-run` | DONE |
| 3 | Assist mode (+ review and auto) | DONE |
| 4 | LinkedIn, Naukri, Indeed discovery + routing to career pages | DONE |
| 5 | Report and tracking | DONE |
| 6 | Cold email outreach, **rebuilt to the real spec you sent** | DONE |
| 7 | Full spec review, README, this checklist | DONE |

**311 tests pass. Every one runs offline.** No site was visited, no login was
performed, no application was submitted, **no email was sent**, and the Gmail
OAuth consent screen was never opened.

```
python -m pytest        ->  311 passed
```

---

## The outreach rebuild

You sent the real section 8 partway through the night. The conservative version
I had built was replaced, not patched around. What changed:

| | Conservative version | Rebuilt to your spec |
|---|---|---|
| Sending | never sent, drafts only | **Gmail API over OAuth, really sends**, after you approve each email |
| Contacts | one address per company | **up to 3: HR, founder, co-founder**, with a `contacts` table |
| Discovery | your CSV + the company page | your CSV first, then job post, careers page, team page, then Hunter.io |
| Email content | one generic email | **three different emails**, one per recipient type, with the spec's lengths and tones |
| Follow-ups | a 2-step ladder | **exactly one**, HR only, same Gmail thread, clamped so a second is impossible |
| Review | none | **y / n / edit** per email in `--email-queue` |
| Reply handling | none | Gmail searched before every send; a reply stops that company |
| Bounces | none | detected, and that address is permanently blacklisted |

Kept from the conservative version, as you asked: `inputs/` target lists, the
`outbox/` folder, and `inputs/contacts.csv` as the first and most trusted
contact source.

### What was retired

Your spec said outreach replaces section 8 (email applications) and section 10
(follow-ups), so rather than leave two half-live systems:

- `email_drafts.py` is gone. A posting that says "email us your CV" now routes
  into the outreach queue — the address in a job post is exactly the first HR
  source outreach looks for anyway.
- `followups.csv` and `--followups` are gone, replaced by 8.5.
- The `email.drafts_dir` / `email.gmail_api` config block is gone.
- `gmail_client.create_draft` is gone; outreach sends directly.

---

## What exists now

~13,400 lines across 28 Python modules, 26 HTML fixtures and 9 test files.

### Core
| File | Purpose |
|---|---|
| `main.py` | CLI, reports, run orchestration |
| `config.py` | Loads `config.yaml` + `.env`, validates your profile |
| `db.py` | Applications, contacts, emails, replies; dedupe; CSV export; migrations |
| `models.py` | `Job`, `FillReport`, `ApplyOutcome`, `ApplyTarget`, `SiteBlocked` |
| `limits.py` | Run window, daily caps, the email sending window |
| `runner.py` | The per-job workflow and all four submit modes |
| `interact.py` | Non-blocking keypress handling for assist mode |
| `contacts.py` | Finding HR / founder / co-founder from public sources |
| `outreach.py` | Queue, review loop, sending, the single follow-up |
| `gmail_client.py` | Gmail OAuth, sending, reply and bounce detection |

### Browser, AI, sites, ATS
- `browser/`: `launcher.py` (persistent + CDP, always headed), `login.py`,
  `human.py`
- `ai/`: `gemini_client.py`, `schemas.py`
- `sites/`: `base.py`, `linkedin.py`, `internshala.py`, `naukri.py`, `indeed.py`
- `ats/`: `base.py`, `fields.py`, `detect.py`, `greenhouse.py`, `lever.py`,
  `ashby.py`, `smartrecruiters.py`, `workday.py`, `generic.py`

### Tests
| File | Covers | Count |
|---|---|---|
| `test_fillers.py` | Greenhouse + Lever end to end | 23 |
| `test_internshala.py` | Search, routing, in-site apply modal | 14 |
| `test_runner_dryrun.py` | Whole workflow in `--dry-run`, dedupe, limits, failure isolation | 11 |
| `test_modes.py` | assist / review / auto / dry-run submit rules | 25 |
| `test_boards.py` | LinkedIn, Naukri, Indeed discovery and routing | 25 |
| `test_ats_extra.py` | Ashby, SmartRecruiters, Workday, generic | 27 |
| `test_contacts.py` | Contact discovery, address rules, Hunter.io | 59 |
| `test_outreach.py` | Queue, tones, every sending rule, the follow-up | 54 |
| `test_spec_compliance.py` | Every documented default and safety rule | 73 |

Gmail and Hunter.io are mocked in `tests/mock_gmail.py`. The mock records what
*would* have been sent, so the real sending code — every gate, the window, the
caps, the reply check, the threading — is genuinely exercised with nothing
leaving the machine.

---

## Bugs found and fixed while testing

Real defects, not test problems.

1. **Relative links were resolved against the live site.** Caught by a test
   guard that aborts any non-`file://` request — the tests had been quietly
   hitting the real internshala.com and scraping its 404 page.
2. **Greenhouse's detector claimed SmartRecruiters pages** (both use
   `form#application-form`).
3. **Labels like "Name *" never classified**, so Ashby's name field stayed empty.
4. **The generic filler treated a hidden form as open** and picked "Apply now"
   as the submit button.
5. **Radio groups used an option's own label as the question** — Lever's "Are
   you comfortable writing SQL?" arrived as "Yes" and was answered "No".
6. **Already-filled fields were being overwritten.**
7. **The 150-word cover-letter limit was never enforced.**
8. **Unreplaced `[placeholder]` values were typed into forms.**
9. **`--report` clipped dates to `2026-10-0?`** (the bug you reported).
10. **Tests wrote into the real `./outbox`** — the path was hardcoded.
11. **`edit_body` used bare `input()`** instead of the injected prompt, so the
    `e` branch of the review loop couldn't be driven or tested.
12. **Dead config and dead code left after the rebuild** — `EmailCfg`,
    `create_draft` — removed rather than left to mislead.

---

## Decisions I made

1. **Daily limits count `applied` + `drafted` + `needs_review`, not `skipped`.**
   *(approved)*
2. **One DB row per job URL, updated in place.** *(approved)*
3. **Ambiguous login state means logged out.** *(approved)*
4. **`models.py`** at the root so `sites/` and `ats/` share types without a
   circular import.
5. **`limits.py` and `runner.py`** keep that logic out of `main.py`.
6. **Fillers can never submit.** A test asserts no filler contains a submit
   click.
7. **Credential-like fields are never typed into.**
8. **Resume PDFs are never uploaded to photo/transcript file inputs.**
9. **"Website or Portfolio" gets your portfolio URL**, and the resume link is
   guaranteed to reach the form some other way.
10. **Workday stops at its account wall** rather than half-filling 5 steps.
11. **Naukri's in-site apply is left for you** — it submits on the first click.
12. **LinkedIn's "follow this company" box is unticked.**
13. **A dry run never reports status `applied`.**
14. **Apply-by-email jobs route into the outreach queue** instead of writing a
    separate draft file. One path, and it's the one that can actually send.
15. **`send_message` requires `confirmed=True`.** One explicit switch between
    "the user approved this" and "code decided to send". A test parses the AST
    and fails if it is passed from more than one place.
16. **Gmail scopes are `send` + `readonly` only** — never `gmail.modify` or full
    access, so the tool cannot alter or delete anything in your mailbox.
17. **`get_service(interactive=False)` for unattended paths**, so a background
    run can never block on a browser consent screen.
18. **Founder/co-founder emails are scheduled a day later** via a
    `scheduled_for` date on the queue row, not a sleep.
19. **The daily cap is clamped to 40 in code** (`hard_max`), and
    `max_followups` is clamped to 1, so neither can be loosened by editing the
    config.
20. **A bounce is explicitly not a reply** — bounce notices are filtered out of
    the reply search, or every bounce would look like interest.
21. **Bounce handling only acts on addresses this tool wrote to**, so an
    unrelated bounce in your mailbox can't blacklist someone.
22. **`contacts.csv` rows are trusted without validation** (you checked them),
    but everything discovered automatically must pass the work-email rules.
23. **A named founder with no published work email is logged, not emailed.** The
    name is recorded so you can find the address yourself.
24. **Outreach is driven by jobs already scored ≥ 70 in the database**, rather
    than scoring again during the outreach run. Cheaper, and it means outreach
    and applications can't disagree about a job.

---

## Needs me

1. **No live Gemini call has ever succeeded.** There's no key in `.env`. I did
   verify the failure path end to end: with an invalid key, `--email-queue`
   loads your contacts, finds HR and founder, correctly records the missing
   co-founder as `no_email_found`, fails to write the emails, records that, and
   exits cleanly without sending anything.

2. **Gmail was never authorised.** `credentials.json` doesn't exist and
   `--gmail-auth` was never run, as you instructed. Every Gmail path is tested
   against the mock only. **This is the biggest untested-for-real area:** the
   OAuth flow, the real send, threading a follow-up, and Gmail's search syntax
   for replies and bounces.

3. **Hunter.io was never called for real.** Off by default; tested with a mock.

4. **No real resume PDFs and no real `config.yaml`.**

5. **No site was visited and no login performed.** Selectors in `sites/` were
   written from knowledge of these sites' markup and tested against local HTML
   fixtures, **not** the live sites. Still the most likely thing to need fixing.

6. **The reply-detection search is a best guess at your mailbox.** It searches
   `from:@<domain>` over the last 60 days. If someone replies from a personal
   address rather than the company domain, it won't be spotted — check
   `--email-report` against your actual inbox for the first week.

---

## Git

- **13 commits tonight** (plus your "this first commit" = 14 total).
- **Everything is pushed.** `git log origin/main..HEAD` is empty, tree clean.
- Remote: `https://github.com/ShashankVaish/automatic-job-apply.git`, branch
  `main`. Never force-pushed, no history rewritten, no branches created.
- Git identity left untouched: `Shashank Vaish <vaishshashank3@gmail.com>`.
- Before every commit I ran `git status` and scanned the staged diff for
  `AIza`, `GEMINI_API_KEY=`, `HUNTER_API_KEY=`, `client_secret`,
  `refresh_token` and personal details. **Nothing sensitive is tracked** —
  `git ls-files` shows no `.env`, `credentials.json`, `token.json`,
  `config.yaml`, resume PDF, database or input file, and a scan of the full
  history found no keys.

```
561011e docs: rewrite README for the real outreach spec, and drop dead config
3cc9b40 feat(outreach): Gmail sending, queue review and the single follow-up
e943fe0 feat(outreach): contact discovery for HR, founder and co-founder (spec 8.2)
b026700 docs: correct the commit count and refresh the git log in PROGRESS
fcc00ff docs: final progress log, spec coverage and morning checklist
a546108 fix(config): reject every unreplaced [placeholder], not just six fields
646d0bb docs: rewrite README with all features, including email outreach setup
f8256ad test: add spec-compliance suite and enforce the cover-letter word limit
c8f6cbd feat(outreach): add cold email outreach (superseded by the real spec)
42f159f feat(email): add Gmail draft client, follow-ups and report hardening
9aadd59 feat(sites,ats): add LinkedIn, Naukri, Indeed and the remaining fillers
515584f test(modes): cover assist, review, auto and dry-run submit rules
b8b0cb9 fix(sites): resolve relative links against the current page
4f71e86 this first commit
```

`.gitignore` covers `.env`, `credentials.json`, `token.json`, `config.yaml`,
`browser_profile/`, `chrome_bot_profile/`, `resumes/*.pdf`, `data/*.db`,
`logs/`, `screenshots/`, `drafts/`, `outbox/`, `applications.csv`,
`followups.csv`, `inputs/contacts.csv`, `inputs/companies.csv`,
`inputs/job_urls.txt`, `inputs/blocklist.txt`, `.venv/`, `__pycache__/`,
`.pytest_cache/`. Only the `.example` input files are committed.

---

## Spec coverage

### Original spec

| Section | Status |
|---|---|
| 1. Tech stack | Python 3.11+, Playwright Chromium always headed, `google-genai`, key + model from `.env`, SQLite + CSV |
| 2. Profile in config.yaml | All fields, 3+ resume variants, nothing hard-coded |
| 3. Browser and login | Both modes, both Chrome scripts, manual login only |
| 4. Job sources | 4 boards + 6 ATS fillers, base class with the three methods |
| 5. Apply routing | Career page beats board flow beats email |
| 6. Per-job workflow | Scrape, dedupe, score, fill, questions, cover letter, submit, log |
| 7. Modes | `--assist` (default), `--review`, `--dry-run`, `--auto`, `--site`, `--max` |
| 8. Email applications | **Replaced** by the new outreach spec |
| 9. Limits and safety | Daily caps, 09:00–21:00 IST, paced delays, CAPTCHA backoff, per-job isolation, Gemini retry + validation |
| 10. Follow-ups | **Replaced** by 8.5 |
| 11. Output and logs | Dated logs, screenshots, run summary, `applications.csv`, `--report` |
| 12. Project structure | Matches, plus the modules listed above |
| 13. README | Rewritten, 18 sections |
| 14. Build order | Followed |

### Outreach spec (section 8)

| Section | Status |
|---|---|
| 8.1 Sending setup | Gmail API + OAuth, `credentials.json` → `token.json`, sends from your address, resume attached **and** linked. No password, no SMTP — a test asserts it |
| 8.2 Finding contacts | Up to 3 per matched job (≥ 70): HR, founder, co-founder. Job post → careers/contact page → about/team page. Hunter.io optional, off by default, confidence ≥ 85 only. Never pattern-guessed, never personal addresses, `no_email_found` logged. `contacts` table with company, name, role, email, source URL, date |
| 8.3 Writing the emails | Three different emails. Subject < 9 words with the role name; HR 90–150 words formal, founder 80–110 shorter, co-founder tailored to function. One specific company/role detail, 2–3 real skills, clear ask, resume link, phone + LinkedIn signature, first-name greeting with a company-team fallback |
| 8.4 Sending rules | `--email-queue` default with y/n/edit, `--email-auto`. 1 per person, 3 per company, HR first and founder/co-founder +1 day. Cap 25, hard max 40. Mon–Fri 09:30–12:30 IST, random 3–8 min gap. Never the same address in 60 days. Gmail reply check before each send → company marked `replied` and stopped. Every email logged with its Gmail message id. Bounces blacklist the address. `inputs/blocklist.txt` honoured |
| 8.5 Follow-up | Exactly one, HR only, same thread, after 6 days. A second is impossible — the config value is clamped. `--email-report` shows sent / replied / bounced / reply rate for 30 days |
| Testing | Gmail and Hunter.io mocked. `--email-test-to <address>` implemented. No consent screen opened |

---

# Morning checklist

Run these in order. Nothing before step 6 touches a real website, and nothing
before step 13 sends an email.

### 1. Open a terminal in the project and activate the environment

```powershell
cd G:\coding\goo\automatic-agent
.venv\Scripts\Activate.ps1
```

The venv already exists with every dependency and Chromium installed. If
PowerShell blocks the script:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

### 2. Confirm the build is sound (about 3 minutes, fully offline)

```powershell
python -m pytest
```

Expect `311 passed`. If anything fails, read it before going further.

### 3. Add your API key

```powershell
copy .env.example .env
notepad .env
```

Paste your key from https://aistudio.google.com/app/apikey into
`GEMINI_API_KEY=`. Leave `GEMINI_MODEL=gemini-2.5-flash`. Leave
`HUNTER_API_KEY=` empty unless you have one.

### 4. Fill in your profile

```powershell
copy config.yaml.example config.yaml
notepad config.yaml
```

Replace **every** `[bracketed]` value. Then put your three PDFs in `.\resumes\`
as `frontend.pdf`, `data.pdf`, `general.pdf`, upload each to Google Drive, set
sharing to **Anyone with the link → Viewer**, and paste the links into
`public_link:`. **Test one link in an incognito window.**

### 5. Verify everything, including the live API key

```powershell
python main.py --check
```

Every row must say `OK`. This is the first command that makes a real Gemini
call, so it proves your key works, and it names any `[placeholder]` you missed.

### 6. Log in to the job sites by hand

```powershell
python main.py --login
```

Sign in yourself in the browser window, press Enter in the terminal after each.

### 7. First real dry run — Internshala only, 3 jobs

```powershell
python main.py --dry-run --site internshala --max 3 --verbose
```

**This is the moment of truth for the selectors.** Watch the browser. If you see
`no result cards found - selectors may have changed`, Internshala's markup has
moved; check the newest file in `.\screenshots\`, tell me what you see, and I'll
fix it.

```powershell
python main.py --report
```

### 8. Dry-run the other boards one at a time

```powershell
python main.py --dry-run --site linkedin --max 3 --verbose
python main.py --dry-run --site naukri --max 3 --verbose
python main.py --dry-run --site indeed --max 3 --verbose
```

Enable each under `sources:` in `config.yaml` first — only LinkedIn and
Internshala are on by default.

### 9. Your first real application, with you in control

```powershell
python main.py --assist --site internshala --max 1
```

It fills the form, highlights Submit in red, beeps, and prints what it filled.
**Read the summary, especially anything under "NEEDS A LOOK."** Click Submit
yourself, or press `s` to skip.

---

## Now the email outreach

### 10. Set up Gmail (about 5 minutes in the browser)

Follow **section 11 of README.md** — it's step by step. In short:

1. [console.cloud.google.com](https://console.cloud.google.com/) → **New Project**.
2. **APIs & Services → Library** → search **Gmail API** → **Enable**.
3. **OAuth consent screen** → **External** → fill in your own email → skip
   Scopes → **Test users: add vaishshashank3@gmail.com** → Save.
4. **Credentials → Create Credentials → OAuth client ID → Desktop app** →
   **Download JSON**.
5. Save that file in the project folder as **`credentials.json`**.

### 11. Do the one-time consent

```powershell
python main.py --gmail-auth
```

You'll get a "Google hasn't verified this app" warning — expected for your own
project. **Advanced → Go to ... (unsafe)**, then approve. It should print
`Gmail authorised for vaishshashank3@gmail.com`.

### 12. Add your contacts, then build a queue and read it — don't send yet

```powershell
copy inputs\contacts.example.csv inputs\contacts.csv
copy inputs\blocklist.example.txt inputs\blocklist.txt
notepad inputs\contacts.csv
```

Put in any HR people, founders or co-founders you already know, with their
**work** emails. These are used first and trusted. Anything true and specific in
the `notes` column makes the emails noticeably better. Companies where nothing
is published will come back as `no_email_found` — that's working as intended.

```powershell
python main.py --email-queue --no-discover --max 3
```

`--no-discover` keeps it to `inputs/contacts.csv` and never opens a browser.
When it shows you each email, **press `n` for all of them** this first time.
Then read the full text on disk:

```powershell
dir outbox
notepad outbox\<Company>_hr.txt
```

Check the tone, that the "specific detail" is actually true, and that the resume
link and your phone/LinkedIn are right.

### 13. Send a test batch to yourself

```powershell
python main.py --email-auto --email-test-to vaishshashank3@gmail.com --max 3
```

Every email goes to **you** instead of the real recipient, with the resume
attached. Open them in Gmail, check how they render, then check they were
recorded:

```powershell
python main.py --email-report
```

> Those test sends count as "sent" and use up the one-email-per-person rule for
> those contacts. To re-test the same people, clear the email rows first:
>
> ```powershell
> python -c "import sys;sys.path.insert(0,'.');from db import open_db;d=open_db('./data/applications.db');d.conn.execute('DELETE FROM emails');d.conn.commit();print('cleared')"
> ```

### 14. Send for real, with approval on every email

```powershell
python main.py --email-queue --max 3
```

Press `y` only on emails you're happy with. `e` to edit, `n` to skip.

Remember the window: **Mon–Fri 09:30–12:30 IST**. Outside it nothing sends and
your emails stay queued. Founder and co-founder emails are deliberately
scheduled a day after the HR one, so run it again tomorrow for those.

### 15. Let discovery loose, once you trust it

```powershell
python main.py --email-queue --max 5
```

Without `--no-discover` it opens a browser and reads each company's careers and
team pages for contacts. Watch the first run.

### 16. After a few days

```powershell
python main.py --email-sync          # find replies and bounces
python main.py --email-followups     # queue the one follow-up, review it
python main.py --email-report        # sent / replied / bounced / reply rate
python main.py --contacts-report     # who was found, and where no email exists
python main.py --report-days 30      # applications
```

---

### Things worth knowing before you start

- **Selectors are the weak point.** Boards change markup constantly and I could
  not test against the live sites. Everything else has 311 tests behind it;
  this part doesn't, and it's where problems will appear first.
- **Gmail's real behaviour is the second weak point.** The OAuth flow, the real
  send, follow-up threading and the reply search are mock-tested only. Step 13
  exists specifically to shake those out safely.
- **Nothing sends without you**, unless you ask for `--email-auto` on a queue
  you already reviewed.
- **A reply stops outreach to that company automatically**, so go and answer
  people — `--email-report` tells you who replied.
- **Cold email is a real imposition on strangers.** The caps, the window, the
  one-per-person rule and the single follow-up keep this on the right side of
  that line. I would not raise them.
