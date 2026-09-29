import os
import unittest
from io import BytesIO
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite://"

import app as worklog


class MemoryObjectStorage:
    def __init__(self):
        self.objects = {}

    def get_object(self, Bucket, Key):
        return {"Body": BytesIO(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, **kwargs):
        self.objects[Key] = Body


class LocalEditorApiTestCase(unittest.TestCase):
    token = "test-local-editor-token-0123456789abcdef"

    def setUp(self):
        self.token_patch = patch.dict(os.environ, {"LOCAL_EDITOR_TOKEN": self.token})
        self.token_patch.start()
        self.original_storage_client = worklog.storage.client
        self.storage = MemoryObjectStorage()
        self.original_attachment_data = b"original document"
        self.storage.objects["attachments/test.docx"] = self.original_attachment_data
        worklog.storage.client = self.storage

        self.app_context = worklog.app.app_context()
        self.app_context.push()
        worklog.app.config["TESTING"] = True
        worklog.db.drop_all()
        worklog.db.create_all()
        self.attachment = worklog.Attachment(
            original_name="test.docx",
            object_key="attachments/test.docx",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            size=len(self.original_attachment_data),
        )
        worklog.db.session.add(self.attachment)
        worklog.db.session.commit()
        self.client = worklog.app.test_client()
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def tearDown(self):
        worklog.db.session.remove()
        worklog.db.drop_all()
        worklog.app.config["TESTING"] = False
        self.app_context.pop()
        worklog.storage.client = self.original_storage_client
        self.token_patch.stop()

    def test_local_editor_rejects_requests_without_token(self):
        response = self.client.get(f"/api/local-editor/{self.attachment.id}")

        self.assertEqual(response.status_code, 401)

    def test_local_editor_refuses_stale_revision(self):
        response = self.client.put(
            f"/api/local-editor/{self.attachment.id}/content",
            data=b"stale edit",
            headers={**self.headers, "If-Match": "wrong-revision"},
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.storage.objects["attachments/test.docx"], self.original_attachment_data)

    def test_local_editor_uploads_matching_revision(self):
        download = self.client.get(
            f"/api/local-editor/{self.attachment.id}/content",
            headers=self.headers,
        )
        revision = download.headers["ETag"]
        updated_data = b"updated document bytes"

        response = self.client.put(
            f"/api/local-editor/{self.attachment.id}/content",
            data=updated_data,
            headers={**self.headers, "If-Match": revision},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.storage.objects["attachments/test.docx"], updated_data)
        self.assertEqual(worklog.db.session.get(worklog.Attachment, self.attachment.id).size, len(updated_data))

    def test_knowledge_import_survives_database_sync_failure(self):
        with patch.object(worklog.ai, "embed", side_effect=worklog.ai.EmbedUnavailable("not configured")), patch.object(
            worklog, "sync_database", side_effect=RuntimeError("OSS unavailable")
        ):
            response = self.client.post(
                "/knowledge/upload",
                data={"title": "sync-test", "content": "document text"},
                follow_redirects=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("数据库备份同步失败", response.get_data(as_text=True))
        self.assertIsNotNone(worklog.KnowledgeDoc.query.filter_by(title="sync-test").first())


if __name__ == "__main__":
    unittest.main()