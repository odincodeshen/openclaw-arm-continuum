import unittest

from openclaw_runtime.source_ingest import google_doc_export_url, is_google_doc_url


class SourceIngestTest(unittest.TestCase):
    def test_google_doc_edit_url_exports_text(self):
        url = "https://docs.google.com/document/d/108rpxLtCquGOzSPI79DHWSX4HcOLNDBdetr9zbocOts/edit?usp=drivesdk"
        self.assertTrue(is_google_doc_url(url))
        self.assertEqual(
            google_doc_export_url(url),
            "https://docs.google.com/document/d/108rpxLtCquGOzSPI79DHWSX4HcOLNDBdetr9zbocOts/export?format=txt",
        )

    def test_google_doc_user_scoped_url_exports_text(self):
        url = "https://docs.google.com/document/u/0/d/abc123DEF/edit"
        self.assertEqual(
            google_doc_export_url(url),
            "https://docs.google.com/document/d/abc123DEF/export?format=txt",
        )

    def test_published_google_doc_exports_text(self):
        url = "https://docs.google.com/document/d/e/2PACX-1vR-public-id/pub"
        self.assertEqual(
            google_doc_export_url(url),
            "https://docs.google.com/document/d/e/2PACX-1vR-public-id/pub?output=txt",
        )


if __name__ == "__main__":
    unittest.main()
