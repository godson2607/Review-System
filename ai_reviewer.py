"""AI reviewer: sends a student's submission to the configured AI provider and gets structured feedback back.
Works with Claude, OpenAI, Gemini, Groq, OpenRouter, DeepSeek or any OpenAI-compatible server."""
import base64
import io
import json

import anthropic
import openai
from pypdf import PdfReader

import settings
from file_extract import extract

FEEDBACK_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "description": "0-100"},
        "verdict": {"type": "string", "enum": ["pass", "needs_revision"]},
        "summary": {"type": "string"},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "mistakes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "where": {"type": "string"},
                    "explanation": {"type": "string"},
                    "fix": {"type": "string"},
                    "severity": {"type": "string", "enum": ["major", "minor"]},
                },
                "required": ["title", "where", "explanation", "fix", "severity"],
                "additionalProperties": False,
            },
        },
        "improvements": {"type": "array", "items": {"type": "string"}},
        "email_subject": {"type": "string"},
        "email_body": {"type": "string"},
    },
    "required": ["score", "verdict", "summary", "strengths", "mistakes", "improvements",
                 "email_subject", "email_body"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You are a senior mentor on the review team of an internship program. Interns finish a class, \
then get a short window to complete that module's task and upload their work. You review each submission and \
write the feedback email the intern receives.

How to review:
- Read every file. Judge the work against the module's task and rubric, not against an ideal professional project.
- Find real mistakes: bugs, wrong logic, missing requirements, security problems, bad practices, unclear code. \
For each one say exactly where it is (file, function or line), why it is a problem, and how to fix it.
- Note what the intern did well, specifically. Generic praise is useless.
- Suggest the few improvements that would teach them the most. Don't list everything you can think of.
- verdict "pass" means the core requirements of the task are met, even if there is room to improve. \
"needs_revision" means something required is missing or broken.
- Score 0-100: 90+ excellent, 75-89 good, 60-74 meets basics with clear gaps, below 60 incomplete.
- If the files don't relate to the task, are empty, or look like the task text pasted back, say so plainly \
and mark needs_revision.

How to write the email (email_body). This matters most: it must read exactly like a busy, experienced \
human mentor typed it after going through their work, never like generated text.
- Open with "Hi <first name>," then get straight to the point. No warm-up sentence about having reviewed it.
- Sound like a person talking: contractions (you've, it's, don't), plain everyday words, a mix of short and \
longer sentences. A short blunt sentence now and then is good ("This part breaks." / "Nice touch.").
- Be specific to THIS submission: quote a variable or function name, mention a file, describe what you saw \
when you read it. Specific detail is what makes feedback feel human.
- Give your honest opinion like a mentor would: "I'd move this into its own function", "honestly this \
loop is doing too much", "you got the hard part right".
- Keep it as long as the work deserves. A small task with two issues gets a short email. Don't pad.
- Mistakes: write them as a simple numbered list (1. 2. 3.), each one or two sentences of plain explanation \
plus the fix. Only the ones that matter. Everything else flows as normal paragraphs.
- Never use: markdown (#, **, backticks, bullets with *), emojis, em dashes or en dashes, headings like \
"Strengths:" / "Areas for improvement:", or phrases such as "Great job", "Overall,", "I hope this helps", \
"Keep up the good work", "delve", "robust", "leverage", "showcase", "commendable", "it's worth noting", \
"don't hesitate to reach out", "in conclusion", "a testament to", "seamless", "elevate".
- Don't list things in neat groups of three, don't summarise what you just said at the end, and don't \
balance every criticism with a compliment. Real reviewers are a bit uneven.
- If this is a resubmission, say what they fixed since last time and what's still left, like you remember it.
- End with one or two lines on the next step (what to fix and resubmit, or what to focus on next module).
- Don't sign off or add a signature; it is added automatically.
- email_subject: short and plain, the way a person would write it, e.g. "Module 2 - your REST API task" or \
"Feedback: Module 3 login page".
"""

# Phrases that make an email read as machine-written; removed from the final email as a safety net.
_AI_OPENERS = ("great job", "excellent work", "fantastic work", "well done on", "thank you for submitting",
               "thanks for submitting", "i have reviewed", "i've reviewed", "i reviewed your")
_AI_CLOSERS = ("i hope this helps", "hope this helps", "keep up the good work", "keep up the great work",
               "don't hesitate to reach out", "feel free to reach out", "happy coding", "best of luck")


def humanize(text):
    """Cleans formatting and stock phrases that give away generated text."""
    import re
    t = text.replace("\r\n", "\n").strip()
    t = re.sub(r"\s*[—–]\s*", ", ", t)            # em/en dashes -> comma
    t = t.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    t = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), t)  # bold
    t = re.sub(r"`([^`]+)`", r"\1", t)                         # inline code
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)        # headings
    t = re.sub(r"^\s*[*•]\s+", "- ", t, flags=re.M)       # bullets
    t = re.sub(r"[\U0001F300-\U0001FAFF☀-➿]️?", "", t)  # emojis

    paragraphs = [p for p in re.split(r"\n\s*\n", t) if p.strip()]
    # Drop a stock closing paragraph / sentence.
    while paragraphs and len(paragraphs[-1]) < 160 and paragraphs[-1].strip().lower().startswith(_AI_CLOSERS):
        paragraphs.pop()
    closers = "|".join(re.escape(c) for c in _AI_CLOSERS)
    if paragraphs:  # stock closing sentence at the very end
        paragraphs[-1] = re.sub(rf"\s*(?i:{closers})[^.!?\n]*[.!?]?\s*$", "", paragraphs[-1]).strip()
    openers = "|".join(re.escape(o) for o in _AI_OPENERS)
    for i in range(min(2, len(paragraphs))):  # stock opening sentence, kept only if nothing else follows it
        paragraphs[i] = re.sub(rf"^((?:hi|hello|hey)[^\n,]*,\s*)?(?i:{openers})[^.!?\n]*[.!?]\s+(?=\S)",
                               lambda m: m.group(1) or "", paragraphs[i], flags=re.I)
    return "\n\n".join(p for p in paragraphs if p.strip())

PROVIDERS = {
    # name: (label, OpenAI-compatible base URL (None for Claude), default model)
    "claude": ("Anthropic Claude", None, "claude-opus-5-5"),
    "openai": ("OpenAI", "https://api.openai.com/v1", "gpt-4o"),
    "gemini": ("Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai/", "gemini-2.5-flash"),
    "groq": ("Groq", "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
    "openrouter": ("OpenRouter", "https://openrouter.ai/api/v1", "openai/gpt-4o"),
    "deepseek": ("DeepSeek", "https://api.deepseek.com/v1", "deepseek-chat"),
    "custom": ("Custom (OpenAI-compatible)", None, ""),
}


class ReviewError(Exception):
    pass


def _provider():
    name = settings.get("ai_provider") or "claude"
    if name not in PROVIDERS:
        name = "claude"
    label, base_url, default_model = PROVIDERS[name]
    if name == "custom":
        base_url = settings.get("ai_base_url")
        if not base_url:
            raise ReviewError("The custom provider needs a base URL. Set it in Admin -> Settings.")
    model = settings.get("ai_model") or default_model
    if not model:
        raise ReviewError("No AI model is set. Add one in Admin -> Settings.")
    key = settings.get("ai_api_key")
    if not key and name != "custom":
        raise ReviewError(f"The {label} API key is not set. Add it in Admin -> Settings.")
    return name, base_url, model, key


# ---------- Claude (Anthropic SDK) ----------

def _claude(model, key, system, content, max_tokens, schema, effort):
    client = anthropic.Anthropic(api_key=key, max_retries=3)
    output_config = {"effort": effort}
    if schema:
        output_config["format"] = {"type": "json_schema", "schema": schema}
    kwargs = dict(model=model, max_tokens=max_tokens, output_config=output_config,
                  messages=[{"role": "user", "content": content}])
    if system:
        kwargs["system"] = system
    try:
        response = client.beta.messages.create(
            betas=["server-side-fallback-2026-07-01"],
            extra_body={"fallbacks": "default"},  # re-run on a fallback model if a safety classifier declines
            **kwargs,
        )
    except anthropic.AuthenticationError:
        raise ReviewError("The Claude API key is invalid. Check Admin -> Settings.")
    except anthropic.NotFoundError:
        raise ReviewError(f"Claude model '{model}' was not found. Check Admin -> Settings.")
    except anthropic.BadRequestError as e:
        raise ReviewError(f"Claude rejected the request: {e.message}")
    except anthropic.RateLimitError:
        raise ReviewError("Claude API rate limit reached. Retry in a minute.")
    except anthropic.APIConnectionError:
        raise ReviewError("Could not reach the Claude API. Check the internet connection.")
    except anthropic.APIStatusError as e:
        raise ReviewError(f"Claude API error ({e.status_code}): {e.message}")

    if response.stop_reason == "refusal":
        raise ReviewError("The AI declined to review this submission. Please review it manually.")
    if response.stop_reason == "max_tokens":
        raise ReviewError("The AI response was cut off (too long). Retry, or lower the review depth.")
    return next((b.text for b in response.content if b.type == "text"), ""), response.model


# ---------- OpenAI / Gemini / Groq / OpenRouter / DeepSeek / custom ----------

def _pdf_text(b64):
    reader = PdfReader(io.BytesIO(base64.b64decode(b64)))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _to_openai_parts(content):
    parts = []
    for block in content:
        if block["type"] == "text":
            parts.append({"type": "text", "text": block["text"]})
        elif block["type"] == "image":
            src = block["source"]
            parts.append({"type": "image_url",
                          "image_url": {"url": f"data:{src['media_type']};base64,{src['data']}"}})
        elif block["type"] == "document":
            text = _pdf_text(block["source"]["data"]).strip()
            parts.append({"type": "text", "text": text or "(PDF had no extractable text)"})
    return parts


def _openai_compatible(name, base_url, model, key, system, content, max_tokens, schema):
    client = openai.OpenAI(api_key=key or "not-needed", base_url=base_url, max_retries=3)
    if schema:
        system = (system or "") + ("\n\nRespond with ONLY a JSON object (no markdown, no code fences) "
                                   "matching this JSON schema:\n" + json.dumps(schema))
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    parts = _to_openai_parts(content)
    # Plain string when there are no images: some providers reject the multi-part format.
    if all(p["type"] == "text" for p in parts):
        messages.append({"role": "user", "content": "\n\n".join(p["text"] for p in parts)})
    else:
        messages.append({"role": "user", "content": parts})

    kwargs = dict(model=model, messages=messages, max_tokens=max_tokens)
    if schema:
        kwargs["response_format"] = {"type": "json_object"}
    label = PROVIDERS[name][0]
    try:
        try:
            response = client.chat.completions.create(**kwargs)
        except openai.BadRequestError:
            # Newer OpenAI models want max_completion_tokens; some servers don't support response_format.
            kwargs.pop("max_tokens", None)
            kwargs["max_completion_tokens"] = max_tokens
            try:
                response = client.chat.completions.create(**kwargs)
            except openai.BadRequestError:
                kwargs.pop("response_format", None)
                response = client.chat.completions.create(**kwargs)
    except openai.AuthenticationError:
        raise ReviewError(f"The {label} API key is invalid. Check Admin -> Settings.")
    except openai.NotFoundError:
        raise ReviewError(f"{label} model '{model}' was not found. Check Admin -> Settings.")
    except openai.BadRequestError as e:
        raise ReviewError(f"{label} rejected the request: {e.message}")
    except openai.RateLimitError:
        raise ReviewError(f"{label} rate limit or quota reached. Retry later or check your plan/billing.")
    except openai.APIConnectionError:
        raise ReviewError(f"Could not reach {label} at {base_url}.")
    except openai.APIStatusError as e:
        raise ReviewError(f"{label} API error ({e.status_code}): {e.message}")

    choice = response.choices[0]
    if choice.finish_reason == "length":
        raise ReviewError("The AI response was cut off (too long). Retry this review.")
    return choice.message.content or "", response.model


def generate(system, content, max_tokens=16000, schema=None):
    """Calls whichever AI provider is configured. Returns (text, model_used)."""
    name, base_url, model, key = _provider()
    if name == "claude":
        return _claude(model, key, system, content, max_tokens, schema, settings.get("review_effort") or "medium")
    return _openai_compatible(name, base_url, model, key, system, content, max_tokens, schema)


def _parse_json(text):
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ReviewError("The AI returned an unreadable response. Retry this review.")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        raise ReviewError("The AI returned an unreadable response. Retry this review.")


def review(submission, previous=None):
    """Reviews one Submission. `previous` is the student's earlier reviewed attempt for this module, if any.
    Returns (feedback_dict, skipped_files)."""
    blocks, included, skipped = extract(submission.stored_path, submission.original_filename)
    module, user = submission.module, submission.user

    intro = [
        f"Intern: {user.name}",
        f"Module {module.position}: {module.title}",
        f"Attempt number: {submission.attempt}",
        f"Submitted late: {'yes' if submission.is_late else 'no'}",
        "",
        "TASK GIVEN TO THE INTERN:",
        module.instructions,
        "",
        "REVIEW RUBRIC / WHAT TO CHECK:",
        module.rubric or "(no extra rubric - judge against the task)",
    ]
    if submission.student_note:
        intro += ["", "NOTE FROM THE INTERN:", submission.student_note]
    if previous is not None and previous.feedback_json:
        prev = json.loads(previous.feedback_json)
        intro += ["", f"YOUR FEEDBACK ON THEIR PREVIOUS ATTEMPT (score {previous.score}):",
                  prev.get("summary", ""),
                  "Mistakes you pointed out: " + "; ".join(m.get("title", "") for m in prev.get("mistakes", []))]
    if skipped:
        intro += ["", "Files that could not be included in this review: " + ", ".join(skipped)]
    intro += ["", f"SUBMITTED FILES ({len(included)}):"]

    content = [{"type": "text", "text": "\n".join(intro)}] + blocks + [
        {"type": "text", "text": "Review this submission and write the feedback email."}]

    text, _ = generate(SYSTEM_PROMPT, content, schema=FEEDBACK_SCHEMA)
    fb = _parse_json(text)

    # Normalise, since non-Claude providers don't enforce the schema strictly.
    try:
        fb["score"] = max(0, min(100, int(fb.get("score", 0))))
    except (TypeError, ValueError):
        fb["score"] = 0
    if fb.get("verdict") not in ("pass", "needs_revision"):
        fb["verdict"] = "pass" if fb["score"] >= 60 else "needs_revision"
    for key in ("strengths", "mistakes", "improvements"):
        if not isinstance(fb.get(key), list):
            fb[key] = []
    fb["mistakes"] = [m for m in fb["mistakes"] if isinstance(m, dict)]
    fb.setdefault("summary", "")
    if not fb.get("email_body"):
        raise ReviewError("The AI did not write the feedback email. Retry this review.")
    fb["email_body"] = humanize(fb["email_body"])
    if not fb.get("email_subject"):
        fb["email_subject"] = f"Feedback on your Module {module.position} submission: {module.title}"
    return fb, skipped


def test_connection():
    """Used by the 'Test AI' button on the Settings page."""
    text, model = generate(None, [{"type": "text", "text": "Reply with just the word OK."}], max_tokens=300)
    return f"Connected. Model {model} replied: {text.strip()[:40]}"
