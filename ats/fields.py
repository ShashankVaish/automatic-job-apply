"""The shared form-filling engine used by every ATS filler.

Strategy: walk every visible form control, work out what it's asking for from
its label / name / placeholder, and fill it from config.yaml. Anything we can't
classify becomes a screening question for Gemini, which is told the question,
the allowed options, the profile and the job description.

Nothing here invents facts. Unclassified required fields and low-confidence
answers flag the job needs_review instead of being guessed at.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Iterable

from ai.gemini_client import GeminiClient
from config import Config, ResumeVariant
from models import FillReport, Job

log = logging.getLogger(__name__)

# Controls we never touch.
IGNORED_TYPES = {"hidden", "submit", "button", "reset", "image", "password"}

# Anything matching this is a credential field - we never type into it.
CREDENTIAL_RE = re.compile(r"password|passwd|\bpin\b|otp|cvv|captcha", re.I)


@dataclass
class FillContext:
    """Everything a filler needs to fill one form."""

    cfg: Config
    job: Job
    resume: ResumeVariant
    gemini: GeminiClient
    report: FillReport
    pacer: Any = None
    answer_questions: bool = True

    def pace(self) -> None:
        if self.pacer is not None:
            self.pacer.action()


# --------------------------------------------------------------------- labels


LABEL_JS = """
(el) => {
  const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  // 1. explicit <label for=id>
  if (el.id) {
    const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    if (l && clean(l.innerText)) return clean(l.innerText);
  }
  // 2. wrapping <label>
  const wrap = el.closest('label');
  if (wrap && clean(wrap.innerText)) return clean(wrap.innerText);
  // 3. aria
  const al = el.getAttribute('aria-label');
  if (clean(al)) return clean(al);
  const ab = el.getAttribute('aria-labelledby');
  if (ab) {
    const parts = ab.split(/\\s+/).map(i => document.getElementById(i))
      .filter(Boolean).map(n => clean(n.innerText));
    if (parts.join(' ').trim()) return clean(parts.join(' '));
  }
  // 4. nearest ancestor field wrapper that carries a label or legend
  let node = el.parentElement;
  for (let i = 0; i < 5 && node; i++, node = node.parentElement) {
    const l = node.querySelector('label, legend, .label, .application-label');
    if (l && !l.contains(el) && clean(l.innerText)) return clean(l.innerText);
  }
  // 5. fall back to the attributes
  return clean(el.getAttribute('placeholder') || el.getAttribute('name') || '');
}
"""


# The shared question above a radio/checkbox group. The hard part is not
# picking up one option's own label ("Yes") as the question, so we only accept
# a label/legend that contains no form control of its own.
GROUP_QUESTION_JS = """
(el) => {
  const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const name = el.getAttribute('name') || '';
  let node = el.parentElement;
  for (let i = 0; i < 8 && node; i++, node = node.parentElement) {
    const legend = node.querySelector(':scope > legend, legend');
    if (legend && !legend.querySelector('input, select, textarea')) {
      const t = clean(legend.innerText);
      if (t) return t;
    }
    const groupSize = name
      ? node.querySelectorAll(`[name="${CSS.escape(name)}"]`).length
      : 1;
    if (groupSize >= 2 || i >= 2) {
      const cands = node.querySelectorAll(
        'label, .label, .application-label, .field-label, legend'
      );
      for (const c of cands) {
        if (c.querySelector('input, select, textarea')) continue;
        if (c.contains(el)) continue;
        const t = clean(c.innerText);
        if (t && t.length > 2) return t;
      }
    }
  }
  return '';
}
"""


def label_of(locator: Any) -> str:
    try:
        return (locator.evaluate(LABEL_JS) or "").strip()
    except Exception:
        return ""


def _attrs(locator: Any) -> dict[str, str]:
    try:
        return locator.evaluate(
            "(el) => ({name: el.name || '', id: el.id || '', type: (el.type||'').toLowerCase(),"
            " placeholder: el.placeholder || '', autocomplete: el.autocomplete || '',"
            " tag: el.tagName.toLowerCase(), required: el.required ? '1' : '',"
            " ariaRequired: el.getAttribute('aria-required') || ''})"
        )
    except Exception:
        return {}


def _haystack(label: str, attrs: dict[str, str]) -> str:
    return " ".join(
        [
            label,
            attrs.get("name", ""),
            attrs.get("id", ""),
            attrs.get("placeholder", ""),
            attrs.get("autocomplete", ""),
        ]
    ).lower()


def _is_required(label: str, attrs: dict[str, str]) -> bool:
    if attrs.get("required") or attrs.get("ariaRequired") == "true":
        return True
    return "*" in label or "(required)" in label.lower()


# ---------------------------------------------------------------- classifying

# Order matters: the first rule that matches wins, so put the specific ones
# (first name, resume link) above the broad ones (name, website).
RULES: list[tuple[str, re.Pattern[str]]] = [
    ("first_name", re.compile(r"first[\s_-]*name|given[\s_-]*name|\bfname\b", re.I)),
    ("last_name", re.compile(r"last[\s_-]*name|family[\s_-]*name|surname|\blname\b", re.I)),
    ("full_name", re.compile(r"full[\s_-]*name|^name$|your name|candidate name|applicant name", re.I)),
    ("email", re.compile(r"e-?mail", re.I)),
    ("phone", re.compile(r"phone|mobile|contact number|\btel\b|whatsapp", re.I)),
    ("linkedin", re.compile(r"linked[\s_-]*in", re.I)),
    ("github", re.compile(r"git[\s_-]*hub", re.I)),
    (
        "resume_link",
        re.compile(
            r"(resume|cv)[\s_-]*(link|url|drive)|link to (your )?(resume|cv)|"
            r"(resume|cv).*(google drive|dropbox)",
            re.I,
        ),
    ),
    (
        "portfolio",
        re.compile(
            r"portfolio|personal (site|website)|website|\bblog\b|other (url|link)s?|"
            r"behance|dribbble",
            re.I,
        ),
    ),
    (
        "cover_letter",
        re.compile(
            r"cover letter|motivation|letter"
            r"|why (do you|are you|should (you|we)|this)"
            r"|why would you|hire you",
            re.I,
        ),
    ),
    (
        "additional_info",
        re.compile(
            r"additional information|anything else|other information|tell us more|"
            r"is there anything",
            re.I,
        ),
    ),
    ("location", re.compile(r"\bcity\b|current location|your location|where are you|\bhometown\b", re.I)),
    ("salary", re.compile(r"salary|ctc|compensation|expected pay|stipend", re.I)),
    ("notice_period", re.compile(r"notice period|availability to (join|start)|when can you (join|start)", re.I)),
    ("graduation_year", re.compile(r"graduation (year|date)|year of (passing|graduation)|batch", re.I)),
    ("college", re.compile(r"college|university|institute|school name", re.I)),
    ("degree", re.compile(r"degree|qualification|course|major|field of study", re.I)),
    ("experience_years", re.compile(r"years of (relevant )?experience|total experience|\bexp\b.*years", re.I)),
]

# Fields that must never be auto-filled from the profile even if a rule matches.
NEVER_AUTOFILL = {"referral", "how did you hear"}


def classify(label: str, attrs: dict[str, str]) -> str:
    hay = _haystack(label, attrs)
    if CREDENTIAL_RE.search(hay):
        return "credential"
    for key in NEVER_AUTOFILL:
        if key in hay:
            return ""
    ac = attrs.get("autocomplete", "").lower()
    if ac in ("given-name",):
        return "first_name"
    if ac in ("family-name",):
        return "last_name"
    if ac in ("name",):
        return "full_name"
    if ac in ("email",):
        return "email"
    if ac in ("tel", "tel-national"):
        return "phone"
    for name, pattern in RULES:
        if pattern.search(hay):
            return name
    return ""


def profile_value(kind: str, ctx: FillContext) -> str:
    p = ctx.cfg.profile
    link = ctx.resume.public_link
    table = {
        "first_name": p.first_name(),
        "last_name": p.last_name(),
        "full_name": p.name,
        "email": p.email,
        "phone": p.phone,
        "linkedin": p.linkedin,
        "github": p.github,
        "resume_link": link,
        "portfolio": p.portfolio or link,
        "location": p.location,
        "salary": p.expected_salary or p.expected_stipend,
        "notice_period": p.notice_period,
        "graduation_year": str(p.graduation_year or ""),
        "college": p.college,
        "degree": p.degree,
        "experience_years": str(p.years_of_experience),
        "additional_info": "Resume: " + link if link else "",
    }
    return table.get(kind, "")


# ------------------------------------------------------------------- helpers


def _visible(locator: Any, timeout_ms: int = 800) -> bool:
    try:
        return bool(locator.is_visible(timeout=timeout_ms))
    except Exception:
        return False


def _enabled(locator: Any) -> bool:
    try:
        return bool(locator.is_enabled(timeout=500))
    except Exception:
        return False


def _already_filled(locator: Any) -> bool:
    try:
        return bool((locator.input_value(timeout=800) or "").strip())
    except Exception:
        return False


def set_text(locator: Any, value: str) -> bool:
    """Type into a text input or textarea. Returns True if it took."""
    try:
        locator.scroll_into_view_if_needed(timeout=2500)
    except Exception:
        pass
    try:
        locator.click(timeout=2500)
    except Exception:
        pass
    try:
        locator.fill(value, timeout=4000)
        return True
    except Exception as exc:
        log.debug("fill failed, trying type(): %s", exc)
    try:
        locator.press_sequentially(value, delay=25, timeout=6000)
        return True
    except Exception as exc:
        log.debug("type failed: %s", exc)
        return False


def best_option(wanted: str, options: Iterable[str]) -> str | None:
    """Match Gemini's answer to a real option, tolerantly."""
    opts = [o for o in options if o is not None]
    w = (wanted or "").strip().lower()
    if not w:
        return None
    for o in opts:
        if o.strip().lower() == w:
            return o
    for o in opts:
        ol = o.strip().lower()
        if ol and (w in ol or ol in w):
            return o
    # Yes/No questions where the option text is longer ("Yes, I am authorized").
    if w in ("yes", "no"):
        for o in opts:
            if o.strip().lower().startswith(w):
                return o
    return None


