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
        response = client.get('/finance/cashflow')
        self.assertIn(response.status_code, (302, 401))

    def test_cashflow_page_exposes_rich_dad_game_metrics(self):
        client = app.test_client()
        login_response = client.post('/finance/login', data={'module': 'cashflow', 'password': 'changeme-cashflow'}, follow_redirects=False)
        self.assertIn(login_response.status_code, (200, 302))

        response = client.get('/finance/cashflow')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('收入', html)
        self.assertIn('支出', html)
        self.assertIn('资产', html)
        self.assertIn('负债', html)
        self.assertIn('手头现金', html)
        self.assertIn('被动收入', html)

    def test_cashflow_page_targets_passive_income_above_total_expense(self):
        client = app.test_client()
        client.post('/finance/login', data={'module': 'cashflow', 'password': 'changeme-cashflow'}, follow_redirects=False)

        response = client.get('/finance/cashflow')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('目标：被动收入 > 所有支出', html)

    def test_cashflow_page_summarizes_game_metrics_for_freedom(self):
        client = app.test_client()
        client.post('/finance/login', data={'module': 'cashflow', 'password': 'changeme-cashflow'}, follow_redirects=False)

        response = client.get('/finance/cashflow')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('主动收入', html)
        self.assertIn('被动收入', html)
        self.assertIn('财务自由指数', html)
        self.assertIn('资产净值', html)


if __name__ == '__main__':
    unittest.main()
