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


if __name__ == '__main__':
    unittest.main()
