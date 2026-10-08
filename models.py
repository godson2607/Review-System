from datetime import datetime, timedelta

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(200), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), default="student")  # student | admin
    enrolled_at = db.Column(db.DateTime, default=datetime.now)
    certificate_id = db.Column(db.String(40), unique=True)
    certificate_issued_at = db.Column(db.DateTime)

    submissions = db.relationship("Submission", back_populates="user", cascade="all, delete-orphan")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_admin(self):
        return self.role == "admin"

    @property
    def internship_end(self):
        import settings
        return self.enrolled_at + timedelta(days=settings.get_int("internship_days"))

    def latest_submission(self, module_id):
        subs = [s for s in self.submissions if s.module_id == module_id]
        return max(subs, key=lambda s: s.submitted_at) if subs else None

    def passed_module_ids(self):
        return {s.module_id for s in self.submissions if s.verdict == "pass"}


class Module(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    position = db.Column(db.Integer, default=1)
    title = db.Column(db.String(200), nullable=False)
    instructions = db.Column(db.Text, nullable=False)  # the task given to students
    rubric = db.Column(db.Text, default="")           # what the reviewer should check
    opened_at = db.Column(db.DateTime)                # set when the class is completed / module opened

    submissions = db.relationship("Submission", back_populates="module", cascade="all, delete-orphan")

    @property
    def is_open(self):
        return self.opened_at is not None

    @property
    def deadline(self):
        if not self.opened_at:
            return None
        import settings
        return self.opened_at + timedelta(days=settings.get_int("deadline_days"))


class Submission(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    module_id = db.Column(db.Integer, db.ForeignKey("module.id"), nullable=False)
    attempt = db.Column(db.Integer, default=1)
    original_filename = db.Column(db.String(255))
    stored_path = db.Column(db.String(500))
    student_note = db.Column(db.Text, default="")
    submitted_at = db.Column(db.DateTime, default=datetime.now)
    is_late = db.Column(db.Boolean, default=False)

    # pending -> reviewing -> scheduled (auto mode) | awaiting_approval (approve mode) -> sent ; error on failure
    status = db.Column(db.String(30), default="pending")
    score = db.Column(db.Integer)
    verdict = db.Column(db.String(30))  # pass | needs_revision
    feedback_json = db.Column(db.Text)
    email_subject = db.Column(db.String(300))
    email_body = db.Column(db.Text)
    error = db.Column(db.Text)
    reviewed_at = db.Column(db.DateTime)
    send_after = db.Column(db.DateTime)  # feedback email goes out at this time (status "scheduled")
    sent_at = db.Column(db.DateTime)

    user = db.relationship("User", back_populates="submissions")
    module = db.relationship("Module", back_populates="submissions")


class Setting(db.Model):
    key = db.Column(db.String(60), primary_key=True)
    value = db.Column(db.Text, default="")


class Notification(db.Model):
    """Remembers which automatic emails were already sent, so the scheduler never sends twice."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    module_id = db.Column(db.Integer, db.ForeignKey("module.id"))
    kind = db.Column(db.String(40), nullable=False)  # module_open | reminder | missed
    created_at = db.Column(db.DateTime, default=datetime.now)
    __table_args__ = (db.UniqueConstraint("user_id", "module_id", "kind"),)


class EmailLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    to_email = db.Column(db.String(200))
    subject = db.Column(db.String(300))
    status = db.Column(db.String(20))  # sent | saved | failed
    error = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.now)
