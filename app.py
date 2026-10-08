import json
import os
import secrets
from datetime import datetime
from functools import wraps
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from flask import (Flask, abort, flash, redirect, render_template, request, send_file, session,
                   url_for)
from flask_login import LoginManager, current_user, login_required, login_user, logout_user
from werkzeug.utils import secure_filename

import ai_reviewer
import services
import settings
from config import Config
from emailer import send_email
from file_extract import ALLOWED_UPLOADS
from icons import icon
from models import EmailLog, Module, Submission, User, db

IN_PROGRESS = ("pending", "reviewing", "scheduled", "awaiting_approval")

app = Flask(__name__)
app.config.from_object(Config)
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"connect_args": {"timeout": 30}}
db.init_app(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message_category = "info"


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# ---------- helpers ----------

def admin_required(view):
    @wraps(view)
    @login_required
    def wrapper(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)
    return wrapper


def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    return session["_csrf"]


@app.before_request
def guard():
    if request.method == "POST" and request.form.get("_csrf") != session.get("_csrf"):
        abort(400, "Form expired. Go back, refresh the page and try again.")
    if request.endpoint not in ("setup", "static") and not User.query.filter_by(role="admin").first():
        return redirect(url_for("setup"))


@app.context_processor
def inject():
    return {"csrf_token": csrf_token, "S": settings, "now": datetime.now()}


@app.template_filter("dt")
def fmt_dt(value, fmt="%d %b %Y, %I:%M %p"):
    return value.strftime(fmt) if value else "-"


@app.template_filter("ago")
def time_ago(value):
    if not value:
        return "-"
    secs = (datetime.now() - value).total_seconds()
    future = secs < 0
    secs = abs(secs)
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if secs >= size:
            n = int(secs // size)
            text = f"{n} {unit}{'s' if n != 1 else ''}"
            return f"in {text}" if future else f"{text} ago"
    return "just now"


@app.template_filter("fromjson")
def from_json(value):
    return json.loads(value) if value else {}


def student_status(sub):
    """What a student sees. Feedback stays hidden until the email has gone out."""
    if sub is None:
        return ("Not submitted", "muted")
    if sub.status == "sent":
        return ("Passed", "success") if sub.verdict == "pass" else ("Needs revision", "warning")
    return ("Under review", "info")


def admin_status(sub):
    return {
        "pending": ("Queued", "muted"), "reviewing": ("AI reviewing", "info"),
        "scheduled": ("Scheduled", "brand"), "awaiting_approval": ("Needs approval", "warning"),
        "sent": ("Sent", "success"), "error": ("Error", "danger"),
    }.get(sub.status, (sub.status, "muted"))


def score_class(score):
    return "" if score is None else "hi" if score >= 75 else "mid" if score >= 60 else "lo"


def initials(name):
    parts = (name or "?").split()
    return (parts[0][0] + (parts[-1][0] if len(parts) > 1 else "")).upper()


app.jinja_env.globals.update(student_status=student_status, admin_status=admin_status, score_class=score_class,
                             initials=initials, icon=icon)


# ---------- setup & auth ----------

@app.route("/setup", methods=["GET", "POST"])
def setup():
    if User.query.filter_by(role="admin").first():
        return redirect(url_for("login"))
    if request.method == "POST":
        f = request.form
        if not f.get("name") or not f.get("email") or len(f.get("password", "")) < 6:
            flash("Fill in all fields. Password needs at least 6 characters.", "error")
        else:
            admin = User(name=f["name"].strip(), email=f["email"].strip().lower(), role="admin")
            admin.set_password(f["password"])
            db.session.add(admin)
            settings.set_value("program_name", f.get("program_name", "").strip() or settings.get("program_name"))
            settings.set_value("organization_name", f.get("organization_name", "").strip()
                               or settings.get("organization_name"))
            settings.set_value("base_url", request.host_url.rstrip("/"))
            db.session.commit()
            login_user(admin)
            flash("Admin account created. Now add your AI key and email settings.", "success")
            return redirect(url_for("admin_settings"))
    return render_template("setup.html")


@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("admin_overview" if current_user.is_admin else "dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        user = User.query.filter_by(email=request.form.get("email", "").strip().lower()).first()
        if user and user.check_password(request.form.get("password", "")):
            login_user(user, remember=True)
            return redirect(request.args.get("next") if (request.args.get("next") or "").startswith("/")
                            else url_for("index"))
        flash("Wrong email or password.", "error")
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if not settings.get_bool("allow_registration"):
        flash("Sign-up is closed. Ask your program admin to add you.", "info")
        return redirect(url_for("login"))
    if request.method == "POST":
        f = request.form
        email = f.get("email", "").strip().lower()
        if not f.get("name") or not email or len(f.get("password", "")) < 6:
            flash("Fill in all fields. Password needs at least 6 characters.", "error")
        elif User.query.filter_by(email=email).first():
            flash("An account with this email already exists.", "error")
        else:
            user = _create_student(f["name"].strip(), email, f["password"])
            login_user(user, remember=True)
            return redirect(url_for("dashboard"))
    return render_template("register.html")


def _create_student(name, email, password):
    user = User(name=name, email=email, role="student")
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    send_email(user.email, f"Welcome to the {settings.get('program_name')}", services.signed(
        f"Hi {services.first_name(user)},\n\n"
        f"Welcome aboard. Your internship runs from {user.enrolled_at:%d %B} to {user.internship_end:%d %B %Y}.\n\n"
        f"After each class you'll get the module task by email, and you'll have "
        f"{settings.get_int('deadline_days')} days to upload your work. I'll go through every submission and "
        "send you feedback on what to fix and improve. Finish all modules and you'll get your certificate.\n\n"
        f"Your dashboard: {settings.get('base_url')}/dashboard"))
    return user


@app.route("/logout", methods=["POST"])
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/verify", methods=["GET", "POST"])
@app.route("/verify/<cert_id>")
def verify(cert_id=None):
    cert_id = (cert_id or request.values.get("id", "")).strip().upper()
    user = User.query.filter_by(certificate_id=cert_id).first() if cert_id else None
    return render_template("verify.html", cert_id=cert_id, holder=user)


# ---------- student ----------

@app.route("/dashboard")
@login_required
def dashboard():
    if current_user.is_admin:
        return redirect(url_for("admin_overview"))
    modules = Module.query.order_by(Module.position).all()
    rows = [(m, current_user.latest_submission(m.id)) for m in modules]
    opened = [m for m in modules if m.is_open]
    passed = len(current_user.passed_module_ids() & {m.id for m in opened})
    total_days = settings.get_int("internship_days")
    day = min(total_days, max(1, (datetime.now() - current_user.enrolled_at).days + 1))
    scores = [s.score for s in current_user.submissions if s.status == "sent" and s.score is not None]
    return render_template("student/dashboard.html", rows=rows, opened=opened, passed=passed, day=day,
                           total_days=total_days, avg=round(sum(scores) / len(scores)) if scores else None)


@app.route("/modules/<int:module_id>", methods=["GET", "POST"])
@login_required
def module_page(module_id):
    module = db.get_or_404(Module, module_id)
    if not module.is_open and not current_user.is_admin:
        flash("This module hasn't opened yet.", "info")
        return redirect(url_for("dashboard"))
    history = sorted((s for s in current_user.submissions if s.module_id == module.id),
                     key=lambda s: s.submitted_at, reverse=True)
    latest = history[0] if history else None
    locked = latest is not None and latest.status in IN_PROGRESS

    if request.method == "POST":
        file = request.files.get("file")
        if locked:
            flash("Your last submission is still being reviewed. You can resubmit after you get feedback.", "info")
        elif not file or not file.filename:
            flash("Choose a file to upload.", "error")
        elif Path(file.filename.lower()).suffix not in ALLOWED_UPLOADS:
            flash("That file type isn't supported. Upload code files, a PDF, images, or a .zip of your project.",
                  "error")
        else:
            folder = app.config["UPLOAD_DIR"] / str(current_user.id)
            folder.mkdir(parents=True, exist_ok=True)
            name = secure_filename(file.filename) or "submission"
            path = folder / f"{datetime.now():%Y%m%d%H%M%S}_{name}"
            file.save(path)
            sub = Submission(user_id=current_user.id, module_id=module.id, attempt=len(history) + 1,
                             original_filename=file.filename, stored_path=str(path),
                             student_note=request.form.get("note", "").strip(),
                             is_late=datetime.now() > module.deadline)
            db.session.add(sub)
            db.session.commit()
            services.queue_review(sub.id)
            flash("Submitted! Your work is with the review team. You'll get feedback by email.", "success")
            return redirect(url_for("module_page", module_id=module.id))
    return render_template("student/module.html", module=module, history=history, latest=latest, locked=locked,
                           accept=",".join(sorted(ALLOWED_UPLOADS)))


@app.route("/feedback/<int:sub_id>")
@login_required
def feedback(sub_id):
    sub = db.get_or_404(Submission, sub_id)
    if sub.user_id != current_user.id and not current_user.is_admin:
        abort(404)
    if sub.status != "sent" and not current_user.is_admin:
        flash("Feedback for this submission isn't ready yet.", "info")
        return redirect(url_for("module_page", module_id=sub.module_id))
    return render_template("student/feedback.html", sub=sub, body=services.signed(sub.email_body or ""))


@app.route("/certificate")
@login_required
def my_certificate():
    if not current_user.certificate_id:
        abort(404)
    import certificates
    path = certificates.certificate_path(current_user)
    if not path.exists():
        certificates.generate_pdf(current_user)
    return send_file(path, as_attachment=True, download_name=f"Certificate_{current_user.name}.pdf")


# ---------- admin ----------

@app.route("/admin")
@admin_required
def admin_overview():
    students = User.query.filter_by(role="student").all()
    subs = Submission.query.order_by(Submission.submitted_at.desc())
    scored = [s.score for s in Submission.query.filter(Submission.score.isnot(None)).all()]
    stats = {
        "students": len(students),
        "active": len(services.active_students()),
        "submissions": subs.count(),
        "pending": Submission.query.filter(Submission.status.in_(IN_PROGRESS)).count(),
        "errors": Submission.query.filter_by(status="error").count(),
        "approval": Submission.query.filter_by(status="awaiting_approval").count(),
        "certificates": sum(1 for s in students if s.certificate_id),
        "avg": round(sum(scored) / len(scored)) if scored else None,
    }
    ai_ready = bool(settings.get("ai_api_key")) or settings.get("ai_provider") == "custom"
    return render_template("admin/overview.html", stats=stats, recent=subs.limit(8).all(),
                           modules=Module.query.order_by(Module.position).all(), ai_ready=ai_ready,
                           smtp_ready=bool(settings.get("smtp_host")))


@app.route("/admin/modules")
@admin_required
def admin_modules():
    return render_template("admin/modules.html", modules=Module.query.order_by(Module.position).all())


@app.route("/admin/modules/new", methods=["GET", "POST"])
@app.route("/admin/modules/<int:module_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_module_form(module_id=None):
    module = db.get_or_404(Module, module_id) if module_id else None
    if request.method == "POST":
        f = request.form
        if not f.get("title", "").strip() or not f.get("instructions", "").strip():
            flash("Title and task instructions are required.", "error")
        else:
            if module is None:
                module = Module()
                db.session.add(module)
            module.title = f["title"].strip()
            module.position = int(f.get("position") or 1)
            module.instructions = f["instructions"].strip()
            module.rubric = f.get("rubric", "").strip()
            db.session.commit()
            flash("Module saved.", "success")
            return redirect(url_for("admin_modules"))
    next_pos = (db.session.query(db.func.max(Module.position)).scalar() or 0) + 1
    return render_template("admin/module_form.html", module=module, next_pos=next_pos)


@app.route("/admin/modules/<int:module_id>/open", methods=["POST"])
@admin_required
def admin_module_open(module_id):
    module = db.get_or_404(Module, module_id)
    services.open_module(module)
    flash(f"Module {module.position} is open. Students were emailed the task; deadline "
          f"{module.deadline:%d %b, %I:%M %p}.", "success")
    return redirect(url_for("admin_modules"))


@app.route("/admin/modules/<int:module_id>/delete", methods=["POST"])
@admin_required
def admin_module_delete(module_id):
    module = db.get_or_404(Module, module_id)
    db.session.delete(module)
    db.session.commit()
    flash("Module deleted.", "success")
    return redirect(url_for("admin_modules"))


@app.route("/admin/students", methods=["GET", "POST"])
@admin_required
def admin_students():
    if request.method == "POST":
        f = request.form
        email = f.get("email", "").strip().lower()
        if not f.get("name") or not email or len(f.get("password", "")) < 6:
            flash("Name, email and a password of 6+ characters are required.", "error")
        elif User.query.filter_by(email=email).first():
            flash("A user with this email already exists.", "error")
        else:
            _create_student(f["name"].strip(), email, f["password"])
            flash("Student added and welcome email sent.", "success")
        return redirect(url_for("admin_students"))
    students = User.query.filter_by(role="student").order_by(User.enrolled_at.desc()).all()
    opened = {m.id for m in Module.query.filter(Module.opened_at.isnot(None)).all()}
    return render_template("admin/students.html", students=students, opened=opened)


@app.route("/admin/students/<int:user_id>")
@admin_required
def admin_student(user_id):
    student = db.get_or_404(User, user_id)
    modules = Module.query.order_by(Module.position).all()
    rows = [(m, student.latest_submission(m.id)) for m in modules]
    subs = sorted(student.submissions, key=lambda s: s.submitted_at, reverse=True)
    return render_template("admin/student.html", student=student, rows=rows, subs=subs,
                           eligible=services.is_eligible(student))


@app.route("/admin/students/<int:user_id>/certificate", methods=["POST"])
@admin_required
def admin_issue_certificate(user_id):
    student = db.get_or_404(User, user_id)
    services.issue_certificate(student)
    flash(f"Certificate {student.certificate_id} issued and emailed to {student.name}.", "success")
    return redirect(url_for("admin_student", user_id=user_id))


@app.route("/admin/students/<int:user_id>/certificate.pdf")
@admin_required
def admin_download_certificate(user_id):
    import certificates
    student = db.get_or_404(User, user_id)
    if not student.certificate_id:
        abort(404)
    return send_file(certificates.generate_pdf(student), download_name=f"{student.certificate_id}.pdf")


@app.route("/admin/students/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_student_delete(user_id):
    student = db.get_or_404(User, user_id)
    if student.is_admin:
        abort(400)
    db.session.delete(student)
    db.session.commit()
    flash("Student removed.", "success")
    return redirect(url_for("admin_students"))


@app.route("/admin/submissions")
@admin_required
def admin_submissions():
    status = request.args.get("status", "")
    q = Submission.query.order_by(Submission.submitted_at.desc())
    if status:
        q = q.filter(Submission.status.in_(IN_PROGRESS) if status == "in_progress" else Submission.status == status)
    return render_template("admin/submissions.html", subs=q.all(), status=status)


@app.route("/admin/submissions/<int:sub_id>", methods=["GET", "POST"])
@admin_required
def admin_submission(sub_id):
    sub = db.get_or_404(Submission, sub_id)
    if request.method == "POST":
        action = request.form.get("action")
        if action in ("save", "send") and sub.feedback_json:
            sub.email_subject = request.form.get("email_subject", sub.email_subject).strip()
            sub.email_body = request.form.get("email_body", sub.email_body).strip()
            if request.form.get("verdict") in ("pass", "needs_revision"):
                sub.verdict = request.form["verdict"]
            if (request.form.get("score") or "").isdigit():
                sub.score = max(0, min(100, int(request.form["score"])))
            db.session.commit()
            if action == "send":
                if services.send_feedback(sub):
                    flash("Feedback emailed to the student.", "success")
                else:
                    flash("Email failed. Check the Email settings and the Email log.", "error")
            else:
                flash("Changes saved.", "success")
        elif action == "retry":
            sub.status, sub.error = "pending", None
            db.session.commit()
            services.queue_review(sub.id)
            flash("Review restarted. Refresh in a minute.", "success")
        return redirect(url_for("admin_submission", sub_id=sub.id))
    return render_template("admin/submission.html", sub=sub)


@app.route("/admin/submissions/<int:sub_id>/file")
@admin_required
def admin_submission_file(sub_id):
    sub = db.get_or_404(Submission, sub_id)
    return send_file(sub.stored_path, as_attachment=True, download_name=sub.original_filename)


@app.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings():
    if request.method == "POST":
        action = request.form.get("action", "save")
        if action.startswith("clear:"):
            settings.clear_secret(action[6:])
            flash("Saved secret removed.", "success")
            return redirect(url_for("admin_settings"))
        errors = settings.save_form(request.form)  # tests use what's on the form, so save first
        for e in errors:
            flash(e, "error")
        if not errors and action == "save":
            flash("Settings saved.", "success")
        elif not errors and action == "test_ai":
            try:
                flash(ai_reviewer.test_connection(), "success")
            except ai_reviewer.ReviewError as e:
                flash(f"AI test failed: {e}", "error")
        elif not errors and action == "test_email":
            to = request.form.get("test_to") or current_user.email
            try:
                send_email(to, "Test email from the Review System",
                           "This is a test. If you can read it, email settings are working.", raise_errors=True)
                flash(f"Test email sent to {to}." if settings.get("smtp_host")
                      else "No SMTP server set, so the test email was saved to the outbox folder.", "success")
            except Exception as e:
                flash(f"Email test failed: {e}", "error")
        return redirect(url_for("admin_settings"))
    return render_template("admin/settings.html", groups=settings.GROUPS, providers=ai_reviewer.PROVIDERS)


@app.route("/admin/emails")
@admin_required
def admin_emails():
    return render_template("admin/emails.html", logs=EmailLog.query.order_by(EmailLog.id.desc()).limit(200).all())


@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(400)
def error_page(e):
    return render_template("error.html", error=e), e.code


@app.errorhandler(413)
def too_large(e):
    flash("That file is too large. The limit is 20 MB.", "error")
    return redirect(request.url)


# ---------- startup ----------

def start():
    with app.app_context():
        db.create_all()
        services.requeue_unfinished()
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(services.run_periodic_jobs, "interval", minutes=2, args=[app],
                      max_instances=1, coalesce=True, next_run_time=datetime.now())
    scheduler.start()


if __name__ == "__main__":
    start()
    port = int(os.environ.get("PORT", 5000))
    print(f"\n  Review System running at http://127.0.0.1:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
