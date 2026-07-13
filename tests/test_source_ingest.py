import unittest

from openclaw_runtime.http_client import assert_public_url
from openclaw_runtime.source_ingest import google_doc_export_url, is_google_doc_url


class AssertPublicUrlTest(unittest.TestCase):
    def test_rejects_loopback_host(self) -> None:
        with self.assertRaises(ValueError):
            assert_public_url("http://127.0.0.1:6333/collections")

    def test_rejects_private_lan_host(self) -> None:
        with self.assertRaises(ValueError):
            assert_public_url("http://192.168.0.24:11434/api")

    def test_rejects_link_local_metadata_host(self) -> None:
        # 169.254.169.254 is the cloud-provider instance metadata address.
        with self.assertRaises(ValueError):
            assert_public_url("http://169.254.169.254/latest/meta-data/")

    def test_rejects_localhost_hostname(self) -> None:
        with self.assertRaises(ValueError):
            assert_public_url("http://localhost:8000/v1/models")

    def test_rejects_non_http_scheme(self) -> None:
        with self.assertRaises(ValueError):
            assert_public_url("file:///etc/passwd")

    def test_allows_public_host(self) -> None:
        assert_public_url("https://docs.google.com/document/d/abc/export?format=txt")


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
