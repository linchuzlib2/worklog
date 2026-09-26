import unittest

from app import app, db, Task


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

    def test_dashboard_shows_parent_task_for_child_tasks(self):
        parent = Task(title='主任务')
        child = Task(title='子任务', parent=parent)
        db.session.add_all([parent, child])
        db.session.commit()

        client = app.test_client()
        response = client.get('/')

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('子任务', html)
        self.assertTrue('所属主任务' in html or '所属' in html)


if __name__ == '__main__':
    unittest.main()
