import csv
import hashlib
import hmac
import json
import mimetypes
import os
import posixpath
import re
import tempfile
import threading
import uuid
import zipfile
from datetime import date, datetime, timedelta, timezone
from io import BytesIO, StringIO
from urllib.parse import unquote, urlsplit

import bleach
import boto3
from ebooklib import epub, ITEM_DOCUMENT
import openpyxl
import xlrd
from botocore.exceptions import BotoCoreError, ClientError
from botocore.config import Config
from docx import Document
from dotenv import load_dotenv
from flask import Flask, flash, jsonify, redirect, render_template, request, send_file, session, url_for
from werkzeug.exceptions import RequestEntityTooLarge

import ai
from flask_sqlalchemy import SQLAlchemy
from lxml import html as lxml_html
from sqlalchemy import event, func, inspect, or_

load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "change-this-secret")
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///worklog.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024

db = SQLAlchemy(app)


@app.errorhandler(RequestEntityTooLarge)
def handle_request_too_large(error):
    app.logger.warning("Request exceeded MAX_CONTENT_LENGTH: path=%s length=%s", request.path, request.content_length)
    if request.path == url_for("upload_attachment"):
        flash("文件总大小超过 100 MiB 上传上限，请拆分成多个批次上传。", "error")
        return redirect(request.referrer or url_for("file_manager"))
    return error

CHINA_TIMEZONE = timezone(timedelta(hours=8), name="China Standard Time")


def china_now():
    return datetime.now(CHINA_TIMEZONE).replace(tzinfo=None)


