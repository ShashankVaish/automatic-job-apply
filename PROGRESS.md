# Build progress

Living log for unattended work. **If a session is interrupted, read this file
first, then continue from "Next up".**

Last updated: 2026-10-04, overnight unattended run.

---

## Status at a glance

| Stage | Scope | State |
|---|---|---|
| 1 | Config, database, Gemini client, browser launcher + manual login | DONE, verified |
| 2 | Internshala + Greenhouse + Lever end to end in `--dry-run` | DONE, 48 tests pass |
| 3 | Assist mode (+ review mode) | IN PROGRESS |
| 4 | LinkedIn, Naukri, Indeed discovery + routing to career pages | not started |
| 5 | Email drafts, follow-ups, report | not started |
| 6 | Cold email outreach (replaces spec sections 8 and 10) | not started |
| 7 | Full spec review, README, morning checklist | not started |

## Next up

Finish stage 2: `ats/lever.py`, `ats/detect.py`, `sites/base.py`,
`sites/internshala.py`, `runner.py`, wire into `main.py`, build
`tests/fixtures/` + `tests/mock_gemini.py`, get `--dry-run` passing against
fixtures.

---

## Stage 1 - DONE

Config, SQLite tracking, Gemini client, browser launcher, manual login.

Files: `config.py`, `config.yaml.example`, `.env.example`, `db.py`, `main.py`,
`ai/gemini_client.py`, `ai/schemas.py`, `browser/launcher.py`,
`browser/login.py`, `browser/human.py`, `start_chrome.bat`, `start_chrome.sh`,
`README.md`, `requirements.txt`, `.gitignore`.

Tested: all modules import; `--check` table renders and correctly fails on
missing resumes + missing API key; DB dedupe (URL and company+title, 30-day
window, case/whitespace-insensitive), daily counters, site blocking, CSV export;
`--report` renders; headed Chromium launches with persistent profile and tears
down cleanly; CAPTCHA/logged-out detection; manual-login pause loop including
the 3-attempt give-up path.

---

## Decisions I made

Recorded as instructed when the spec didn't cover something.

1. **Daily limits count `applied` + `drafted` + `needs_review`, not `skipped`.**
   A job rejected on match score shouldn't burn a daily slot. *(approved)*
2. **One DB row per job URL, updated in place.** Re-runs don't duplicate rows.
   *(approved)*
3. **Ambiguous login state is treated as logged out.** *(approved)*
4. **Added `models.py` at the project root** (not in the spec's file list) to
   hold `Job`, `FillReport`, `ApplyOutcome`, `ApplyTarget`, `SiteBlocked`. Both
   `sites/` and `ats/` need these types; a root module avoids a circular import.
5. **Added `limits.py`** for the run window and daily/run caps, and `runner.py`
   for the per-job workflow, rather than putting that logic in `main.py`. Keeps
   `main.py` to CLI parsing and reporting.
6. **Fillers never submit.** An `ATSFiller` only fills; `runner.py` owns the
   submit decision per mode. Makes `--dry-run` safe by construction.
7. **Credential-like fields are never typed into.** Any field whose label/name
   matches `password|passwd|pin|otp|cvv|captcha` is skipped and logged, even if
   something else would have matched it.
8. **Cover-letter *file* upload slots are left empty** when a cover-letter text
   box exists; the generated letter goes in the text box.
9. **Resume PDFs are not uploaded to photo/transcript/certificate file inputs**,
   matched by label.

---

## Needs me

Things I could not do unattended. Nothing here blocks the rest of the build.

1. **The email outreach spec never arrived.** The overnight message says the
   feature "REPLACES section 8 and section 10" and ends with
   `[PASTE THE EMAIL OUTREACH SECTION HERE, from "# 8. Cold email outreach" to
   the end of "8.5 Follow-up"]` - the placeholder itself, not the content. I
   built a conservative interpretation (see stage 6 notes when written):
   draft-only, never sends, reuses config/db/Gemini. **Paste the real 8.x
   section and I will align the implementation to it.**
2. **No `GEMINI_API_KEY` in `.env`,** so no real Gemini call has ever run. All
   AI paths are tested against `tests/mock_gemini.py`. The live client is
   exercised only by `python main.py --check`, which you need to run yourself.
3. **No resume PDFs** in `./resumes/` and no real `config.yaml`. Tests use
   generated fixtures. You need to add your own.
4. **No logins performed, no real site visited.** Every site module's selectors
   are written from knowledge of these sites' markup, and tested against local
   HTML fixtures - not against the live sites. Expect selector tuning on your
   first real `--dry-run`. This is the single most likely thing to need fixing.
5. **Gmail OAuth consent screen was never opened.** The flow is implemented and
   mock-tested only.

---

## Test results

Filled in as stages complete.
