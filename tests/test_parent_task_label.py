import unittest
from datetime import datetime, timedelta
from io import BytesIO

from openpyxl import Workbook

from app import KnowledgeDoc, app, Assignee, Schedule, db, Task


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


if __name__ == '__main__':
    unittest.main()