def to_china_datetime(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(CHINA_TIMEZONE)


@app.template_filter("china_time")
def format_china_time(value, format_string="%Y-%m-%d %H:%M"):
    return to_china_datetime(value).strftime(format_string) if value else ""

ALLOWED_TAGS = set(bleach.sanitizer.ALLOWED_TAGS) | {
    "p", "br", "h1", "h2", "h3", "h4", "blockquote", "pre", "code",
    "ul", "ol", "li", "strong", "em", "u", "s", "a", "img"
}
ALLOWED_ATTRIBUTES = {
    "a": ["href", "title", "target", "rel"],
    "img": ["src", "alt", "title", "width", "height"],
}


class Assignee(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Task(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, default="")
    status = db.Column(db.String(20), default="todo", nullable=False)
    priority = db.Column(db.String(20), default="medium", nullable=False)
    due_date = db.Column(db.Date, nullable=True)
    parent_id = db.Column(db.Integer, db.ForeignKey("task.id"), nullable=True)
    assignee_id = db.Column(db.Integer, db.ForeignKey("assignee.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    notes = db.relationship("Note", backref="task", cascade="all, delete-orphan", order_by="Note.updated_at.desc()")
    attachments = db.relationship("Attachment", backref="task", cascade="all, delete-orphan", order_by="Attachment.created_at.desc()")
    children = db.relationship("Task", backref=db.backref("parent", remote_side=[id]), order_by="Task.created_at", cascade="all, delete-orphan")
    assignee = db.relationship("Assignee", backref=db.backref("tasks", order_by="Task.created_at.desc()"))


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


class KnowledgeDoc(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    filename = db.Column(db.String(255), default="")
    content = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    chunks = db.relationship("KnowledgeChunk", backref="doc", cascade="all, delete-orphan", order_by="KnowledgeChunk.chunk_index")


class KnowledgeChunk(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    doc_id = db.Column(db.Integer, db.ForeignKey("knowledge_doc.id"), nullable=False)
    chunk_index = db.Column(db.Integer, nullable=False)
    text = db.Column(db.Text, nullable=False)
    embedding = db.Column(db.Text, nullable=False)  # JSON 数组


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


_database_sync_requested = threading.Event()
_database_sync_worker_lock = threading.Lock()
_database_sync_worker_thread = None


def _database_sync_worker():
    while True:
        _database_sync_requested.wait()
        _database_sync_requested.clear()
        while _database_sync_requested.wait(timeout=1.0):
            _database_sync_requested.clear()
        try:
            sync_database()
        except Exception:
            app.logger.exception("Background database backup sync failed")


def schedule_database_sync():
    """合并短时间内的导图修改，避免每次交互都等待完整 OSS 数据库上传。"""
    global _database_sync_worker_thread
    if not storage.enabled:
        return
    with _database_sync_worker_lock:
        if _database_sync_worker_thread is None or not _database_sync_worker_thread.is_alive():
            _database_sync_worker_thread = threading.Thread(
                target=_database_sync_worker,
                name="database-backup-sync",
                daemon=True,
            )
            _database_sync_worker_thread.start()
        _database_sync_requested.set()


# ---- 知识库 RAG ----
CHUNK_SIZE = 500
CHUNK_OVERLAP = 60


def chunk_text(text):
    text = re.sub(r"\s+\n", "\n", text.strip())
    chunks = []
    start = 0
    while start < len(text):
        piece = text[start:start + CHUNK_SIZE]
        if len(piece) < CHUNK_SIZE // 3 and chunks:
            chunks[-1] += piece
        else:
            chunks.append(piece)
        start += CHUNK_SIZE - CHUNK_OVERLAP
    return [c for c in chunks if c.strip()]


def embed_document(doc):
    """为文档分块并写入向量（无向量接口时降级为空向量，检索走关键词匹配）。"""
    KnowledgeChunk.query.filter_by(doc_id=doc.id).delete()
    chunks = chunk_text(doc.content)
    vectors = []
    try:
        for start in range(0, len(chunks), 16):
            vectors.extend(ai.embed(chunks[start:start + 16]))
    except ai.EmbedUnavailable:
        vectors = []
    for index, piece in enumerate(chunks):
        vector = vectors[index] if index < len(vectors) else []
        doc.chunks.append(KnowledgeChunk(chunk_index=index, text=piece, embedding=json.dumps(vector)))
    db.session.commit()


def search_knowledge(question, top_k=5):
    """检索与问题最相关的知识块：优先向量相似，无向量时降级关键词匹配。"""
    query_tokens = ai.tokenize(question)
    try:
        query_vector = ai.embed([question])[0]
    except ai.EmbedUnavailable:
        query_vector = None
    results = []
    for chunk in KnowledgeChunk.query.all():
        vector = json.loads(chunk.embedding) if chunk.embedding else []
        if query_vector and vector:
            score = sum(a * b for a, b in zip(query_vector, vector))
        else:
            score = ai.keyword_score(query_tokens, ai.tokenize(chunk.text))
        results.append((chunk, chunk.doc, score))
    results.sort(key=lambda item: item[2], reverse=True)
    return results[:top_k]


def ask_knowledge(question):
    """RAG：检索知识库 → 组装上下文 → AI 生成回答。"""
    if not KnowledgeDoc.query.count():
        return {"answer": "知识库还是空的，请先上传制度文件或管理办法。", "sources": []}
    hits = search_knowledge(question)
    context_blocks = []
    for chunk, doc, _ in hits:
        context_blocks.append(f"【{doc.title}】\n{chunk.text}")
    prompt = (
        "你是企业制度助理。请优先根据下面的知识库资料回答问题；"
        "如果资料中没有相关内容，请如实说明知识库中没有找到，并基于常识简要回答。回答使用简体中文。\n\n"
        "=== 知识库资料 ===\n" + "\n\n".join(context_blocks) + "\n\n=== 问题 ===\n" + question
    )
    answer = ai.chat([{"role": "user", "content": prompt}], temperature=0.2)
    sources = [{"doc": doc.title, "snippet": chunk.text[:120]} for chunk, doc, _ in hits]
    return {"answer": answer, "sources": sources}


def sanitize_html(value):
    return bleach.clean(value or "", tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRIBUTES, protocols=["http", "https", "mailto", "data"], strip=True)


def link_note_image_attachments(content, note_id):
    """历史兼容：笔记图片已统一改为直接存储在正文中的 data URL，不再依赖附件列表。

    这里保留为空实现，避免旧附件引用继续影响新逻辑。
    """
    return None


def parse_date(value):
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


def parse_datetime(value):
    return datetime.strptime(value, "%Y-%m-%dT%H:%M") if value else None


def plain_text(value):
    return re.sub(r"<[^>]+>", " ", value or "")


def normalize_assignee_name(name):
    return (name or "").strip()


def collect_assignee_ids(task):
    ids = set()
    if task is None:
        return ids
    if task.assignee_id is not None:
        ids.add(task.assignee_id)
    for child in task.children:
        ids |= collect_assignee_ids(child)
    return ids


def sync_task_status_tree(task, status=None):
    if task is None:
        return
    if getattr(task, "_syncing_status_tree", False):
        return

    task._syncing_status_tree = True
    try:
        if status is not None:
            task.status = status
        for child in task.children:
            sync_task_status_tree(child, task.status)
    finally:
        task._syncing_status_tree = False


@event.listens_for(Task.status, "set", retval=True)
def _sync_task_status_on_change(target, value, oldvalue, initiator):
    if getattr(target, "_syncing_status_tree", False):
        return value
    sync_task_status_tree(target, value)
    return value


def sync_task_assignee_tree(task, assignee_id=None):
    if task is None:
        return
    if getattr(task, "_syncing_assignee_tree", False):
        return

    task._syncing_assignee_tree = True
    try:
        if assignee_id is not None:
            task.assignee_id = assignee_id

        for child in task.children:
            if child.assignee_id is None or assignee_id is not None:
                child.assignee_id = assignee_id if assignee_id is not None else task.assignee_id
            sync_task_assignee_tree(child, child.assignee_id)

        current = task.parent
        while current:
            ids = collect_assignee_ids(current)
            current.assignee_id = next(iter(ids)) if len(ids) == 1 else None
            current = current.parent
    finally:
        task._syncing_assignee_tree = False


@event.listens_for(Task.assignee_id, "set", retval=True)
def _sync_task_assignee_on_assignment(target, value, oldvalue, initiator):
    if getattr(target, "_syncing_assignee_tree", False):
        return value
    sync_task_assignee_tree(target, value)
    return value


def root_task_for(task):
    current = task
    while current and current.parent:
        current = current.parent
    return current


def extract_attachment_text(data, filename, content_type):
    extension = os.path.splitext(filename.lower())[1]
    if extension == ".docx" or content_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        document = Document(BytesIO(data))
        return "\n".join(paragraph.text for paragraph in document.paragraphs)
    if extension in {".doc", ".docx"}:
        try:
            document = Document(BytesIO(data))
            return "\n".join(paragraph.text for paragraph in document.paragraphs)
        except Exception:
            pass
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


def extract_knowledge_file_text(data, filename):
    lower = filename.lower()
    if lower.endswith(".docx") or lower.endswith(".doc"):
        try:
            document = Document(BytesIO(data))
            return "\n".join(p.text for p in document.paragraphs if p.text.strip())
        except Exception:
            return ""
    if lower.endswith(".xlsx"):
        workbook = openpyxl.load_workbook(BytesIO(data), read_only=True, data_only=True)
        return "\n".join(
            "\t".join(str(cell) for cell in row if cell is not None)
            for worksheet in workbook.worksheets
            for row in worksheet.iter_rows(values_only=True)
        )
    if lower.endswith(".xls"):
        workbook = xlrd.open_workbook(file_contents=data, on_demand=True)
        return "\n".join(
            "\t".join(str(cell) for cell in worksheet.row_values(row_index) if cell != "")
            for worksheet in workbook.sheets()
            for row_index in range(worksheet.nrows)
        )
    if lower.endswith(".pdf"):
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(data))
        pages = []
        for page in reader.pages:
            text = page.extract_text() or ""
            if text.strip():
                pages.append(text)
        return "\n".join(pages)
    if lower.endswith((".txt", ".md")):
        return data.decode("utf-8", errors="ignore")
    raise ValueError("仅支持 .txt / .md / .doc / .docx / .xls / .xlsx / .pdf 文件")


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
    task_columns = {column["name"] for column in inspector.get_columns("task")}
    if "parent_id" not in task_columns:
        db.session.execute(db.text("ALTER TABLE task ADD COLUMN parent_id INTEGER REFERENCES task(id)"))
        db.session.commit()
    if "assignee_id" not in task_columns:
        db.session.execute(db.text("ALTER TABLE task ADD COLUMN assignee_id INTEGER REFERENCES assignee(id)"))
        db.session.commit()
    if not inspector.has_table("assignee"):
        Assignee.__table__.create(bind=db.engine)
        db.session.commit()


@app.context_processor
def inject_counts():
    return {"pending_count": Task.query.filter(Task.status != "done").count() if db.engine else 0, "oss_enabled": storage.enabled, "local_editor_enabled": bool(os.getenv("LOCAL_EDITOR_TOKEN", "").strip()), "now": china_now(), "timedelta": timedelta, "root_task_for": root_task_for}




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
        root_task = root_task_for(task)
        items.append({
            "kind": "task",
            "id": task.id,
            "title": task.title,
            "description": task.description or "",
            "done": task.status == "done",
            "priority": task.priority,
            "parent_title": root_task.title if root_task and root_task.id != task.id else "",
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
    today = china_now().date()
    today_tasks = Task.query.filter(Task.due_date == today, Task.status != "done").order_by(Task.priority).all()
    day_start = datetime.combine(today, datetime.min.time())
    day_end = datetime.combine(today, datetime.max.time())
    today_schedules = Schedule.query.filter(Schedule.start_at.between(day_start, day_end)).order_by(Schedule.start_at).all()
    today_items = []
    for task in today_tasks:
        root_task = root_task_for(task)
        today_items.append({"kind": "task", "id": task.id, "title": task.title, "done": False,
                            "time_label": "截止今天", "priority": task.priority,
                            "parent_title": root_task.title if root_task and root_task.id != task.id else ""})
    for item in today_schedules:
        label = item.start_at.strftime("%H:%M")
        if item.end_at:
            label += " - " + item.end_at.strftime("%H:%M")
        today_items.append({"kind": "schedule", "id": item.id, "title": item.title, "done": bool(item.completed),
                            "time_label": label, "priority": None})
    return render_template("index.html", items=items, notes=notes, current_status=status, query=query,
                           total_pending=total_pending, total_done=total_done,
                           shown_pending=shown_pending, shown_finished=shown_finished,
                           today_items=today_items, today_label=today.strftime("%m月%d日"))


@app.get("/notes")
def note_list():
    """独立笔记清单页：支持按关键词筛选标题/正文，按更新时间倒序。"""
    query = request.args.get("q", "").strip()
    notes_query = Note.query
    if query:
        pattern = f"%{query}%"
        notes_query = notes_query.filter(or_(Note.title.ilike(pattern), Note.content.ilike(pattern)))
    notes = notes_query.order_by(Note.updated_at.desc()).all()
    total = Note.query.count()
    return render_template("note_list.html", notes=notes, query=query, total=total)


@app.get("/tasks/list")
def task_list():
    assignee_id = request.args.get("assignee_id", type=int)
    query = Task.query
    if assignee_id:
        query = query.filter_by(assignee_id=assignee_id)
    tasks = query.order_by(Task.parent_id.is_(None), Task.due_date.is_(None), Task.due_date.asc(), Task.created_at.asc()).all()
    task_map = {task.id: task for task in tasks}
    for task in tasks:
        task._tree_children = []
    for task in tasks:
        parent_id = task.parent_id
        if parent_id in task_map:
            task_map[parent_id]._tree_children.append(task)
    roots = [task for task in tasks if task.parent_id is None or task.parent_id not in task_map]
    all_tasks = Task.query.order_by(Task.created_at.asc()).all()
    return render_template("task_list.html", tasks=roots, assignees=Assignee.query.order_by(Assignee.name).all(), current_assignee_id=assignee_id, task_count=len(tasks), task_list_all_tasks=all_tasks)


@app.get("/tasks/board")
def task_board():
    tasks = Task.query.order_by(Task.created_at.desc()).all()
    columns = {"todo": [], "doing": [], "done": []}
    for task in tasks:
        columns.setdefault(task.status, []).append(task)
    return render_template("task_board.html", columns=columns, assignees=Assignee.query.order_by(Assignee.name).all())


@app.get("/assignees")
def task_assignees():
    assignees = Assignee.query.order_by(Assignee.name).all()
    return render_template("task_assignees.html", assignees=assignees)


@app.post("/assignees")
def create_assignee():
    name = normalize_assignee_name(request.form.get("name"))
    if not name:
        flash("请输入经办人名称", "error")
        return redirect(request.referrer or url_for("task_assignees"))
    assignee = Assignee.query.filter_by(name=name).first()
    if not assignee:
        assignee = Assignee(name=name)
        db.session.add(assignee)
        db.session.commit()
        sync_database()
    flash("经办人已保存", "success")
    return redirect(request.referrer or url_for("task_assignees"))


@app.post("/tasks/<int:task_id>/assign")
def set_task_assignee(task_id):
    task = db.get_or_404(Task, task_id)
    assignee_id = request.form.get("assignee_id", type=int)
    new_name = normalize_assignee_name(request.form.get("new_assignee_name"))
    if assignee_id:
        task.assignee_id = assignee_id
    elif new_name:
        assignee = Assignee.query.filter_by(name=new_name).first()
        if not assignee:
            assignee = Assignee(name=new_name)
            db.session.add(assignee)
            db.session.commit()
        task.assignee_id = assignee.id
    else:
        task.assignee_id = None

    sync_task_assignee_tree(task, task.assignee_id)
    db.session.commit()
    sync_database()
    flash("已更新经办人", "success")
    return redirect(request.referrer or url_for("task_detail", task_id=task.id))


@app.get("/tasks/export")
def export_tasks_for_assignee():
    assignee_id = request.args.get("assignee_id", type=int)
    if not assignee_id:
        flash("请选择要导出的经办人", "error")
        return redirect(url_for("task_assignees"))
    assignee = db.get_or_404(Assignee, assignee_id)
    tasks = Task.query.filter_by(assignee_id=assignee_id).order_by(Task.due_date.is_(None), Task.due_date.asc(), Task.created_at.desc()).all()
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["任务名称", "所属主任务", "状态", "优先级", "截止日期", "经办人", "描述"])
    for task in tasks:
        writer.writerow([
            task.title,
            root_task_for(task).title if root_task_for(task) else "",
            {'todo': '待处理', 'doing': '进行中', 'done': '已完成'}.get(task.status, task.status),
            {'high': '高', 'medium': '中', 'low': '低'}.get(task.priority, task.priority),
            task.due_date.strftime('%Y-%m-%d') if task.due_date else '',
            assignee.name,
            plain_text(task.description).strip(),
        ])
    data = output.getvalue().encode('utf-8-sig')
    return send_file(BytesIO(data), as_attachment=True, download_name=f"{assignee.name}任务清单.csv", mimetype="text/csv; charset=utf-8-sig")


@app.get("/api/reminders")
def api_reminders():
    """返回未完成且已到点/已过期的日程，前端轮询后弹窗提醒，避免遗漏。"""
    now = china_now()
    window_start = now - timedelta(days=7)
    items = (
        Schedule.query
        .filter(Schedule.completed.is_(False), Schedule.start_at <= now, Schedule.start_at >= window_start)
        .order_by(Schedule.start_at)
        .all()
    )
    return jsonify([{
        "id": item.id,
        "title": item.title,
        "time_label": item.start_at.strftime("%H:%M") + (f" - {item.end_at.strftime('%H:%M')}" if item.end_at else ""),
        "task": item.task.title if item.task else "",
        "description": (item.description or "")[:100],
    } for item in items])


@app.route("/schedule", methods=["GET", "POST"])
def schedule():
    if request.method == "POST":
        schedule_id = request.form.get("schedule_id", type=int)
        item = db.get_or_404(Schedule, schedule_id) if schedule_id else None
        start_at = parse_datetime(request.form.get("start_at"))
        end_at = parse_datetime(request.form.get("end_at"))
        if not start_at:
            flash("请填写开始时间", "error")
        else:
            if not end_at or end_at <= start_at:
                end_at = start_at + timedelta(minutes=30)
            if item:
                item.title = request.form["title"].strip()
                item.start_at = start_at
                item.end_at = end_at
                item.description = request.form.get("description", "").strip()
                item.task_id = request.form.get("task_id", type=int) or None
                success_message = "日程已更新"
            else:
                item = Schedule(title=request.form["title"].strip(), start_at=start_at, end_at=end_at, description=request.form.get("description", "").strip(), task_id=request.form.get("task_id", type=int) or None)
                db.session.add(item)
                success_message = "日程已创建"
            db.session.commit()
            try:
                sync_database()
            except Exception:
                app.logger.exception("Failed to sync database after saving schedule")
                flash(f"{success_message}，但数据库备份同步失败，请检查 Render 日志中的 OSS 错误。", "error")
            else:
                flash(success_message, "success")
        return redirect(url_for("schedule", week=request.form.get("week")))
    selected = request.args.get("week")
    try:
        week_start = datetime.strptime(selected, "%Y-%m-%d") if selected else china_now()
    except ValueError:
        week_start = china_now()
    week_start = (week_start - timedelta(days=week_start.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    week_end = week_start + timedelta(days=7)
    items = Schedule.query.filter(Schedule.start_at >= week_start, Schedule.start_at < week_end).order_by(Schedule.start_at).all()
    days = [week_start + timedelta(days=index) for index in range(7)]
    today = china_now().date()
    time_slots = [f"{hour:02d}:{minute:02d}" for hour in range(6, 25) for minute in (0, 30)]
    return render_template("schedule.html", items=items, days=days, today=today, time_slots=time_slots, week_start=week_start, previous_week=week_start - timedelta(days=7), next_week=week_start + timedelta(days=7), tasks=Task.query.order_by(Task.title).all())


@app.post("/schedule/<int:schedule_id>/delete")
def delete_schedule(schedule_id):
    item = db.get_or_404(Schedule, schedule_id)
    db.session.delete(item)
    db.session.commit()
    try:
        sync_database()
    except Exception:
        app.logger.exception("Failed to sync database after deleting schedule")
        flash("日程已删除，但数据库备份同步失败，请检查 Render 日志中的 OSS 错误。", "error")
    else:
        flash("日程已删除", "success")
    return redirect(request.referrer or url_for("schedule"))


@app.post("/schedule/<int:schedule_id>/status")
def update_schedule_status(schedule_id):
    item = db.get_or_404(Schedule, schedule_id)
    item.completed = request.form.get("completed") == "1"
    db.session.commit()
    sync_database()
    return redirect(request.referrer or url_for("schedule"))


@app.post("/schedule/<int:schedule_id>/move")
def move_schedule(schedule_id):
    """拖拽日程到任意时间段：保留原时长，平移 start_at / end_at。"""
    item = db.get_or_404(Schedule, schedule_id)
    payload = request.get_json(silent=True) or request.form
    new_start = parse_datetime(payload.get("start_at"))
    if not new_start:
        if request.is_json:
            return jsonify({"error": "请提供 start_at"}), 400
        flash("请提供开始时间", "error")
        return redirect(request.referrer or url_for("schedule"))
    duration = item.end_at - item.start_at if item.end_at else timedelta(minutes=30)
    item.start_at = new_start
    item.end_at = new_start + duration if item.end_at else None
    db.session.commit()
    sync_database()
    if request.is_json:
        return jsonify({"ok": True})
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
    """任务创建统一在导图页完成，此入口重定向过去。"""
    return redirect(url_for("mindmap"))


@app.route("/tasks/<int:task_id>")
def task_detail(task_id):
    task = db.get_or_404(Task, task_id)
    return render_template("task_detail.html", task=task, assignees=Assignee.query.order_by(Assignee.name).all(), available_files=Attachment.query.order_by(Attachment.original_name).all())


@app.route("/tasks/<int:task_id>/edit", methods=["GET", "POST"])
def edit_task(task_id):
    """任务编辑统一在导图页完成，此入口重定向过去。"""
    db.get_or_404(Task, task_id)
    return redirect(url_for("mindmap"))


@app.post("/tasks/<int:task_id>/status")
def update_task_status(task_id):
    task = db.get_or_404(Task, task_id)
    payload = request.get_json(silent=True) or request.form
    task.status = payload.get("status") or "todo"
    db.session.commit()
    if request.is_json:
        schedule_database_sync()
        return jsonify({"ok": True})
    sync_database()
    next_url = payload.get("next", "")
    if next_url and str(next_url).startswith("/"):
        return redirect(next_url)
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
    if request.is_json:
        return jsonify({"ok": True})
    payload = request.get_json(silent=True) or request.form
    next_url = payload.get("next", "")
    if next_url and str(next_url).startswith("/"):
        return redirect(next_url)
    return redirect(url_for("index"))


@app.route("/notes/new", methods=["GET", "POST"])
def new_note():
    if request.method == "POST":
        note = Note(title=request.form["title"].strip(), content=sanitize_html(request.form.get("content")), task_id=request.form.get("task_id", type=int) or None)
        db.session.add(note)
        db.session.commit()
        schedule_database_sync()
        flash("笔记已保存", "success")
        return redirect(url_for("note_detail", note_id=note.id))
    return render_template("note_form.html", note=None, tasks=Task.query.order_by(Task.title).all(), selected_task_id=request.args.get("task_id", type=int))


@app.route("/notes/<int:note_id>")
def note_detail(note_id):
    note = db.get_or_404(Note, note_id)
    return render_template("note_detail.html", note=note, available_files=Attachment.query.order_by(Attachment.original_name).all(), tasks=Task.query.order_by(Task.title).all())


@app.route("/notes/<int:note_id>/edit", methods=["GET", "POST"])
def edit_note(note_id):
    note = db.get_or_404(Note, note_id)
    if request.method == "POST":
        note.title = request.form["title"].strip()
        note.content = sanitize_html(request.form.get("content"))
        note.task_id = request.form.get("task_id", type=int) or None
        db.session.commit()
        schedule_database_sync()
        flash("笔记已更新", "success")
        return redirect(url_for("note_detail", note_id=note.id))
    return render_template("note_form.html", note=note, tasks=Task.query.order_by(Task.title).all(), selected_task_id=note.task_id)


@app.post("/notes/<int:note_id>/delete")
def delete_note(note_id):
    note = db.session.get(Note, note_id)
    if note:  # 已不存在时静默成功（幂等），避免重复提交报 404
        for attachment in note.attachments:
            storage.delete(attachment.object_key)
        db.session.delete(note)
        db.session.commit()
        sync_database()
    flash("笔记已删除", "success")
    # 来源页若是被删笔记自己的详情页，则跳回工作台，避免跳转到已失效的地址
    referrer = request.referrer or ""
    if f"/notes/{note_id}" in referrer:
        return redirect(url_for("index"))
    return redirect(referrer or url_for("index"))


@app.route("/notes/<int:note_id>/polish", methods=["POST"])
def polish_note(note_id):
    """AI 润色已有笔记：润色后直接保存到数据库。"""
    note = db.get_or_404(Note, note_id)
    payload = request.get_json(silent=True) or {}
    raw = bleach.clean(payload.get("content") or note.content, tags=[], strip=True).strip()
    if not raw:
        return jsonify({"error": "笔记内容为空，无法润色"}), 400
    prompt = (
        "请润色以下笔记内容：修正错别字和标点，理顺语句，让表达更清晰专业，"
        "保留原有结构和要点，不要遗漏信息，不要添加知识库之外的新结论。直接输出润色后的全文，不要解释。\n\n" + raw
    )
    try:
        polished = ai.chat([{"role": "user", "content": prompt}], temperature=0.3)
    except Exception as error:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(error)}), 502
    paragraphs = [line.strip() for line in polished.splitlines() if line.strip()]
    html = "".join(f"<p>{line}</p>" for line in paragraphs)
    if not html.strip():
        # AI 返回空结果时绝不能覆盖原笔记，保底返回错误
        return jsonify({"error": "AI 返回了空内容，为保护原笔记未做任何修改"}), 502
    note.content = sanitize_html(html)
    note.updated_at = datetime.utcnow()
    db.session.commit()
    sync_database()
    return jsonify({"html": note.content, "text": polished})


@app.post("/polish")
def polish_text():
    """通用 AI 润色：编辑器中的草稿内容（尚未保存为笔记）润色后返回，不落库。"""
    payload = request.get_json(silent=True) or {}
    raw = bleach.clean(payload.get("content") or "", tags=[], strip=True).strip()
    if not raw:
        return jsonify({"error": "内容为空，无法润色"}), 400
    prompt = (
        "请润色以下笔记内容：修正错别字和标点，理顺语句，让表达更清晰专业，"
        "保留原有结构和要点，不要遗漏信息。直接输出润色后的全文，不要解释。\n\n" + raw
    )
    try:
        polished = ai.chat([{"role": "user", "content": prompt}], temperature=0.3)
    except Exception as error:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(error)}), 502
    paragraphs = [line.strip() for line in polished.splitlines() if line.strip()]
    html = "".join(f"<p>{line}</p>" for line in paragraphs)
    if not html.strip():
        return jsonify({"error": "AI 返回了空内容，请重试"}), 502
    return jsonify({"html": html, "text": polished})


# ---- 知识库 ----
@app.get("/knowledge")
def knowledge():
    docs = KnowledgeDoc.query.order_by(KnowledgeDoc.created_at.desc()).all()
    return render_template("knowledge.html", docs=docs, ai_ready=ai.enabled())


@app.post("/knowledge/upload")
def knowledge_upload():
    try:
        return _knowledge_upload_inner()
    except Exception as error:  # 兜底：任何异常都转为页面提示，避免 500
        import traceback
        traceback.print_exc()
        db.session.rollback()
        flash(f"导入失败：{error}", "error")
        return redirect(url_for("knowledge"))


def _knowledge_upload_inner():
    title = (request.form.get("title") or "").strip()
    pasted = (request.form.get("content") or "").strip()
    uploads = [upload for upload in request.files.getlist("files") if upload and upload.filename]
    if not uploads:
        legacy_upload = request.files.get("file")
        if legacy_upload and legacy_upload.filename:
            uploads = [legacy_upload]

    if uploads:
        imported_docs = []
        failures = []
        known_filenames = {
            os.path.basename((stored_name or "").replace("\\", "/")).strip().casefold()
            for (stored_name,) in KnowledgeDoc.query.with_entities(KnowledgeDoc.filename).all()
            if stored_name
        }
        for upload in uploads:
            filename = os.path.basename(upload.filename.replace("\\", "/")).strip()
            normalized_filename = filename.casefold()
            if normalized_filename in known_filenames:
                failures.append(f"{filename}: 文件名已存在，未重复导入")
                continue
            try:
                content = extract_knowledge_file_text(upload.read(), filename)
                if not content.strip():
                    if filename.lower().endswith(".pdf"):
                        raise ValueError("PDF 未提取到文字，可能是扫描件或图片版")
                    raise ValueError("文件中没有可导入的文字")

                doc = KnowledgeDoc(title=filename[:200], filename=filename, content=content)
                db.session.add(doc)
                db.session.commit()
                try:
                    embed_document(doc)
                except Exception:
                    db.session.rollback()
                    failed_doc = db.session.get(KnowledgeDoc, doc.id)
                    if failed_doc:
                        db.session.delete(failed_doc)
                        db.session.commit()
                    raise
                imported_docs.append(doc)
                known_filenames.add(normalized_filename)
            except Exception as error:
                db.session.rollback()
                app.logger.exception("Failed to import knowledge file %s", filename)
                failures.append(f"{filename}: {error}")

        if imported_docs:
            total_chunks = sum(len(doc.chunks) for doc in imported_docs)
            schedule_database_sync()
            flash(f"已导入 {len(imported_docs)} 个文件、{total_chunks} 个知识块，标题使用原文件名。", "success")
        if failures:
            visible_failures = failures[:5]
            if len(failures) > len(visible_failures):
                visible_failures.append(f"另有 {len(failures) - len(visible_failures)} 个文件导入失败")
            flash("部分文件导入失败：" + "；".join(visible_failures), "error")
        return redirect(url_for("knowledge"))

    if not pasted:
        flash("请上传文件或粘贴文本内容", "error")
        return redirect(url_for("knowledge"))
    if not title:
        title = "未命名文档"
    doc = KnowledgeDoc(title=title[:200], filename="", content=pasted)
    db.session.add(doc)
    db.session.commit()
    try:
        embed_document(doc)
    except Exception as error:
        db.session.delete(doc)
        db.session.commit()
        raise error
    schedule_database_sync()
    flash(f"已导入「{doc.title}」（{len(doc.chunks)} 个知识块）", "success")
    return redirect(url_for("knowledge"))


@app.post("/knowledge/doc/<int:doc_id>/delete")
def knowledge_delete(doc_id):
    doc = db.get_or_404(KnowledgeDoc, doc_id)
    db.session.delete(doc)
    db.session.commit()
    schedule_database_sync()
    flash("文档已删除", "success")
    return redirect(url_for("knowledge"))


@app.post("/knowledge/ask")
def knowledge_ask():
    question = (request.get_json(silent=True) or {}).get("question", "").strip()
    if not question:
        return jsonify({"error": "请输入问题"}), 400
    try:
        result = ask_knowledge(question)
    except Exception as error:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(error)}), 502
    return jsonify(result)


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
    filenames = [os.path.basename(name.replace("\\", "/")).strip() for name in request.args.getlist("filename") if name.strip()]
    normalized_names = [name.casefold() for name in filenames]
    existing_names = {
        name.casefold()
        for (name,) in Attachment.query.with_entities(Attachment.original_name)
        .filter(func.lower(Attachment.original_name).in_(normalized_names))
        .all()
    } if normalized_names else set()
    seen = set()
    duplicates = []
    for name, normalized in zip(filenames, normalized_names):
        if normalized in existing_names or normalized in seen:
            duplicates.append(name)
        seen.add(normalized)
    return jsonify({"duplicate": bool(duplicates), "duplicates": duplicates})


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


@app.post("/attachments/<int:attachment_id>/unlink")
def unlink_attachment(attachment_id):
    attachment = db.get_or_404(Attachment, attachment_id)
    target = request.form.get("target")
    if target == "task" and attachment.task_id:
        attachment.task_id = None
        db.session.commit()
        sync_database()
        flash("已解除与任务的关联，文件仍保留在文件管理中", "success")
    elif target == "note" and attachment.note_id:
        attachment.note_id = None
        db.session.commit()
        sync_database()
        flash("已解除与笔记的关联，文件仍保留在文件管理中", "success")
    else:
        flash("没有可解除的关联", "error")
    return redirect(request.referrer or url_for("file_manager"))


def mindmap_tree_data():
    tasks = Task.query.order_by(Task.created_at).all()
    nodes = {
        task.id: {
            "id": task.id,
            "parent_id": task.parent_id,
            "title": task.title,
            "description": task.description or "",
            "status": task.status,
            "priority": task.priority,
            "due_date": task.due_date.strftime("%Y-%m-%d") if task.due_date else "",
            "assignee_id": task.assignee_id,
            "notes": [],
            "attachments": [],
            "children": [],
        }
        for task in tasks
    }
    if not nodes:
        return []

    task_ids = list(nodes)
    assignee_ids = {node["assignee_id"] for node in nodes.values() if node["assignee_id"] is not None}
    assignees = {
        assignee.id: assignee.name
        for assignee in Assignee.query.filter(Assignee.id.in_(assignee_ids)).all()
    } if assignee_ids else {}
    for node in nodes.values():
        node["assignee_name"] = assignees.get(node["assignee_id"], "")

    note_rows = Note.query.with_entities(Note.id, Note.task_id, Note.title).filter(Note.task_id.in_(task_ids)).order_by(Note.updated_at.desc()).all()
    for note_id, task_id, title in note_rows:
        nodes[task_id]["notes"].append({"id": note_id, "title": title})
    attachment_rows = Attachment.query.with_entities(Attachment.id, Attachment.task_id, Attachment.original_name).filter(Attachment.task_id.in_(task_ids)).order_by(Attachment.created_at.desc()).all()
    for attachment_id, task_id, filename in attachment_rows:
        nodes[task_id]["attachments"].append({"id": attachment_id, "name": filename})

    roots = []
    for task in tasks:
        node = nodes[task.id]
        parent = nodes.get(task.parent_id)
        if parent:
            parent["children"].append(node)
        else:
            roots.append(node)
    for node in nodes.values():
        node.pop("parent_id")
    return roots


@app.context_processor
def inject_static_version():
    def static_version(filename):
        path = os.path.join(app.static_folder or "static", filename)
        try:
            return str(int(os.path.getmtime(path)))
        except OSError:
            return "0"
    return dict(static_version=static_version)


@app.get("/mindmap")
def mindmap():
    tree = mindmap_tree_data()
    notes = Note.query.order_by(Note.updated_at.desc()).all()
    attachments = Attachment.query.order_by(Attachment.created_at.desc()).all()
    assignees = Assignee.query.order_by(Assignee.name).all()
    return render_template("mindmap.html", tree=tree, notes=notes, attachments=attachments, assignees=assignees)


@app.get("/mindmap/data")
def mindmap_data():
    return jsonify({"tree": mindmap_tree_data()})


@app.post("/mindmap/tasks/create")
def mindmap_create_task():
    payload = request.get_json(silent=True) or request.form
    title = (payload.get("title") or "").strip()
    raw_parent = payload.get("parent_id")
    parent_id = int(raw_parent) if raw_parent not in (None, "", 0, "0") else None
    if not title:
        if request.is_json:
            return jsonify({"error": "请填写节点名称"}), 400
        flash("请填写节点名称", "error")
    else:
        if parent_id:
            db.get_or_404(Task, parent_id)
        parent = db.session.get(Task, parent_id) if parent_id else None
        task = Task(title=title, parent_id=parent_id, assignee_id=parent.assignee_id if parent else None)
        db.session.add(task)
        db.session.flush()
        if task.assignee_id is not None:
            sync_task_assignee_tree(task, task.assignee_id)
        db.session.commit()
        if request.is_json:
            schedule_database_sync()
            return jsonify({"ok": True, "id": task.id})
        sync_database()
        flash("节点已创建", "success")
    return redirect(url_for("mindmap"))


@app.post("/mindmap/tasks/<int:task_id>/move")
def mindmap_move_task(task_id):
    task = db.get_or_404(Task, task_id)
    payload = request.get_json(silent=True) or request.form
    raw_parent = payload.get("parent_id")
    new_parent_id = int(raw_parent) if raw_parent not in (None, "", 0, "0") else None
    if new_parent_id == task.id:
        return jsonify({"error": "不能移动到自身"}), 400
    if new_parent_id:
        ancestor = db.session.get(Task, new_parent_id)
        while ancestor:
            if ancestor.id == task.id:
                return jsonify({"error": "不能移动到自己的子节点下"}), 400
            ancestor = ancestor.parent
        task.parent_id = new_parent_id
        parent = db.session.get(Task, new_parent_id)
        if parent and parent.assignee_id is not None:
            task.assignee_id = parent.assignee_id
    else:
        task.parent_id = None
    sync_task_assignee_tree(task, task.assignee_id)
    db.session.commit()
    schedule_database_sync()
    return jsonify({"ok": True})


@app.post("/mindmap/tasks/<int:task_id>/delete-node")
def mindmap_delete_node(task_id):
    """仅删除当前节点，子节点提升一级（模仿 MindNow 删除逻辑）"""
    with db.session.no_autoflush:
        task = db.get_or_404(Task, task_id)
        old_parent_id = task.parent_id
        children_ids = [child.id for child in Task.query.filter_by(parent_id=task_id).all()]
    db.session.expunge_all()
    db.session.execute(db.update(Task).where(Task.parent_id == task_id).values(parent_id=old_parent_id))
    db.session.commit()
    task = db.session.get(Task, task_id)
    db.session.delete(task)
    db.session.commit()
    schedule_database_sync()
    return jsonify({"ok": True})


@app.post("/mindmap/tasks/<int:task_id>/rename")
def mindmap_rename_task(task_id):
    task = db.get_or_404(Task, task_id)
    payload = request.get_json(silent=True) or request.form
    title = (payload.get("title") or "").strip()
    if title:
        task.title = title
        db.session.commit()
        if request.is_json:
            schedule_database_sync()
            return jsonify({"ok": True})
        sync_database()
        flash("节点已重命名", "success")
    return redirect(url_for("mindmap"))


@app.post("/mindmap/tasks/<int:task_id>/update")
def mindmap_update_task(task_id):
    task = db.get_or_404(Task, task_id)
    payload = request.get_json(silent=True) or request.form
    title = (payload.get("title") or "").strip()
    if title:
        task.title = title
    task.description = sanitize_html(payload.get("description"))
    task.status = payload.get("status") or "todo"
    task.priority = payload.get("priority") or "medium"
    task.due_date = parse_date(payload.get("due_date"))

    raw_parent = payload.get("parent_id")
    if raw_parent not in (None, "", "0"):
        new_parent_id = int(raw_parent)
        if new_parent_id != task.id:
            candidate = db.session.get(Task, new_parent_id)
            if candidate is None:
                flash("上级任务不存在", "error")
                return redirect(request.referrer or url_for("task_list"))
            current = candidate
            while current:
                if current.id == task.id:
                    flash("不能把任务移动到自己的子节点下", "error")
                    return redirect(request.referrer or url_for("task_list"))
                current = current.parent
            task.parent_id = new_parent_id
        else:
            flash("不能指定自己为上级任务", "error")
            return redirect(request.referrer or url_for("task_list"))
    elif payload.get("parent_id") in (None, "", "0"):
        task.parent_id = None

    assignee_id = payload.get("assignee_id")
    raw_assignee = payload.get("new_assignee_name")
    if assignee_id not in (None, "", "0"):
        task.assignee_id = int(assignee_id)
    elif raw_assignee:
        name = normalize_assignee_name(raw_assignee)
        if name:
            assignee = Assignee.query.filter_by(name=name).first()
            if not assignee:
                assignee = Assignee(name=name)
                db.session.add(assignee)
                db.session.commit()
            task.assignee_id = assignee.id
    elif payload.get("assignee_id") in (None, "", "0"):
        task.assignee_id = None
    sync_task_assignee_tree(task, task.assignee_id)
    db.session.commit()
    if request.is_json:
        schedule_database_sync()
        return jsonify({"ok": True})
    sync_database()
    flash("任务已更新", "success")
    next_url = payload.get("next") or request.args.get("next")
    if next_url and str(next_url).startswith("/"):
        return redirect(next_url)
    return redirect(request.referrer or url_for("task_list"))


@app.post("/mindmap/tasks/<int:task_id>/link")
def mindmap_link(task_id):
    task = db.get_or_404(Task, task_id)
    payload = request.get_json(silent=True) or request.form
    kind = payload.get("kind")
    try:
        item_id = int(payload.get("item_id") or 0)
    except (TypeError, ValueError):
        item_id = 0
    if kind == "note" and item_id:
        note = db.get_or_404(Note, item_id)
        note.task_id = task.id
    elif kind == "attachment" and item_id:
        attachment = db.get_or_404(Attachment, item_id)
        attachment.task_id = task.id
    else:
        if request.is_json:
            return jsonify({"error": "请选择要关联的内容"}), 400
        flash("请选择要关联的内容", "error")
        return redirect(url_for("mindmap"))
    db.session.commit()
    schedule_database_sync()
    if request.is_json:
        return jsonify({"ok": True})
    flash("已关联", "success")
    return redirect(url_for("mindmap"))


@app.post("/mindmap/tasks/<int:task_id>/unlink")
def mindmap_unlink(task_id):
    task = db.get_or_404(Task, task_id)
    payload = request.get_json(silent=True) or request.form
    kind = payload.get("kind")
    try:
        item_id = int(payload.get("item_id") or 0)
    except (TypeError, ValueError):
        item_id = 0
    if kind == "note" and item_id:
        note = db.get_or_404(Note, item_id)
        if note.task_id == task.id:
            note.task_id = None
    elif kind == "attachment" and item_id:
        attachment = db.get_or_404(Attachment, item_id)
        if attachment.task_id == task.id:
            attachment.task_id = None
    db.session.commit()
    schedule_database_sync()
    if request.is_json:
        return jsonify({"ok": True})
    flash("已解除关联", "success")
    return redirect(url_for("mindmap"))


@app.post("/attachments/upload")
def upload_attachment():
    uploads = [uploaded for uploaded in request.files.getlist("files") if uploaded and uploaded.filename]
    if not uploads:
        legacy_upload = request.files.get("file")
        if legacy_upload and legacy_upload.filename:
            uploads = [legacy_upload]
    if not uploads:
        flash("请选择附件", "error")
        return redirect(request.referrer or url_for("index"))
    if not storage.enabled:
        flash("附件上传需要先配置阿里云 OSS 环境变量", "error")
        return redirect(request.referrer or url_for("index"))
    task_id = request.form.get("task_id", type=int)
    note_id = request.form.get("note_id", type=int)
    folder_id = request.form.get("folder_id", type=int)
    allow_duplicate = request.form.get("allow_duplicate") == "1"
    try:
        existing_names = {
            name.casefold()
            for (name,) in Attachment.query.with_entities(Attachment.original_name).all()
            if name
        }
    except Exception as error:
        db.session.rollback()
        app.logger.exception("Failed to check existing attachment names")
        flash(f"无法开始上传：读取已有文件列表失败：{error}", "error")
        return redirect(request.referrer or url_for("file_manager"))
    seen_names = set()
    uploaded_count = 0
    failures = []
    for uploaded in uploads:
        filename = os.path.basename(uploaded.filename.replace("\\", "/")).strip()
        normalized_name = filename.casefold()
        if not filename:
            failures.append("有一个文件名为空，已跳过")
            continue
        if not allow_duplicate and (normalized_name in existing_names or normalized_name in seen_names):
            failures.append(f"{filename}: 文件名重复，已跳过")
            seen_names.add(normalized_name)
            continue

        safe_name = re.sub(r"[^\w.\- ]", "_", filename)[:180]
        key = f"attachments/{uuid.uuid4().hex}-{safe_name}"
        uploaded_size = None
        try:
            uploaded.stream.seek(0)
            uploaded_size = storage.upload(uploaded, key)
            attachment = Attachment(original_name=filename, object_key=key, content_type=uploaded.content_type or "application/octet-stream", size=uploaded_size, task_id=task_id, note_id=note_id, folder_id=folder_id)
            db.session.add(attachment)
            db.session.commit()
            uploaded_count += 1
            existing_names.add(normalized_name)
            seen_names.add(normalized_name)
        except Exception as error:
            db.session.rollback()
            if uploaded_size is not None:
                try:
                    storage.delete(key)
                except Exception:
                    app.logger.exception("Failed to clean up uploaded object after attachment save failure")
            app.logger.exception("Attachment upload failed for %s", filename)
            failures.append(f"{filename}: {error}")

    if uploaded_count:
        try:
            schedule_database_sync()
        except Exception:
            app.logger.exception("Failed to schedule database backup after attachment upload")
            flash(f"已上传 {uploaded_count} 个文件，但数据库备份排队失败，请检查服务器日志。", "error")
        else:
            flash(f"成功上传 {uploaded_count} 个文件", "success")
    if failures:
        visible_failures = failures[:5]
        if len(failures) > len(visible_failures):
            visible_failures.append(f"另有 {len(failures) - len(visible_failures)} 个文件失败或重名")
        flash("部分文件未上传：" + "；".join(visible_failures), "error")
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


def authorize_local_editor():
    expected_token = os.getenv("LOCAL_EDITOR_TOKEN", "").strip()
    scheme, separator, provided_token = request.headers.get("Authorization", "").partition(" ")
    if not expected_token:
        return jsonify({"error": "本机编辑未配置同步密钥"}), 503
    if scheme.lower() != "bearer" or not separator or not hmac.compare_digest(provided_token, expected_token):
        return jsonify({"error": "同步密钥无效"}), 401
    return None


def local_editor_attachment(attachment_id):
    attachment = db.get_or_404(Attachment, attachment_id)
    extension = os.path.splitext(attachment.original_name)[1].lower()
    if extension not in {".doc", ".docx", ".xls", ".xlsx", ".pdf", ".epub"}:
        return None, (jsonify({"error": "仅支持 doc、docx、xls、xlsx、pdf、epub 文件"}), 415)
    if not storage.enabled:
        return None, (jsonify({"error": "OSS 存储未配置"}), 503)
    return attachment, None


def _epub_toc_titles(book):
    titles = {}

    def visit(entries):
        for entry in entries or []:
            if isinstance(entry, tuple) and len(entry) == 2:
                visit([entry[0]])
                visit(entry[1])
            elif hasattr(entry, "href") and getattr(entry, "href", None):
                path = posixpath.normpath(unquote(urlsplit(entry.href).path))
                titles[path] = (getattr(entry, "title", "") or "").strip()

    visit(book.toc)
    return titles


def _epub_chapters(attachment, data):
    with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as temporary_file:
        temporary_file.write(data)
        temporary_path = temporary_file.name

    reader = epub.EpubReader(temporary_path, options={"ignore_ncx": True})
    try:
        book = reader.load()
        reader.process()
    finally:
        if reader.zf:
            reader.zf.close()
        try:
            os.unlink(temporary_path)
        except OSError:
            app.logger.warning("Could not remove temporary EPUB file")

    spine_items = []
    for spine_entry in book.spine:
        item_id = spine_entry[0] if isinstance(spine_entry, tuple) else spine_entry
        item = book.get_item_with_id(item_id) if isinstance(item_id, str) else item_id
        if item and item.get_type() == ITEM_DOCUMENT:
            spine_items.append(item)
    if not spine_items:
        raise ValueError("EPUB 中没有可阅读的章节")

    toc_titles = _epub_toc_titles(book)
    chapter_indexes = {posixpath.normpath(item.get_name()): index for index, item in enumerate(spine_items)}
    chapters = []
    for item in spine_items:
        chapter_path = posixpath.normpath(item.get_name())
        document = lxml_html.document_fromstring(item.get_content())
        body = document.find("body")
        body = body if body is not None else document
        for element in body.iter():
            if not isinstance(element.tag, str):
                continue
            tag = element.tag.lower()
            for attribute in ("src", "poster"):
                resource = element.get(attribute)
                if not resource:
                    continue
                parsed_resource = urlsplit(resource)
                if parsed_resource.scheme or parsed_resource.netloc or resource.startswith("data:"):
                    continue
                resource_path = posixpath.normpath(posixpath.join(posixpath.dirname(chapter_path), unquote(parsed_resource.path)))
                if resource_path.startswith("../") or resource_path == "..":
                    element.attrib.pop(attribute, None)
                    continue
                element.set(attribute, url_for("epub_resource", attachment_id=attachment.id, resource=resource_path))

            href = element.get("href") if tag == "a" else None
            if href:
                parsed_href = urlsplit(href)
                if not parsed_href.scheme and not parsed_href.netloc and not href.startswith("#"):
                    linked_path = posixpath.normpath(posixpath.join(posixpath.dirname(chapter_path), unquote(parsed_href.path)))
                    linked_index = chapter_indexes.get(linked_path)
                    if linked_index is not None:
                        element.set("data-epub-chapter", str(linked_index))
                        element.set("href", "#")
                    elif not parsed_href.path:
                        element.set("href", "#" + parsed_href.fragment)
                    else:
                        element.attrib.pop("href", None)

        body_html = (body.text or "") + "".join(
            lxml_html.tostring(child, encoding="unicode", method="html")
            for child in body
        )
        safe_html = bleach.clean(
            body_html,
            tags=ALLOWED_TAGS | {
                "article", "section", "div", "span", "figure", "figcaption", "table", "thead",
                "tbody", "tfoot", "tr", "th", "td", "dl", "dt", "dd", "sup", "sub", "del", "ins",
                "hr", "img",
            },
            attributes={
                "*": ["class", "id", "title", "lang", "dir"],
                "a": ["href", "class", "id", "title", "data-epub-chapter"],
                "img": ["src", "alt", "title", "width", "height"],
            },
            protocols=["http", "https", "mailto"],
            strip=True,
        )
        chapters.append({
            "title": toc_titles.get(chapter_path) or getattr(item, "title", "") or os.path.basename(chapter_path),
            "content": safe_html,
        })
    return {"title": book.title or attachment.original_name, "chapters": chapters}


@app.get("/attachments/<int:attachment_id>/epub")
def epub_reader(attachment_id):
    attachment = db.get_or_404(Attachment, attachment_id)
    if os.path.splitext(attachment.original_name)[1].lower() != ".epub":
        return "仅支持 EPUB 文件", 415
    return render_template("epub_reader.html", attachment=attachment)


@app.get("/api/attachments/<int:attachment_id>/epub")
def epub_reader_data(attachment_id):
    attachment = db.get_or_404(Attachment, attachment_id)
    if os.path.splitext(attachment.original_name)[1].lower() != ".epub":
        return jsonify({"error": "仅支持 EPUB 文件"}), 415
    try:
        data = storage.download(attachment.object_key)
        return jsonify(_epub_chapters(attachment, data))
    except Exception as error:
        app.logger.exception("Failed to parse EPUB attachment %s", attachment_id)
        return jsonify({"error": f"EPUB 读取失败：{error}"}), 422


@app.get("/attachments/<int:attachment_id>/epub/resource")
def epub_resource(attachment_id):
    attachment = db.get_or_404(Attachment, attachment_id)
    if os.path.splitext(attachment.original_name)[1].lower() != ".epub":
        return "仅支持 EPUB 文件", 415
    resource = posixpath.normpath(unquote(request.args.get("resource", "")).replace("\\", "/"))
    if not resource or resource.startswith("../") or resource == ".." or resource.startswith("/"):
        return "资源不存在", 404
    try:
        data = storage.download(attachment.object_key)
        with zipfile.ZipFile(BytesIO(data)) as archive:
            resource_data = archive.read(resource)
    except (BotoCoreError, ClientError, KeyError, zipfile.BadZipFile) as error:
        return jsonify({"error": f"EPUB 资源读取失败：{error}"}), 404
    return send_file(BytesIO(resource_data), mimetype=mimetypes.guess_type(resource)[0] or "application/octet-stream")


@app.get("/api/local-editor/<int:attachment_id>")
def local_editor_info(attachment_id):
    auth_error = authorize_local_editor()
    if auth_error:
        return auth_error
    attachment, error_response = local_editor_attachment(attachment_id)
    if error_response:
        return error_response
    return jsonify({"filename": attachment.original_name})


@app.route("/api/local-editor/<int:attachment_id>/content", methods=["GET", "PUT"])
def local_editor_content(attachment_id):
    auth_error = authorize_local_editor()
    if auth_error:
        return auth_error
    attachment, error_response = local_editor_attachment(attachment_id)
    if error_response:
        return error_response

    try:
        current_data = storage.download(attachment.object_key)
    except (BotoCoreError, ClientError) as error:
        app.logger.exception("Failed to read document for local editing")
        return jsonify({"error": f"文件读取失败：{error}"}), 502

    current_revision = hashlib.sha256(current_data).hexdigest()
    if request.method == "GET":
        response = send_file(BytesIO(current_data), as_attachment=True, download_name=attachment.original_name, mimetype="application/octet-stream")
        response.set_etag(current_revision)
        return response

    updated_data = request.get_data(cache=False)
    if not updated_data:
        return jsonify({"error": "不能用空文件覆盖原文档"}), 400
    expected_revision = request.headers.get("If-Match", "").strip().strip('"')
    if not expected_revision or not hmac.compare_digest(expected_revision, current_revision):
        updated_revision = hashlib.sha256(updated_data).hexdigest()
        if hmac.compare_digest(updated_revision, current_revision):
            return jsonify({"ok": True, "revision": current_revision, "unchanged": True})
        return jsonify({"error": "云端文件已被其他操作修改，请重新打开最新版本后再编辑"}), 409

    try:
        storage.upload_bytes(updated_data, attachment.object_key, attachment.content_type or "application/octet-stream")
        attachment.size = len(updated_data)
        db.session.commit()
    except Exception as error:
        db.session.rollback()
        app.logger.exception("Failed to replace document from local editor")
        return jsonify({"error": f"文件推送失败：{error}"}), 502

    try:
        sync_database()
    except Exception:
        app.logger.exception("Failed to sync database after local document edit")
    return jsonify({"ok": True, "revision": hashlib.sha256(updated_data).hexdigest()})


@app.get("/attachments/<int:attachment_id>/inline")
def inline_attachment(attachment_id):
    """以 inline 方式返回附件，用于富文本中的 <img src="/attachments/<id>/inline">。"""
    attachment = db.get_or_404(Attachment, attachment_id)
    try:
        data = storage.download(attachment.object_key)
    except (BotoCoreError, ClientError) as error:
        return jsonify({"error": str(error)}), 502
    return send_file(BytesIO(data), mimetype=attachment.content_type, download_name=attachment.original_name)


@app.post("/attachments/upload-image")
def upload_image():
    """图片只保存在笔记正文中，附件列表被忽略。

    前端已直接把图片转成 data URL，故这里不再创建任何附件记录。
    """
    return jsonify({"error": "图片已按 data URL 直接保存在笔记正文中，不再通过附件列表保存。"}), 410


@app.get("/health")
def health():
    return {"status": "ok"}


with app.app_context():
    restore_database()
    db.create_all()
    migrate_schema()


if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG", "0") == "1", host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
