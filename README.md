# Review Hub: AI Review System for Internships

Students upload their work after each class. An AI reviewer reads it, finds mistakes, suggests improvements, and emails the student feedback written in a natural mentor's voice. At the end of the internship, students who passed every module get a PDF certificate by email.

## Run it

Double-click **`run.bat`**. The first run installs everything automatically.

Or from a terminal:

```
python -m venv venv
venv\Scripts\pip install -r requirements.txt
venv\Scripts\python app.py
```

Then open http://127.0.0.1:5000. The first time, it asks you to create the admin account.

## Configure (inside the app)

There is no `.env` file. Everything is set in **Admin → Settings**:

| Section | What you set |
|---|---|
| General | Program name, organization, app URL (used in email links) |
| Internship rules | Internship length (30 days), submission window (2 days), reminder timing, self sign-up on/off |
| AI Reviewer | Provider (**Claude, OpenAI, Gemini, Groq, OpenRouter, DeepSeek, or any OpenAI-compatible server such as Ollama**), API key, model, review depth, auto-send or approve-first, send delay, email signature |
| Email (SMTP) | SMTP server, port, username, password, from address. Leave the server empty to save emails to the `outbox` folder while testing. |

Use **Save & test AI** and **Save & send test email** to check that the settings work.

**Gmail:** turn on 2-Step Verification, create an App Password (Google Account → Security → App passwords), then use `smtp.gmail.com`, port `587`, your Gmail address, and the 16-character app password.

## How it works

1. **Admin creates modules**: the task for students, plus a private rubric telling the reviewer what to check.
2. **After the class**, the admin clicks **Open module**. Every active student gets the task by email, and the 2-day deadline starts.
3. **Students upload** a code file, PDF, screenshots, or a `.zip` of the whole project, with an optional note.
4. **The AI reviewer** reads every file and returns a score, a pass/needs-revision verdict, the mistakes (where each one is and how to fix it), strengths, improvements, and the feedback email.
5. **Feedback email**
   - *auto mode*: sent after a random delay (default 2–6 hours from submission), like a real reviewer.
   - *approve mode*: waits on the dashboard so a trainer can edit and send it.
   - Every email goes through a clean-up step that removes AI-sounding phrases and formatting.
6. **Automatic emails** (checked every 2 minutes): deadline reminders, missed-deadline notices, and scheduled feedback.
7. **Certificate**: when a student's 30 days are over and they have passed every opened module, a PDF certificate is generated and emailed automatically. The admin can also issue one manually. Anyone can verify it at `/verify/<certificate-id>`.

## Project files

| File | Purpose |
|---|---|
| `app.py` | Web app: pages and routes |
| `ai_reviewer.py` | AI reviewer bot: multi-provider calls, review prompt, humanized email clean-up |
| `services.py` | Workflow: review queue, feedback sending, reminders, certificates |
| `emailer.py` | SMTP email sending (or outbox files) |
| `certificates.py` | PDF certificate generator |
| `file_extract.py` | Reads uploaded files and zips for the reviewer |
| `settings.py` | In-app settings (stored in the database) |
| `models.py` | Database tables |
| `templates/`, `static/` | Frontend |

Data is stored in `instance/review_system.db`, uploaded files in `uploads/`, and certificates in `certificates/`.
