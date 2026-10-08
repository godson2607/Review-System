"""Business workflow: reviewing submissions, sending feedback, reminders and certificates."""
import json
import random
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from flask import current_app
from sqlalchemy.exc import IntegrityError

import ai_reviewer
import certificates
import settings
from emailer import send_email
from file_extract import UnreadableSubmission
from models import Module, Notification, Submission, User, db

_executor = ThreadPoolExecutor(max_workers=3)


def first_name(user):
    return user.name.split()[0] if user.name else "there"


def signed(text):
    body = text.rstrip() + "\n\n" + settings.get("reviewer_signature").strip()
    if settings.get_bool("ai_disclosure"):
        body += "\n\n(This feedback was prepared with the help of an AI review assistant.)"
    return body


def active_students():
    now = datetime.now()
    return [u for u in User.query.filter_by(role="student").all() if u.internship_end >= now]


# ---------- Reviews ----------

def queue_review(submission_id):
    app = current_app._get_current_object()
    _executor.submit(_run_review, app, submission_id)


def _run_review(app, submission_id):
    with app.app_context():
        try:
            review_submission(submission_id)
        except Exception as e:  # never let a background thread die silently
            app.logger.exception("Review %s crashed", submission_id)
            sub = db.session.get(Submission, submission_id)
            if sub:
                sub.status, sub.error = "error", f"Unexpected error: {e}"
                db.session.commit()


def review_submission(submission_id):
    sub = db.session.get(Submission, submission_id)
    if sub is None:
        return
    sub.status, sub.error = "reviewing", None
    db.session.commit()

    previous = (Submission.query
                .filter(Submission.user_id == sub.user_id, Submission.module_id == sub.module_id,
                        Submission.id != sub.id, Submission.feedback_json.isnot(None),
                        Submission.submitted_at < sub.submitted_at)
                .order_by(Submission.submitted_at.desc()).first())
    try:
        feedback, skipped = ai_reviewer.review(sub, previous)
    except (ai_reviewer.ReviewError, UnreadableSubmission) as e:
        sub.status, sub.error = "error", str(e)
        db.session.commit()
        return

    feedback["skipped_files"] = skipped
    sub.feedback_json = json.dumps(feedback)
    sub.score = feedback["score"]
    sub.verdict = feedback["verdict"]
    sub.email_subject = feedback["email_subject"]
    sub.email_body = feedback["email_body"]
    sub.reviewed_at = datetime.now()

    if settings.get("review_mode") == "approve":
        sub.status = "awaiting_approval"
    else:
        lo, hi = sorted((settings.get_int("send_delay_min"), settings.get_int("send_delay_max")))
        sub.status = "scheduled"
        # The delay counts from when the student submitted, like a reviewer picking it up later.
        sub.send_after = sub.submitted_at + timedelta(minutes=random.randint(lo, hi))
    db.session.commit()
    if sub.status == "scheduled" and sub.send_after <= datetime.now():
        send_feedback(sub)


def send_feedback(sub):
    ok = send_email(sub.user.email, sub.email_subject, signed(sub.email_body))
    if ok:
        sub.status, sub.sent_at, sub.error = "sent", datetime.now(), None
    else:
        sub.error = "Email could not be sent. Check Email settings and the Email log, then use Send now."
    db.session.commit()
    return ok


def send_due_feedback():
    due = Submission.query.filter(Submission.status == "scheduled", Submission.send_after <= datetime.now()).all()
    for sub in due:
        send_feedback(sub)


def requeue_unfinished():
    """On startup, restart reviews that were interrupted when the app stopped."""
    for sub in Submission.query.filter(Submission.status.in_(["pending", "reviewing"])).all():
        queue_review(sub.id)


# ---------- Modules, reminders, missed deadlines ----------

def _notify_once(user, module, kind):
    """Records that a notification was sent. Returns False if it was already sent before."""
    db.session.add(Notification(user_id=user.id, module_id=module.id if module else None, kind=kind))
    try:
        db.session.commit()
        return True
    except IntegrityError:
        db.session.rollback()
        return False


def open_module(module):
    module.opened_at = datetime.now()
    db.session.commit()
    deadline = module.deadline
    for user in active_students():
        if not _notify_once(user, module, "module_open"):
            continue
        send_email(user.email, f"Module {module.position} task: {module.title}", signed(
            f"Hi {first_name(user)},\n\n"
            f"Thanks for joining today's class. The task for Module {module.position} ({module.title}) is up now.\n\n"
            f"{module.instructions.strip()}\n\n"
            f"Please upload your work by {deadline:%A, %d %B at %I:%M %p}. You can submit here: "
            f"{settings.get('base_url')}/modules/{module.id}\n\n"
            "If you get stuck on something, put a short note with your submission and I'll take a look."))


def send_reminders():
    now = datetime.now()
    window = timedelta(hours=settings.get_int("reminder_hours_before"))
    for module in Module.query.filter(Module.opened_at.isnot(None)).all():
        deadline = module.deadline
        for user in active_students():
            if user.enrolled_at > deadline or user.latest_submission(module.id):
                continue
            if now <= deadline <= now + window and _notify_once(user, module, "reminder"):
                hours = max(1, int((deadline - now).total_seconds() // 3600))
                send_email(user.email, f"Reminder: Module {module.position} is due soon", signed(
                    f"Hi {first_name(user)},\n\n"
                    f"Just a heads up, I haven't seen your Module {module.position} ({module.title}) submission yet. "
                    f"It's due in about {hours} hour{'s' if hours != 1 else ''} "
                    f"({deadline:%A, %d %B at %I:%M %p}).\n\n"
                    f"Upload it here: {settings.get('base_url')}/modules/{module.id}"))
            elif now > deadline and _notify_once(user, module, "missed"):
                send_email(user.email, f"Module {module.position}: deadline passed", signed(
                    f"Hi {first_name(user)},\n\n"
                    f"The deadline for Module {module.position} ({module.title}) passed and I didn't get a "
                    "submission from you. You can still upload it, it'll be marked as late, but please get it in "
                    "soon so you don't fall behind on the next modules.\n\n"
                    f"{settings.get('base_url')}/modules/{module.id}"))


# ---------- Certificates ----------

def is_eligible(user):
    opened = [m.id for m in Module.query.filter(Module.opened_at.isnot(None)).all()]
    return bool(opened) and set(opened) <= user.passed_module_ids()


def issue_certificate(user):
    if not user.certificate_id:
        user.certificate_id = certificates.new_certificate_id()
        user.certificate_issued_at = datetime.now()
        db.session.commit()
    path = certificates.generate_pdf(user)
    send_email(user.email, f"Your certificate: {settings.get('program_name')}", signed(
        f"Hi {first_name(user)},\n\n"
        f"Congratulations, you've completed the {settings.get('program_name')}. "
        "Your certificate is attached to this email.\n\n"
        "It was good watching your work improve over the month. All the best for what comes next.\n\n"
        f"Anyone can verify the certificate at {settings.get('base_url')}/verify/{user.certificate_id}"),
        attachments=[(f"Certificate_{user.name.replace(' ', '_')}.pdf", path.read_bytes(), "application/pdf")])


def issue_due_certificates():
    now = datetime.now()
    for user in User.query.filter_by(role="student", certificate_id=None).all():
        if now >= user.internship_end and is_eligible(user):
            issue_certificate(user)


def run_periodic_jobs(app):
    with app.app_context():
        for job in (send_due_feedback, send_reminders, issue_due_certificates):
            try:
                job()
            except Exception:
                db.session.rollback()
                app.logger.exception("Scheduled job %s failed", job.__name__)
