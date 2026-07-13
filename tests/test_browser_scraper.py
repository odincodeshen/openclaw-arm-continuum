import unittest

try:
    import openclaw_browser_scraper as scraper
    SCRAPER_IMPORT_ERROR = None
except ImportError as exc:
    scraper = None
    SCRAPER_IMPORT_ERROR = exc


@unittest.skipUnless(scraper, f"openclaw_browser_scraper unavailable: {SCRAPER_IMPORT_ERROR}")
class NormalizeUrlSsrfTest(unittest.TestCase):
    def test_rejects_loopback_host(self) -> None:
        with self.assertRaises(ValueError):
            scraper.normalize_url("http://127.0.0.1:6333/collections")

    def test_rejects_private_lan_host(self) -> None:
        with self.assertRaises(ValueError):
            scraper.normalize_url("http://192.168.0.24:11434/api")

    def test_rejects_link_local_metadata_host(self) -> None:
        with self.assertRaises(ValueError):
            scraper.normalize_url("http://169.254.169.254/latest/meta-data/")

    def test_rejects_localhost_hostname(self) -> None:
        with self.assertRaises(ValueError):
            scraper.normalize_url("http://localhost:8787/scrape")

    def test_allows_public_host(self) -> None:
        self.assertEqual(scraper.normalize_url("https://example.com/page"), "https://example.com/page")


if __name__ == "__main__":
    unittest.main()
