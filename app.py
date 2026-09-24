import os
import re
import uuid
from datetime import datetime
from io import BytesIO

import bleach
import boto3
from botocore.exceptions import BotoCoreError, ClientError
from botocore.config import Config
from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, request, send_file, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import or_

load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "change-this-secret")
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///worklog.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024

db = SQLAlchemy(app)

ALLOWED_TAGS = set(bleach.sanitizer.ALLOWED_TAGS) | {
    "p", "br", "h1", "h2", "h3", "h4", "blockquote", "pre", "code",
    "ul", "ol", "li", "strong", "em", "u", "s", "a"
}
ALLOWED_ATTRIBUTES = {"a": ["href", "title", "target", "rel"]}


class Task(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, default="")
    status = db.Column(db.String(20), default="todo", nullable=False)
    priority = db.Column(db.String(20), default="medium", nullable=False)
    due_date = db.Column(db.Date, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    notes = db.relationship("Note", backref="task", cascade="all, delete-orphan", order_by="Note.updated_at.desc()")
    attachments = db.relationship("Attachment", backref="task", cascade="all, delete-orphan", order_by="Attachment.created_at.desc()")


class Note(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    content = db.Column(db.Text, default="")
    task_id = db.Column(db.Integer, db.ForeignKey("task.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    attachments = db.relationship("Attachment", backref="note", cascade="all, delete-orphan", order_by="Attachment.created_at.desc()")


class Attachment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    original_name = db.Column(db.String(255), nullable=False)
    object_key = db.Column(db.String(500), nullable=False, unique=True)
    content_type = db.Column(db.String(120), default="application/octet-stream")
    size = db.Column(db.Integer, default=0)
    task_id = db.Column(db.Integer, db.ForeignKey("task.id"), nullable=True)
    note_id = db.Column(db.Integer, db.ForeignKey("note.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class ObjectStorage:
    def __init__(self):
        self.bucket = os.getenv("OSS_BUCKET")
        self.endpoint = os.getenv("OSS_ENDPOINT")
        self.client = None
        if self.bucket and self.endpoint and os.getenv("OSS_ACCESS_KEY_ID") and os.getenv("OSS_SECRET_ACCESS_KEY"):
            self.client = boto3.client(
                "s3",
                endpoint_url=self.endpoint,
                aws_access_key_id=os.getenv("OSS_ACCESS_KEY_ID"),
                aws_secret_access_key=os.getenv("OSS_SECRET_ACCESS_KEY"),
                region_name=os.getenv("OSS_REGION", "oss-cn-hangzhou"),
                config=Config(
                    signature_version="s3v4",
                    s3={"addressing_style": "virtual", "payload_signing_enabled": False},
                    request_checksum_calculation="when_required",
                    response_checksum_validation="when_required",
                ),
            )

    @property
    def enabled(self):
        return self.client is not None

    def upload(self, file_storage, key):
        if not self.enabled:
            raise RuntimeError("OSS 尚未配置")
        data = file_storage.read()
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentLength=len(data), ContentType=file_storage.content_type or "application/octet-stream")
        return len(data)

    def download(self, key):
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        return response["Body"].read()

    def delete(self, key):
        if self.enabled:
            self.client.delete_object(Bucket=self.bucket, Key=key)

    def upload_bytes(self, data, key, content_type):
        if self.enabled:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentLength=len(data), ContentType=content_type)

    def download_optional(self, key):
        if not self.enabled:
            return None
        try:
            return self.download(key)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in {"NoSuchKey", "NoSuchBucket", "404"}:
                return None
            raise


storage = ObjectStorage()


def database_path():
    return os.path.join(app.instance_path, "worklog.db")


def restore_database():
    if not storage.enabled:
        return
    data = storage.download_optional(os.getenv("OSS_DATABASE_KEY", "worklog/worklog.db"))
    if data:
        os.makedirs(app.instance_path, exist_ok=True)
        with open(database_path(), "wb") as database_file:
            database_file.write(data)


def sync_database():
    if storage.enabled and os.path.exists(database_path()):
        with open(database_path(), "rb") as database_file:
            storage.upload_bytes(database_file.read(), os.getenv("OSS_DATABASE_KEY", "worklog/worklog.db"), "application/x-sqlite3")


def sanitize_html(value):
    return bleach.clean(value or "", tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRIBUTES, protocols=["http", "https", "mailto"], strip=True)


def parse_date(value):
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


@app.context_processor
def inject_counts():
    return {"pending_count": Task.query.filter(Task.status != "done").count() if db.engine else 0, "oss_enabled": storage.enabled, "now": datetime.utcnow()}


@app.route("/")
def index():
    status = request.args.get("status", "")
    query = request.args.get("q", "").strip()
    task_query = Task.query
    if status:
        task_query = task_query.filter_by(status=status)
    if query:
        task_query = task_query.filter(or_(Task.title.ilike(f"%{query}%"), Task.description.ilike(f"%{query}%")))
    tasks = task_query.order_by(Task.created_at.desc()).all()
    notes = Note.query.order_by(Note.updated_at.desc()).limit(8).all()
    return render_template("index.html", tasks=tasks, notes=notes, current_status=status, query=query)


@app.route("/tasks/new", methods=["GET", "POST"])
def new_task():
    if request.method == "POST":
        task = Task(title=request.form["title"].strip(), description=sanitize_html(request.form.get("description")), status=request.form.get("status", "todo"), priority=request.form.get("priority", "medium"), due_date=parse_date(request.form.get("due_date")))
        db.session.add(task)
        db.session.commit()
        sync_database()
        flash("任务已创建", "success")
        return redirect(url_for("task_detail", task_id=task.id))
    return render_template("task_form.html", task=None)


@app.route("/tasks/<int:task_id>")
def task_detail(task_id):
    task = db.get_or_404(Task, task_id)
    return render_template("task_detail.html", task=task)


@app.route("/tasks/<int:task_id>/edit", methods=["GET", "POST"])
def edit_task(task_id):
    task = db.get_or_404(Task, task_id)
    if request.method == "POST":
        task.title = request.form["title"].strip()
        task.description = sanitize_html(request.form.get("description"))
        task.status = request.form.get("status", "todo")
        task.priority = request.form.get("priority", "medium")
        task.due_date = parse_date(request.form.get("due_date"))
        db.session.commit()
        sync_database()
        flash("任务已更新", "success")
        return redirect(url_for("task_detail", task_id=task.id))
    return render_template("task_form.html", task=task)


@app.post("/tasks/<int:task_id>/status")
def update_task_status(task_id):
    task = db.get_or_404(Task, task_id)
    task.status = request.form["status"]
    db.session.commit()
    sync_database()
    return redirect(request.referrer or url_for("index"))


@app.post("/tasks/<int:task_id>/delete")
def delete_task(task_id):
    task = db.get_or_404(Task, task_id)
    for attachment in task.attachments:
        storage.delete(attachment.object_key)
    db.session.delete(task)
    db.session.commit()
    sync_database()
    flash("任务已删除", "success")
    return redirect(url_for("index"))


@app.route("/notes/new", methods=["GET", "POST"])
def new_note():
    if request.method == "POST":
        note = Note(title=request.form["title"].strip(), content=sanitize_html(request.form.get("content")), task_id=request.form.get("task_id", type=int) or None)
        db.session.add(note)
        db.session.commit()
        sync_database()
        flash("笔记已保存", "success")
        return redirect(url_for("note_detail", note_id=note.id))
    return render_template("note_form.html", note=None, tasks=Task.query.order_by(Task.title).all(), selected_task_id=request.args.get("task_id", type=int))


@app.route("/notes/<int:note_id>")
def note_detail(note_id):
    note = db.get_or_404(Note, note_id)
    return render_template("note_detail.html", note=note)


@app.route("/notes/<int:note_id>/edit", methods=["GET", "POST"])
def edit_note(note_id):
    note = db.get_or_404(Note, note_id)
    if request.method == "POST":
        note.title = request.form["title"].strip()
        note.content = sanitize_html(request.form.get("content"))
        note.task_id = request.form.get("task_id", type=int) or None
        db.session.commit()
        sync_database()
        flash("笔记已更新", "success")
        return redirect(url_for("note_detail", note_id=note.id))
    return render_template("note_form.html", note=note, tasks=Task.query.order_by(Task.title).all(), selected_task_id=note.task_id)


@app.post("/attachments/upload")
def upload_attachment():
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        flash("请选择附件", "error")
        return redirect(request.referrer or url_for("index"))
    if not storage.enabled:
        flash("附件上传需要先配置阿里云 OSS 环境变量", "error")
        return redirect(request.referrer or url_for("index"))
    task_id = request.form.get("task_id", type=int)
    note_id = request.form.get("note_id", type=int)
    safe_name = re.sub(r"[^\w.\- ]", "_", uploaded.filename)[:180]
    key = f"attachments/{uuid.uuid4().hex}-{safe_name}"
    try:
        uploaded.stream.seek(0)
        uploaded_size = storage.upload(uploaded, key)
        attachment = Attachment(original_name=uploaded.filename, object_key=key, content_type=uploaded.content_type or "application/octet-stream", size=uploaded_size, task_id=task_id, note_id=note_id)
        db.session.add(attachment)
        db.session.commit()
        sync_database()
        flash("附件已上传", "success")
    except (BotoCoreError, ClientError, RuntimeError) as error:
        db.session.rollback()
        flash(f"附件上传失败：{error}", "error")
    return redirect(request.referrer or url_for("index"))


@app.get("/attachments/<int:attachment_id>")
def download_attachment(attachment_id):
    attachment = db.get_or_404(Attachment, attachment_id)
    try:
        data = storage.download(attachment.object_key)
    except (BotoCoreError, ClientError) as error:
        flash(f"附件读取失败：{error}", "error")
        return redirect(request.referrer or url_for("index"))
    return send_file(BytesIO(data), as_attachment=True, download_name=attachment.original_name, mimetype=attachment.content_type)


@app.get("/health")
def health():
    return {"status": "ok"}


with app.app_context():
    restore_database()
    db.create_all()


if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG", "0") == "1", host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
