import unittest
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from ebooklib import epub
from openpyxl import Workbook
from sqlalchemy import event

import app as worklog
from app import KnowledgeDoc, Note, app, Assignee, Schedule, _epub_chapters, db, mindmap_tree_data, Task


class DashboardParentTaskLabelTestCase(unittest.TestCase):
    def setUp(self):
        app.config['TESTING'] = True
        self.app_context = app.app_context()
        self.app_context.push()
        db.drop_all()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_dashboard_shows_root_task_for_nested_children(self):
        root = Task(title='洗车')
        child = Task(title='现场洗车排水', parent=root)
        grandchild = Task(title='办理经营资质', parent=child)
        db.session.add_all([root, child, grandchild])
        db.session.commit()

        client = app.test_client()
        response = client.get('/')

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('办理经营资质', html)
        self.assertIn('洗车', html)

    def test_task_list_and_assignee_routes_are_available(self):
        root = Task(title='洗车')
        child = Task(title='现场洗车排水', parent=root, status='doing')
        db.session.add_all([root, child])
        db.session.commit()

        client = app.test_client()
        response = client.get('/tasks/list')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('洗车', html)
        self.assertIn('现场洗车排水', html)

    def test_mindmap_create_returns_without_waiting_for_database_backup(self):
        with patch('app.schedule_database_sync') as schedule_sync, patch('app.sync_database') as sync_database:
            response = app.test_client().post(
                '/mindmap/tasks/create',
                json={'title': '快速创建', 'parent_id': ''},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['id'])
        schedule_sync.assert_called_once_with()
        sync_database.assert_not_called()

    def test_mindmap_tree_uses_batched_relationship_queries(self):
        root = Task(title='项目根节点')
        children = [Task(title=f'节点 {index}', parent=root) for index in range(30)]
        db.session.add_all([root, *children])
        db.session.commit()

        select_statements = []

        def count_selects(connection, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith('SELECT'):
                select_statements.append(statement)

        event.listen(db.engine, 'before_cursor_execute', count_selects)
        try:
            tree = mindmap_tree_data()
        finally:
            event.remove(db.engine, 'before_cursor_execute', count_selects)

        self.assertEqual(len(tree), 1)
        self.assertEqual(len(tree[0]['children']), 30)
        self.assertLessEqual(len(select_statements), 3)

    def test_epub_reader_extracts_spine_and_sanitizes_chapter_html(self):
        book = epub.EpubBook()
        book.set_identifier('reader-test')
        book.set_title('Reader Test')
        book.set_language('en')
        first = epub.EpubHtml(title='Chapter One', file_name='Text/one.xhtml', lang='en')
        first.content = b'<html><body><h1>First</h1><a href="two.xhtml">Next chapter</a><script>alert(1)</script></body></html>'
        second = epub.EpubHtml(title='Chapter Two', file_name='Text/two.xhtml', lang='en')
        second.content = b'<html><body><h1>Second</h1></body></html>'
        book.add_item(first)
        book.add_item(second)
        book.add_item(epub.EpubNcx())
        book.toc = (
            epub.Link('Text/one.xhtml', 'Chapter One', 'chapter-one'),
            epub.Link('Text/two.xhtml', 'Chapter Two', 'chapter-two'),
        )
        book.spine = ['ncx', first, second]

        with TemporaryDirectory() as directory:
            epub_path = Path(directory) / 'reader-test.epub'
            epub.write_epub(str(epub_path), book)
            result = _epub_chapters(
                type('AttachmentStub', (), {'id': 123, 'original_name': 'reader-test.epub'})(),
                epub_path.read_bytes(),
            )

        self.assertEqual(result['title'], 'Reader Test')
        self.assertEqual([chapter['title'] for chapter in result['chapters']], ['Chapter One', 'Chapter Two'])
        self.assertIn('data-epub-chapter="1"', result['chapters'][0]['content'])
        self.assertNotIn('<script', result['chapters'][0]['content'])

    def test_task_assignee_auto_propagates_to_ancestors(self):
        root = Task(title='洗车')
        child = Task(title='现场洗车排水', parent=root)
        grandchild = Task(title='办理经营资质', parent=child)
        assignee = Assignee(name='张三')
        db.session.add_all([root, child, grandchild, assignee])
        db.session.commit()

        grandchild.assignee_id = assignee.id
        db.session.commit()

        db.session.refresh(root)
        db.session.refresh(child)
        db.session.refresh(grandchild)

        self.assertEqual(root.assignee_id, assignee.id)
        self.assertEqual(child.assignee_id, assignee.id)
        self.assertEqual(grandchild.assignee_id, assignee.id)

    def test_parent_status_propagates_to_descendants(self):
        root = Task(title='洗车', status='todo')
        child = Task(title='现场洗车排水', parent=root, status='todo')
        grandchild = Task(title='办理经营资质', parent=child, status='todo')
        db.session.add_all([root, child, grandchild])
        db.session.commit()

        root.status = 'done'
        db.session.commit()

        db.session.refresh(root)
        db.session.refresh(child)
        db.session.refresh(grandchild)

        self.assertEqual(root.status, 'done')
        self.assertEqual(child.status, 'done')
        self.assertEqual(grandchild.status, 'done')

    def test_overdue_schedule_is_returned_by_reminder_api(self):
        overdue = Schedule(
            title='未及时处理的会议',
            start_at=datetime.now() - timedelta(minutes=20),
            end_at=datetime.now() - timedelta(minutes=5),
            completed=False,
            description='需跟进',
        )
        db.session.add(overdue)
        db.session.commit()

        client = app.test_client()
        response = client.get('/api/reminders')
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(any(item['title'] == '未及时处理的会议' for item in payload))

    def test_knowledge_upload_accepts_xlsx_files(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(['姓名', '部门'])
        sheet.append(['张三', '研发'])

        buffer = BytesIO()
        workbook.save(buffer)
        buffer.seek(0)

        client = app.test_client()
        response = client.post(
            '/knowledge/upload',
            data={'title': '人员名单', 'file': (buffer, '人员名单.xlsx')},
            content_type='multipart/form-data',
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn('已导入', response.get_data(as_text=True))
        self.assertIsNotNone(KnowledgeDoc.query.filter_by(title='人员名单.xlsx').first())

    def test_knowledge_upload_imports_multiple_files_using_filenames(self):
        client = app.test_client()
        response = client.post(
            '/knowledge/upload',
            data={
                'title': '这个标题不应用于上传文件',
                'files': [
                    (BytesIO(b'first document'), '制度一.txt'),
                    (BytesIO(b'second document'), '制度二.md'),
                ],
            },
            content_type='multipart/form-data',
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn('已导入 2 个文件', response.get_data(as_text=True))
        self.assertIsNotNone(KnowledgeDoc.query.filter_by(title='制度一.txt').first())
        self.assertIsNotNone(KnowledgeDoc.query.filter_by(title='制度二.md').first())

    def test_knowledge_upload_skips_duplicate_filenames(self):
        existing = KnowledgeDoc(title='已有制度', filename='Policy.txt', content='existing')
        db.session.add(existing)
        db.session.commit()

        response = app.test_client().post(
            '/knowledge/upload',
            data={
                'files': [
                    (BytesIO(b'duplicate'), 'policy.TXT'),
                    (BytesIO(b'new first'), 'new-policy.txt'),
                    (BytesIO(b'new duplicate'), 'NEW-POLICY.TXT'),
                ],
            },
            content_type='multipart/form-data',
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn('文件名已存在', response.get_data(as_text=True))
        self.assertIsNotNone(KnowledgeDoc.query.filter_by(filename='new-policy.txt').first())
        self.assertIsNone(KnowledgeDoc.query.filter_by(filename='NEW-POLICY.TXT').first())
        self.assertEqual(KnowledgeDoc.query.filter_by(filename='Policy.txt').count(), 1)

    def test_note_with_large_embedded_image_saves_without_waiting_for_oss(self):
        image_data = 'A' * (2 * 1024 * 1024)
        content = f'<p>note body</p><img src="data:image/png;base64,{image_data}">'
        with patch('app.schedule_database_sync') as schedule_sync:
            response = app.test_client().post(
                '/notes/new',
                data={'title': 'base64 note', 'content': content},
                follow_redirects=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn('笔记已保存', response.get_data(as_text=True))
        note = Note.query.filter_by(title='base64 note').first()
        self.assertIn('data:image/png;base64,', note.content)
        schedule_sync.assert_called_once_with()

    def test_attachment_upload_failure_returns_flash_instead_of_500(self):
        class FailingStorage:
            def put_object(self, **kwargs):
                raise ValueError('storage unavailable')

        with patch.object(worklog.storage, 'client', FailingStorage()):
            response = app.test_client().post(
                '/attachments/upload',
                data={'file': (BytesIO(b'file bytes'), 'failed.docx'), 'allow_duplicate': '1'},
                content_type='multipart/form-data',
                follow_redirects=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn('部分文件未上传', response.get_data(as_text=True))
        self.assertIn('failed.docx', response.get_data(as_text=True))
        self.assertIsNone(worklog.Attachment.query.filter_by(original_name='failed.docx').first())

    def test_file_manager_batch_upload_skips_duplicates_and_keeps_successes(self):
        class MemoryStorage:
            def __init__(self):
                self.objects = {}

            def put_object(self, Bucket, Key, Body, **kwargs):
                self.objects[Key] = Body

            def delete_object(self, Bucket, Key):
                self.objects.pop(Key, None)

        existing = worklog.Attachment(
            original_name='already.txt',
            object_key='attachments/already.txt',
            size=4,
        )
        db.session.add(existing)
        db.session.commit()
        memory_storage = MemoryStorage()

        with patch.object(worklog.storage, 'client', memory_storage), patch.object(worklog, 'schedule_database_sync') as schedule_sync:
            response = app.test_client().post(
                '/attachments/upload',
                data={
                    'allow_duplicate': '0',
                    'files': [
                        (BytesIO(b'first'), 'first.txt'),
                        (BytesIO(b'duplicate existing'), 'already.txt'),
                        (BytesIO(b'duplicate current batch'), 'FIRST.TXT'),
                        (BytesIO(b'second'), 'second.txt'),
                    ],
                },
                content_type='multipart/form-data',
                follow_redirects=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn('成功上传 2 个文件', response.get_data(as_text=True))
        self.assertIn('文件名重复，已跳过', response.get_data(as_text=True))
        self.assertEqual(worklog.Attachment.query.count(), 3)
        self.assertEqual(len(memory_storage.objects), 2)
        schedule_sync.assert_called_once_with()

    def test_file_upload_request_too_large_returns_actionable_message(self):
        previous_limit = app.config["MAX_CONTENT_LENGTH"]
        app.config["MAX_CONTENT_LENGTH"] = 64
        try:
            response = app.test_client().post(
                "/attachments/upload",
                data={
                    "allow_duplicate": "1",
                    "files": [(BytesIO(b"x" * 256), "large.bin")],
                },
                content_type="multipart/form-data",
                follow_redirects=True,
            )
        finally:
            app.config["MAX_CONTENT_LENGTH"] = previous_limit

        self.assertEqual(response.status_code, 200)
        self.assertIn("超过 100 MiB 上传上限", response.get_data(as_text=True))

    def test_uploaded_file_is_kept_when_backup_queue_fails(self):
        class MemoryStorage:
            def __init__(self):
                self.objects = {}

            def put_object(self, Bucket, Key, Body, **kwargs):
                self.objects[Key] = Body

            def delete_object(self, Bucket, Key):
                self.objects.pop(Key, None)

        memory_storage = MemoryStorage()
        with patch.object(worklog.storage, 'client', memory_storage), patch.object(
            worklog, 'schedule_database_sync', side_effect=RuntimeError('queue unavailable')
        ):
            response = app.test_client().post(
                "/attachments/upload",
                data={"allow_duplicate": "1", "files": [(BytesIO(b"file"), "saved.txt")]},
                content_type="multipart/form-data",
                follow_redirects=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("已上传 1 个文件，但数据库备份排队失败", response.get_data(as_text=True))
        self.assertIsNotNone(worklog.Attachment.query.filter_by(original_name="saved.txt").first())
        self.assertEqual(len(memory_storage.objects), 1)

    def test_schedule_form_updates_existing_schedule(self):
        schedule = Schedule(
            title='旧标题',
            start_at=datetime(2026, 9, 30, 9, 0),
            end_at=datetime(2026, 9, 30, 9, 30),
            description='旧备注',
        )
        db.session.add(schedule)
        db.session.commit()

        response = app.test_client().post(
            '/schedule',
            data={
                'schedule_id': str(schedule.id),
                'title': '新标题',
                'start_at': '2026-09-30T10:00',
                'end_at': '2026-09-30T10:45',
                'description': '新备注',
                'task_id': '',
                'week': '2026-09-28',
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Schedule.query.count(), 1)
        updated = db.session.get(Schedule, schedule.id)
        self.assertEqual(updated.title, '新标题')
        self.assertEqual(updated.start_at, datetime(2026, 9, 30, 10, 0))
        self.assertEqual(updated.end_at, datetime(2026, 9, 30, 10, 45))
        self.assertEqual(updated.description, '新备注')


if __name__ == '__main__':
    unittest.main()