# ----------------------------------------------------------- resume handling


def upload_resume(scope: Any, ctx: FillContext) -> bool:
    """Attach the chosen resume PDF to every file input on the form."""
    pdf = ctx.resume.abs_pdf_path
    if not pdf.is_file():
        ctx.report.flag("resume PDF missing: " + str(pdf))
        return False

    done = 0
    inputs = scope.locator("input[type='file']")
    try:
        count = inputs.count()
    except Exception:
        count = 0

    for i in range(count):
        inp = inputs.nth(i)
        attrs = _attrs(inp)
        hay = _haystack(label_of(inp), attrs)
        # Don't put a resume in a photo or transcript slot.
        if re.search(r"photo|picture|avatar|transcript|certificate|portfolio file", hay):
            continue
        # Cover-letter file slots are fine to leave empty; we use the text box.
        if "cover" in hay:
            continue
        try:
            inp.set_input_files(str(pdf), timeout=15000)
            done += 1
            log.info("Uploaded %s to file input %d", pdf.name, i)
        except Exception as exc:
            log.debug("file input %d rejected the upload: %s", i, exc)

    if done:
        ctx.report.resume_uploaded = True
        return True
    if count:
        ctx.report.flag("could not upload the resume to any file input")
    return False


# ------------------------------------------------------ standard text fields


