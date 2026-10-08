"""App settings stored in the database and edited from Admin -> Settings."""
from models import Setting, db

# key, label, type, default, help
GROUPS = [
    ("General", [
        ("program_name", "Program name", "text", "Full Stack Development Internship", "Shown on emails and certificates."),
        ("organization_name", "Organization name", "text", "Our Academy", "Issuer name on certificates."),
        ("base_url", "App URL", "text", "http://127.0.0.1:5000", "Used for links in emails and the certificate verify link."),
    ]),
    ("Internship rules", [
        ("internship_days", "Internship length (days)", "int", "30", ""),
        ("deadline_days", "Days to submit after a module opens", "int", "2", ""),
        ("reminder_hours_before", "Send reminder this many hours before deadline", "int", "24", ""),
        ("allow_registration", "Students can sign up themselves", "bool", "true", "If off, only the admin can add students."),
    ]),
    ("AI Reviewer", [
        ("ai_provider", "AI provider", "select:claude,openai,gemini,groq,openrouter,deepseek,custom", "claude",
         "Pick the company whose API key you have. 'custom' = any OpenAI-compatible server (e.g. Ollama)."),
        ("ai_api_key", "API key", "password", "", "The key from your provider's dashboard. Not needed for a local Ollama server."),
        ("ai_model", "Model", "text", "", "Leave empty to use the provider's default model (shown below the field)."),
        ("ai_base_url", "Custom base URL", "text", "", "Only for 'custom', e.g. http://localhost:11434/v1 for Ollama."),
        ("review_effort", "Review depth (Claude only)", "select:low,medium,high", "medium",
         "Higher = more thorough, slower and costlier."),
        ("review_mode", "Feedback delivery", "select:auto,approve", "auto",
         "auto: email feedback right away. approve: a trainer checks/edits it on the dashboard first."),
        ("send_delay_min", "Send feedback after at least (minutes)", "int", "120",
         "A real reviewer doesn't reply in seconds. Feedback is held for a random time in this range (auto mode)."),
        ("send_delay_max", "...and at most (minutes)", "int", "360", "Set both to 0 to send immediately."),
        ("reviewer_signature", "Email signature", "textarea", "Review Team",
         "How feedback emails are signed, e.g. a trainer's name and role on two lines."),
        ("ai_disclosure", "Mention AI assistance in feedback emails", "bool", "false",
         "Adds one line saying the feedback was prepared with AI assistance."),
    ]),
    ("Email (SMTP)", [
        ("smtp_host", "SMTP server", "text", "", "Gmail: smtp.gmail.com. Leave empty to save emails to the 'outbox' folder instead."),
        ("smtp_port", "SMTP port", "int", "587", "587 = STARTTLS, 465 = SSL."),
        ("smtp_user", "SMTP username", "text", "", "Usually your email address."),
        ("smtp_password", "SMTP password", "password", "", "For Gmail use a 16-character App Password, not your normal password."),
        ("smtp_from", "From address", "text", "", "Defaults to the SMTP username."),
    ]),
]

FIELDS = {f[0]: {"label": f[1], "type": f[2], "default": f[3], "help": f[4]} for _, fs in GROUPS for f in fs}


def get(key):
    row = db.session.get(Setting, key)
    return row.value if row is not None else FIELDS[key]["default"]


def get_int(key):
    try:
        return int(get(key))
    except (TypeError, ValueError):
        return int(FIELDS[key]["default"])


def get_bool(key):
    return str(get(key)).lower() in ("1", "true", "yes", "on")


def set_value(key, value):
    row = db.session.get(Setting, key)
    if row is None:
        db.session.add(Setting(key=key, value=value))
    else:
        row.value = value


def save_form(form):
    """Saves the Settings page. Returns a list of error messages (empty on success)."""
    errors = []
    for key, field in FIELDS.items():
        kind = field["type"]
        if kind == "bool":
            set_value(key, "true" if form.get(key) else "false")
            continue
        value = (form.get(key) or "").strip()
        if kind == "password" and not value:
            continue  # blank means keep the saved secret
        if kind == "int":
            if not value.isdigit():
                errors.append(f"{field['label']} must be a whole number.")
                continue
        if kind.startswith("select:") and value not in kind[7:].split(","):
            errors.append(f"{field['label']}: invalid choice.")
            continue
        if key == "base_url":
            value = value.rstrip("/")
        set_value(key, value)
    if errors:
        db.session.rollback()
    else:
        db.session.commit()
    return errors


def clear_secret(key):
    if FIELDS[key]["type"] == "password":
        set_value(key, "")
        db.session.commit()
