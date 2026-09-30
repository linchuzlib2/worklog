import os
import unittest
from io import BytesIO
from unittest.mock import patch

from ebooklib import epub

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

        retry_response = self.client.put(
            f"/api/local-editor/{self.attachment.id}/content",
            data=updated_data,
            headers={**self.headers, "If-Match": revision},
        )
        self.assertEqual(retry_response.status_code, 200)
        self.assertTrue(retry_response.get_json()["unchanged"])

    def test_epub_can_be_read_online_and_opened_by_local_editor(self):
        book = epub.EpubBook()
        book.set_identifier("test-book")
        book.set_title("测试电子书")
        book.set_language("zh")
        chapter = epub.EpubHtml(title="第一章", file_name="chapter.xhtml", lang="zh")
        chapter.content = "<html><body><h1>正文标题</h1><p>阅读内容</p></body></html>"
        book.add_item(chapter)
        book.add_item(epub.EpubNcx())
        book.toc = (epub.Link("chapter.xhtml", "第一章", "chapter-one"),)
        book.spine = ["ncx", chapter]
        epub_buffer = BytesIO()
        epub.write_epub(epub_buffer, book)
        epub_data = epub_buffer.getvalue()

        attachment = worklog.Attachment(
            original_name="测试电子书.epub",
            object_key="attachments/test-book.epub",
            content_type="application/epub+zip",
            size=len(epub_data),
        )
        worklog.db.session.add(attachment)
        worklog.db.session.commit()
        self.storage.objects[attachment.object_key] = epub_data

        info = self.client.get(f"/api/local-editor/{attachment.id}", headers=self.headers)
        reader = self.client.get(f"/api/attachments/{attachment.id}/epub")
        page = self.client.get(f"/attachments/{attachment.id}/epub")

        self.assertEqual(info.status_code, 200)
        self.assertEqual(info.get_json()["filename"], "测试电子书.epub")
        self.assertEqual(reader.status_code, 200)
        self.assertEqual(reader.get_json()["title"], "测试电子书")
        self.assertEqual(reader.get_json()["chapters"][0]["title"], "第一章")
        self.assertEqual(page.status_code, 200)

    def test_knowledge_import_schedules_database_backup(self):
        with patch.object(worklog.ai, "embed", side_effect=worklog.ai.EmbedUnavailable("not configured")), patch.object(
            worklog, "schedule_database_sync"
        ) as schedule_sync, patch.object(worklog, "sync_database", side_effect=RuntimeError("OSS unavailable")) as sync_database:
            response = self.client.post(
                "/knowledge/upload",
                data={"title": "sync-test", "content": "document text"},
                follow_redirects=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("已导入", response.get_data(as_text=True))
        self.assertIsNotNone(worklog.KnowledgeDoc.query.filter_by(title="sync-test").first())
        schedule_sync.assert_called_once_with()
        sync_database.assert_not_called()

    def test_knowledge_delete_schedules_backup_without_oss_failure(self):
        document = worklog.KnowledgeDoc(title="delete-test", filename="", content="text")
        worklog.db.session.add(document)
        worklog.db.session.commit()

        with patch.object(worklog, "schedule_database_sync") as schedule_sync, patch.object(
            worklog, "sync_database", side_effect=RuntimeError("OSS unavailable")
        ) as sync_database:
            response = self.client.post(f"/knowledge/doc/{document.id}/delete", follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(worklog.db.session.get(worklog.KnowledgeDoc, document.id))
        schedule_sync.assert_called_once_with()
        sync_database.assert_not_called()

    def test_unexpected_save_error_redirects_with_message(self):
        def fail_upload():
            raise RuntimeError("unexpected database failure")

        with patch.dict(worklog.app.view_functions, {"knowledge_upload": fail_upload}):
            response = self.client.post("/knowledge/upload", follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("请求未能正常完成", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()