def fill_text_fields(scope: Any, ctx: FillContext) -> list[Any]:
    """Fill everything we recognise. Returns the controls we did NOT fill,
    for the screening-question pass."""
    leftovers: list[Any] = []
    controls = scope.locator(
        "input:not([type='file']), textarea, select"
    )
    try:
        count = controls.count()
    except Exception:
        return leftovers

    log.info("Scanning %d form controls", count)
    seen_kinds: set[str] = set()

    for i in range(min(count, 180)):
        ctrl = controls.nth(i)
        attrs = _attrs(ctrl)
        ftype = attrs.get("type", "")
        tag = attrs.get("tag", "")

        if ftype in IGNORED_TYPES:
            continue
        if not _visible(ctrl) or not _enabled(ctrl):
            continue
        # Radios and checkboxes are handled as questions, grouped by name.
        if ftype in ("radio", "checkbox"):
            leftovers.append(ctrl)
            continue

        label = label_of(ctrl)
        kind = classify(label, attrs)

        if kind == "credential":
            log.info("Refusing to touch credential-like field: %s", label or attrs.get("name"))
            continue

        if kind == "cover_letter":
            leftovers.append(ctrl)   # written by Gemini in a later pass
            continue

        if not kind:
            leftovers.append(ctrl)
            continue

        # A second "name" box after we've filled first+last is usually something
        # else; leave it to the question pass rather than duplicating.
        if kind in seen_kinds and kind not in ("portfolio",):
            leftovers.append(ctrl)
            continue

        value = profile_value(kind, ctx)
        if not value:
            if _is_required(label, attrs):
                ctx.report.flag("required field left empty: " + (label or kind))
            continue

        # Never overwrite something that already has a value - the page may
        # have prefilled it, or an earlier pass (e.g. Lever's named fields)
        # already put the right thing there.
        if _already_filled(ctrl):
            seen_kinds.add(kind)
            continue

        if tag == "select":
            leftovers.append(ctrl)   # dropdowns go through the option matcher
            continue

        if set_text(ctrl, value):
            seen_kinds.add(kind)
            ctx.report.note(kind, value)
            if kind == "resume_link":
                ctx.report.resume_link_placed = True
            elif kind in ("portfolio", "additional_info") and ctx.resume.public_link in value:
                ctx.report.resume_link_placed = True
            ctx.pace()
        else:
            ctx.report.unknown_fields.append(label or kind)

    return leftovers


