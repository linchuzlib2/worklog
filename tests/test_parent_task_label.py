import unittest

from app import app, Assignee, db, Task


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

    def test_finance_modules_require_password(self):
        client = app.test_client()
        response = client.get('/finance/accounting')
        self.assertIn(response.status_code, (302, 401))

    def test_accounting_view_is_available_after_login(self):
        client = app.test_client()
        login_response = client.post('/finance/login', data={'module': 'accounting', 'password': 'changeme-accounting'}, follow_redirects=False)
        self.assertIn(login_response.status_code, (200, 302))

        response = client.get('/finance/accounting')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('个人记账', html)


if __name__ == '__main__':
    unittest.main()
