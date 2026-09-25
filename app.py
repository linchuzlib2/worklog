import os
import re
import uuid
from datetime import datetime, timedelta
from io import BytesIO

import bleach
import boto3
import openpyxl
import xlrd
from botocore.exceptions import BotoCoreError, ClientError
from botocore.config import Config
from docx import Document
from dotenv import load_dotenv
from flask import Flask, flash, jsonify, redirect, render_template, request, send_file, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, inspect, or_

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


class Folder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    parent_id = db.Column(db.Integer, db.ForeignKey("folder.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    parent = db.relationship("Folder", remote_side=[id], backref=db.backref("children", cascade="all, delete-orphan"))
    files = db.relationship("Attachment", backref="folder", order_by="Attachment.created_at.desc()")


class Attachment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    original_name = db.Column(db.String(255), nullable=False)
    object_key = db.Column(db.String(500), nullable=False, unique=True)
    content_type = db.Column(db.String(120), default="application/octet-stream")
    size = db.Column(db.Integer, default=0)
    task_id = db.Column(db.Integer, db.ForeignKey("task.id"), nullable=True)
    note_id = db.Column(db.Integer, db.ForeignKey("note.id"), nullable=True)
    folder_id = db.Column(db.Integer, db.ForeignKey("folder.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Schedule(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    start_at = db.Column(db.DateTime, nullable=False)
    end_at = db.Column(db.DateTime, nullable=True)
    description = db.Column(db.Text, default="")
    task_id = db.Column(db.Integer, db.ForeignKey("task.id"), nullable=True)
    completed = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    task = db.relationship("Task", backref=db.backref("schedules", order_by="Schedule.start_at"))


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


def parse_datetime(value):
    return datetime.strptime(value, "%Y-%m-%dT%H:%M") if value else None


def plain_text(value):
    return re.sub(r"<[^>]+>", " ", value or "")


def extract_attachment_text(data, filename, content_type):
    extension = os.path.splitext(filename.lower())[1]
    if extension == ".docx" or content_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        document = Document(BytesIO(data))
        return "\n".join(paragraph.text for paragraph in document.paragraphs)
    if extension == ".xlsx" or content_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
        workbook = openpyxl.load_workbook(BytesIO(data), read_only=True, data_only=True)
        return "\n".join(
            "\t".join(str(cell) for cell in row if cell is not None)
            for worksheet in workbook.worksheets
            for row in worksheet.iter_rows(values_only=True)
        )
    if extension == ".xls" or content_type == "application/vnd.ms-excel":
        workbook = xlrd.open_workbook(file_contents=data, on_demand=True)
        return "\n".join(
            "\t".join(str(cell) for cell in worksheet.row_values(row_index) if cell != "")
            for worksheet in workbook.sheets()
            for row_index in range(worksheet.nrows)
        )
    if (content_type or "").startswith("text/"):
        return data.decode("utf-8", errors="ignore")
    return ""


def migrate_schema():
    inspector = inspect(db.engine)
    columns = {column["name"] for column in inspector.get_columns("attachment")}
    if "folder_id" not in columns:
        db.session.execute(db.text("ALTER TABLE attachment ADD COLUMN folder_id INTEGER REFERENCES folder(id)"))
        db.session.commit()
    schedule_columns = {column["name"] for column in inspector.get_columns("schedule")}
    if "completed" not in schedule_columns:
        db.session.execute(db.text("ALTER TABLE schedule ADD COLUMN completed BOOLEAN NOT NULL DEFAULT 0"))
        db.session.commit()


@app.context_processor
def inject_counts():
    return {"pending_count": Task.query.filter(Task.status != "done").count() if db.engine else 0, "oss_enabled": storage.enabled, "now": datetime.utcnow(), "timedelta": timedelta}


@app.route("/")
def index():
    status = request.args.get("status", "")
    query = request.args.get("q", "").strip()
    task_query = Task.query
    schedule_query = Schedule.query
    if status in {"todo", "done"}:
        task_query = task_query.filter_by(status=status)
        schedule_query = schedule_query.filter(Schedule.completed.is_(status == "done"))
    if query:
        task_query = task_query.filter(or_(Task.title.ilike(f"%{query}%"), Task.description.ilike(f"%{query}%")))
        schedule_query = schedule_query.filter(or_(Schedule.title.ilike(f"%{query}%"), Schedule.description.ilike(f"%{query}%")))
    tasks = task_query.order_by(Task.created_at.desc()).limit(20).all()
    schedules = schedule_query.order_by(Schedule.start_at).limit(20).all()

    # 合并任务与日程为统一清单
    items = []
    for task in tasks:
        items.append({
            "kind": "task",
            "id": task.id,
            "title": task.title,
            "description": task.description or "",
            "done": task.status == "done",
            "priority": task.priority,
            "time_label": task.due_date.strftime("%m月%d日截止") if task.due_date else "",
            "sort_time": datetime.combine(task.due_date, datetime.min.time()) if task.due_date else task.created_at,
        })
    for item in schedules:
        label = item.start_at.strftime("%m月%d日 %H:%M")
        if item.end_at:
            label += " - " + item.end_at.strftime("%H:%M")
        items.append({
            "kind": "schedule",
            "id": item.id,
            "title": item.title,
            "description": item.description or "",
            "done": bool(item.completed),
            "priority": None,
            "time_label": label,
            "sort_time": item.start_at,
        })
    pending = sorted((i for i in items if not i["done"]), key=lambda i: i["sort_time"])
    finished = sorted((i for i in items if i["done"]), key=lambda i: i["sort_time"], reverse=True)
    items = pending + finished
    shown_pending, shown_finished = len(pending), len(finished)

    notes = Note.query.order_by(Note.updated_at.desc()).limit(8).all()
    total_pending = Task.query.filter(Task.status != "done").count() + Schedule.query.filter(Schedule.completed.is_(False)).count()
    total_done = Task.query.filter(Task.status == "done").count() + Schedule.query.filter(Schedule.completed.is_(True)).count()
    return render_template("index.html", items=items, notes=notes, current_status=status, query=query,
                           total_pending=total_pending, total_done=total_done,
                           shown_pending=shown_pending, shown_finished=shown_finished)


@app.route("/schedule", methods=["GET", "POST"])
def schedule():
    if request.method == "POST":
        start_at = parse_datetime(request.form.get("start_at"))
        end_at = parse_datetime(request.form.get("end_at"))
        if not start_at:
            flash("请填写开始时间", "error")
        else:
            item = Schedule(title=request.form["title"].strip(), start_at=start_at, end_at=end_at, description=request.form.get("description", "").strip(), task_id=request.form.get("task_id", type=int) or None)
            db.session.add(item)
            db.session.commit()
            sync_database()
            flash("日程已创建", "success")
        return redirect(url_for("schedule", week=request.form.get("week")))
    selected = request.args.get("week")
    try:
        week_start = datetime.strptime(selected, "%Y-%m-%d") if selected else datetime.now()
    except ValueError:
        week_start = datetime.now()
    week_start = (week_start - timedelta(days=week_start.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    week_end = week_start + timedelta(days=7)
    items = Schedule.query.filter(Schedule.start_at >= week_start, Schedule.start_at < week_end).order_by(Schedule.start_at).all()
    days = [week_start + timedelta(days=index) for index in range(7)]
    time_slots = [f"{hour:02d}:{minute:02d}" for hour in range(24) for minute in (0, 30)]
    return render_template("schedule.html", items=items, days=days, time_slots=time_slots, week_start=week_start, previous_week=week_start - timedelta(days=7), next_week=week_start + timedelta(days=7), tasks=Task.query.order_by(Task.title).all())


@app.post("/schedule/<int:schedule_id>/delete")
def delete_schedule(schedule_id):
    item = db.get_or_404(Schedule, schedule_id)
    db.session.delete(item)
    db.session.commit()
    sync_database()
    flash("日程已删除", "success")
    return redirect(request.referrer or url_for("schedule"))


@app.post("/schedule/<int:schedule_id>/status")
def update_schedule_status(schedule_id):
    item = db.get_or_404(Schedule, schedule_id)
    item.completed = request.form.get("completed") == "1"
    db.session.commit()
    sync_database()
    return redirect(request.referrer or url_for("schedule"))


@app.route("/search")
def global_search():
    query = request.args.get("q", "").strip()
    task_results = []
    note_results = []
    schedule_results = []
    file_results = []
    if query:
        pattern = f"%{query}%"
        task_results = Task.query.filter(or_(Task.title.ilike(pattern), Task.description.ilike(pattern))).all()
        note_results = Note.query.filter(or_(Note.title.ilike(pattern), Note.content.ilike(pattern))).all()
        schedule_results = Schedule.query.filter(or_(Schedule.title.ilike(pattern), Schedule.description.ilike(pattern))).order_by(Schedule.start_at).all()
        candidates = Attachment.query.filter(Attachment.original_name.ilike(pattern)).all()
        for attachment in Attachment.query.order_by(Attachment.created_at.desc()).all():
            if attachment in candidates or not storage.enabled:
                continue
            try:
                content = extract_attachment_text(storage.download(attachment.object_key), attachment.original_name, attachment.content_type)
                if query.lower() in content.lower():
                    candidates.append(attachment)
            except (BotoCoreError, ClientError, UnicodeError, ValueError, KeyError, OSError):
                continue
        file_results = candidates
    return render_template("search.html", query=query, task_results=task_results, note_results=note_results, schedule_results=schedule_results, file_results=file_results)


@app.get("/attachments/<int:attachment_id>/view")
def view_attachment(attachment_id):
    attachment = db.get_or_404(Attachment, attachment_id)
    query = request.args.get("q", "").strip()
    try:
        content = extract_attachment_text(storage.download(attachment.object_key), attachment.original_name, attachment.content_type)
    except (BotoCoreError, ClientError, UnicodeError, ValueError, KeyError, OSError) as error:
        flash(f"附件读取失败：{error}", "error")
        return redirect(request.referrer or url_for("global_search", q=query))
    return render_template("attachment_detail.html", attachment=attachment, content=content, query=query)


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
    return render_template("task_detail.html", task=task, available_files=Attachment.query.order_by(Attachment.original_name).all())


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
    return render_template("note_detail.html", note=note, available_files=Attachment.query.order_by(Attachment.original_name).all())


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


@app.post("/notes/<int:note_id>/delete")
def delete_note(note_id):
    note = db.get_or_404(Note, note_id)
    for attachment in note.attachments:
        storage.delete(attachment.object_key)
    db.session.delete(note)
    db.session.commit()
    sync_database()
    flash("笔记已删除", "success")
    return redirect(url_for("index"))


@app.route("/files")
def file_manager():
    folder_id = request.args.get("folder_id", type=int)
    folder = db.session.get(Folder, folder_id) if folder_id else None
    if folder_id and not folder:
        return redirect(url_for("file_manager"))
    folders = Folder.query.filter_by(parent_id=folder_id).order_by(Folder.name).all()
    if folder:
        files = Attachment.query.filter_by(folder_id=folder.id).order_by(Attachment.created_at.desc()).all()
    else:
        files = Attachment.query.order_by(Attachment.created_at.desc()).all()
    all_folders = Folder.query.order_by(Folder.name).all()
    children_map = {}
    for entry in all_folders:
        children_map.setdefault(entry.parent_id, []).append(entry)

    def folder_path(entry):
        parts = []
        while entry:
            parts.append(entry.name)
            entry = entry.parent
        return " / ".join(reversed(parts))

    folder_paths = {entry.id: folder_path(entry) for entry in all_folders}
    breadcrumbs = []
    current = folder
    while current:
        breadcrumbs.append(current)
        current = current.parent
    breadcrumbs = list(reversed(breadcrumbs))
    expanded_ids = {entry.id for entry in breadcrumbs}
    return render_template("file_manager.html", folder=folder, folders=folders, files=files, all_folders=all_folders,
                           folder_paths=folder_paths, children_map=children_map, breadcrumbs=breadcrumbs,
                           expanded_ids=expanded_ids)


@app.post("/files/folders")
def create_folder():
    name = request.form.get("name", "").strip()
    parent_id = request.form.get("parent_id", type=int) or None
    if not name:
        flash("请输入文件夹名称", "error")
    else:
        db.session.add(Folder(name=name, parent_id=parent_id))
        db.session.commit()
        sync_database()
        flash("文件夹已创建", "success")
    return redirect(url_for("file_manager", folder_id=parent_id))


def folder_has_content(folder):
    if Attachment.query.filter_by(folder_id=folder.id).first() is not None:
        return True
    children = Folder.query.filter_by(parent_id=folder.id).all()
    return bool(children) or any(folder_has_content(child) for child in children)


@app.post("/files/folders/<int:folder_id>/rename")
def rename_folder(folder_id):
    folder = db.get_or_404(Folder, folder_id)
    name = request.form.get("name", "").strip()
    if name:
        folder.name = name
        db.session.commit()
        sync_database()
        flash("文件夹已改名", "success")
    else:
        flash("文件夹名称不能为空", "error")
    return redirect(url_for("file_manager", folder_id=folder.parent_id))


@app.post("/files/folders/<int:folder_id>/delete")
def delete_folder(folder_id):
    folder = db.get_or_404(Folder, folder_id)
    parent_id = folder.parent_id
    if folder_has_content(folder):
        flash("文件夹或其子文件夹不为空，不能删除", "error")
    else:
        db.session.delete(folder)
        db.session.commit()
        sync_database()
        flash("文件夹已删除", "success")
    return redirect(url_for("file_manager", folder_id=parent_id))


@app.post("/files/<int:attachment_id>/move")
def move_file(attachment_id):
    attachment = db.get_or_404(Attachment, attachment_id)
    payload = request.get_json(silent=True) or request.form
    raw_folder_id = payload.get("folder_id") if hasattr(payload, "get") else None
    folder_id = int(raw_folder_id) if raw_folder_id not in (None, "", 0, "0") else None
    if folder_id and not db.session.get(Folder, folder_id):
        return jsonify({"error": "文件夹不存在"}), 404
    attachment.folder_id = folder_id or None
    db.session.commit()
    sync_database()
    if not request.is_json:
        flash("文件已移动", "success")
        return redirect(request.referrer or url_for("file_manager"))
    return jsonify({"ok": True})


@app.post("/files/<int:attachment_id>/delete")
def delete_file(attachment_id):
    attachment = db.get_or_404(Attachment, attachment_id)
    storage.delete(attachment.object_key)
    db.session.delete(attachment)
    db.session.commit()
    sync_database()
    flash("文件已删除", "success")
    return redirect(request.referrer or url_for("file_manager"))


@app.get("/attachments/check-name")
def check_attachment_name():
    filename = request.args.get("filename", "").strip()
    duplicate = bool(filename and Attachment.query.filter(func.lower(Attachment.original_name) == filename.lower()).first())
    return jsonify({"duplicate": duplicate})


@app.post("/attachments/link")
def link_attachment():
    attachment_id = request.form.get("attachment_id", type=int)
    if not attachment_id:
        flash("请选择文件", "error")
        return redirect(request.referrer or url_for("file_manager"))
    attachment = db.get_or_404(Attachment, attachment_id)
    task_id = request.form.get("task_id", type=int)
    note_id = request.form.get("note_id", type=int)
    if not task_id and not note_id:
        flash("请选择任务或笔记", "error")
    else:
        if task_id:
            db.get_or_404(Task, task_id)
            attachment.task_id = task_id
        if note_id:
            db.get_or_404(Note, note_id)
            attachment.note_id = note_id
        db.session.commit()
        sync_database()
        flash("文件已关联", "success")
    return redirect(request.referrer or url_for("file_manager"))


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
    folder_id = request.form.get("folder_id", type=int)
    allow_duplicate = request.form.get("allow_duplicate") == "1"
    duplicate = Attachment.query.filter(func.lower(Attachment.original_name) == uploaded.filename.strip().lower()).first()
    if duplicate and not allow_duplicate:
        flash(f"已存在同名文件“{uploaded.filename}”，如需继续上传请确认重复上传。", "error")
        return redirect(request.referrer or url_for("file_manager"))
    safe_name = re.sub(r"[^\w.\- ]", "_", uploaded.filename)[:180]
    key = f"attachments/{uuid.uuid4().hex}-{safe_name}"
    try:
        uploaded.stream.seek(0)
        uploaded_size = storage.upload(uploaded, key)
        attachment = Attachment(original_name=uploaded.filename, object_key=key, content_type=uploaded.content_type or "application/octet-stream", size=uploaded_size, task_id=task_id, note_id=note_id, folder_id=folder_id)
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
    migrate_schema()


if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG", "0") == "1", host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