# --------------------------------------------------------- cover letter box


def fill_cover_letter(scope: Any, ctx: FillContext) -> bool:
    """Find the cover-letter textarea and have Gemini write into it."""
    areas = scope.locator("textarea")
    try:
        count = areas.count()
    except Exception:
        return False

    for i in range(count):
        area = areas.nth(i)
        if not _visible(area) or not _enabled(area):
            continue
        attrs = _attrs(area)
        label = label_of(area)
        if classify(label, attrs) != "cover_letter":
            continue
        if _already_filled(area):
            return False
        try:
            letter = ctx.gemini.cover_letter(
                title=ctx.job.title,
                company=ctx.job.company,
                description=ctx.job.description,
                resume_link=ctx.resume.public_link,
            )
        except Exception as exc:
            log.warning("Gemini could not write a cover letter: %s", exc)
            ctx.report.flag("cover letter generation failed: " + type(exc).__name__)
            return False
        if set_text(area, letter):
            ctx.report.cover_letter = True
            ctx.report.note("cover letter", "{0} words".format(len(letter.split())))
            # The letter always ends with the resume link, so it counts.
            if ctx.resume.public_link and ctx.resume.public_link in letter:
                ctx.report.resume_link_placed = True
            ctx.pace()
            return True
        ctx.report.flag("could not type the cover letter")
        return False
    return False


# ------------------------------------------------------ screening questions


def _select_options(sel: Any) -> list[str]:
    try:
        return [
            (t or "").strip()
            for t in sel.locator("option").all_text_contents()
            if (t or "").strip()
        ]
    except Exception:
        return []


def _group_name(attrs: dict[str, str]) -> str:
    return attrs.get("name") or attrs.get("id") or ""


def answer_questions(scope: Any, ctx: FillContext, leftovers: list[Any]) -> None:
    """Send each unrecognised field to Gemini as a screening question."""
    if not ctx.answer_questions:
        for ctrl in leftovers:
            lbl = label_of(ctrl)
            if lbl:
                ctx.report.unknown_fields.append(lbl)
        return

    handled_groups: set[str] = set()

    for ctrl in leftovers:
        attrs = _attrs(ctrl)
        tag = attrs.get("tag", "")
        ftype = attrs.get("type", "")
        label = label_of(ctrl)

        if not _visible(ctrl) or not _enabled(ctrl):
            continue

        # --- dropdowns -------------------------------------------------
        if tag == "select":
            options = [o for o in _select_options(ctrl) if not _is_placeholder_option(o)]
            if not options:
                continue
            if _already_filled(ctrl):
                continue
            _ask_and_apply(ctx, ctrl, label, "select", options)
            continue

        # --- radio / checkbox groups ----------------------------------
        if ftype in ("radio", "checkbox"):
            group = _group_name(attrs)
            if group and group in handled_groups:
                continue
            siblings, options, question = _radio_group(scope, ctrl, group, label)
            if not options:
                continue
            handled_groups.add(group)
            _ask_and_apply_group(ctx, siblings, options, question, ftype)
            continue

        # --- free text / number ---------------------------------------
        if not label:
            continue
        if _already_filled(ctrl):
            continue
        kind = "number" if ftype == "number" else "text"
        _ask_and_apply(ctx, ctrl, label, kind, [])


def _is_placeholder_option(text: str) -> bool:
    t = text.strip().lower()
    return t in ("", "-", "--", "select", "select...", "select an option", "choose", "choose...",
                 "please select", "none", "-- select --")


def _radio_group(
    scope: Any, ctrl: Any, group: str, label: str
) -> tuple[list[Any], list[str], str]:
    """Collect every input in this radio/checkbox group plus its option texts."""
    if group:
        try:
            members = scope.locator(
                "input[name='{0}']".format(group.replace("'", "\\'"))
            )
            n = members.count()
        except Exception:
            members, n = None, 0
    else:
        members, n = None, 0

    if not members or n == 0:
        return [ctrl], [label] if label else [], label

    siblings = [members.nth(i) for i in range(n)]
    options: list[str] = []
    for s in siblings:
        txt = label_of(s)
        options.append(txt)

    question = ""
    try:
        question = (ctrl.evaluate(GROUP_QUESTION_JS) or "").strip()
    except Exception:
        question = ""
    if not question:
        question = label or group

    # Drop the per-option text from the question if they got merged.
    options = [o for o in options if o]
    return siblings, options, question


