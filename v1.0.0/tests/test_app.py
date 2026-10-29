import unittest

import app


class AppRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()

    def test_homepage_renders(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Salin DataStore Roblox", response.data)
        self.assertIn(b"Halaman tutorial", response.data)

    def test_settings_include_author_and_version(self):
        response = self.client.get("/api/settings")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["author"], "Fari Noveri")
        self.assertEqual(response.json["version"], "1.0.0")

    def test_universe_requires_numeric_id(self):
        response = self.client.get("/api/universe/not-a-number")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["error"], "Universe ID harus berupa angka")


if __name__ == "__main__":
    unittest.main()