def _ask_and_apply(
    ctx: FillContext, ctrl: Any, question: str, kind: str, options: list[str]
) -> None:
    answer = _ask(ctx, question, kind, options)
    if answer is None:
        ctx.report.unknown_fields.append(question)
        return
    if answer.kind == "skip":
        ctx.report.flag("left unanswered: " + question[:80])
        ctx.report.unknown_fields.append(question)
        return

    if kind == "select":
        choice = best_option(answer.value, options)
        if choice is None:
            ctx.report.flag("no matching dropdown option for: " + question[:80])
            ctx.report.unknown_fields.append(question)
            return
        try:
            ctrl.select_option(label=choice, timeout=4000)
            ctx.report.question(question, choice)
        except Exception as exc:
            log.debug("select_option failed: %s", exc)
            ctx.report.flag("could not set dropdown: " + question[:80])
        ctx.pace()
        return

    value = answer.value.strip()
    if kind == "number":
        digits = re.sub(r"[^0-9.]", "", value)
        value = digits or "0"
    if not value:
        ctx.report.unknown_fields.append(question)
        return
    if set_text(ctrl, value):
        ctx.report.question(question, value)
    else:
        ctx.report.flag("could not type answer for: " + question[:80])
    ctx.pace()


def _ask_and_apply_group(
    ctx: FillContext,
    siblings: list[Any],
    options: list[str],
    question: str,
    ftype: str,
) -> None:
    answer = _ask(ctx, question, ftype, options)
    if answer is None or answer.kind == "skip":
        ctx.report.flag("left unanswered: " + question[:80])
        ctx.report.unknown_fields.append(question)
        return

    wanted = answer.values if (ftype == "checkbox" and answer.values) else [answer.value]
    chosen: list[str] = []
    for want in wanted:
        match = best_option(want, options)
        if match is None:
            continue
        idx = options.index(match)
        if idx >= len(siblings):
            continue
        try:
            siblings[idx].check(timeout=4000)
            chosen.append(match)
        except Exception as exc:
            log.debug("could not check option %r: %s", match, exc)
            try:
                siblings[idx].click(timeout=3000)
                chosen.append(match)
            except Exception:
                pass

    if chosen:
        ctx.report.question(question, ", ".join(chosen))
    else:
        ctx.report.flag("could not select an option for: " + question[:80])
        ctx.report.unknown_fields.append(question)
    ctx.pace()


def ensure_resume_link(scope: Any, ctx: FillContext) -> bool:
    """Guarantee the resume's public link ends up somewhere on the form.

    A dedicated resume-link field, an "additional information" box, or an empty
    extra URL field - in that order. If the cover letter already carries the
    link, `resume_link_placed` is already True and we leave the form alone.
    """
    link = ctx.resume.public_link
    if ctx.report.resume_link_placed or not link:
        return False

    preferred = ("resume_link", "additional_info", "portfolio")
    controls = scope.locator("input:not([type='file']), textarea")
    try:
        count = controls.count()
    except Exception:
        return False

    for wanted in preferred:
        for i in range(min(count, 180)):
            ctrl = controls.nth(i)
            attrs = _attrs(ctrl)
            if attrs.get("type", "") in IGNORED_TYPES:
                continue
            if not _visible(ctrl) or not _enabled(ctrl) or _already_filled(ctrl):
                continue
            label = label_of(ctrl)
            if classify(label, attrs) != wanted:
                continue
            value = link if wanted != "additional_info" else "Resume: " + link
            if set_text(ctrl, value):
                ctx.report.note("resume link", value)
                ctx.report.resume_link_placed = True
                ctx.pace()
                return True

    ctx.report.flag("no field accepted the resume link")
    return False


def _ask(ctx: FillContext, question: str, kind: str, options: list[str]) -> Any:
    """One Gemini call for one question. Flags needs_review when unconfident."""
    try:
        answer = ctx.gemini.answer_question(
            question=question,
            field_kind=kind,
            options=options,
            job_title=ctx.job.title,
            company=ctx.job.company,
            description=ctx.job.description,
            resume_link=ctx.resume.public_link,
        )
    except Exception as exc:
        log.warning("Gemini failed on question %r: %s", question[:60], exc)
        ctx.report.flag("unanswered question (AI error): " + question[:80])
        return None

    if not answer.confident:
        ctx.report.flag(
            "low-confidence answer: {0} -> {1}{2}".format(
                question[:60],
                (answer.value or ", ".join(answer.values))[:40],
                " (" + answer.note[:60] + ")" if answer.note else "",
            )
        )
    return answer
