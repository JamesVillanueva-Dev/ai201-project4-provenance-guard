import os
import tempfile
import unittest

os.environ["PROVENANCE_USE_GROQ"] = "false"

from app import AI_LABEL, HUMAN_LABEL, UNCERTAIN_LABEL, create_app, label_for_score, run_detection_pipeline


class ProvenanceGuardTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = create_app(
            {
                "TESTING": True,
                "DATABASE": f"{self.temp_dir.name}/test.db",
                "SUBMIT_LIMIT": "1000 per minute",
            }
        )
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_label_thresholds(self):
        self.assertEqual(label_for_score(0.82), ("likely_ai", "high_confidence_ai", AI_LABEL))
        self.assertEqual(label_for_score(0.18), ("likely_human", "high_confidence_human", HUMAN_LABEL))
        self.assertEqual(label_for_score(0.52), ("uncertain", "uncertain", UNCERTAIN_LABEL))

    def test_submit_writes_structured_log_entry(self):
        response = self.client.post(
            "/submit",
            json={
                "creator_id": "test-user",
                "text": (
                    "Artificial intelligence represents a transformative paradigm shift in modern society. "
                    "It is important to note that stakeholders across various sectors must collaborate "
                    "to ensure responsible deployment."
                ),
            },
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertIn("content_id", data)
        self.assertIn("confidence", data)
        self.assertGreaterEqual(len(data["signals"]), 3)

        log_response = self.client.get("/log")
        entries = log_response.get_json()["entries"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["event_type"], "classification")
        self.assertEqual(entries[0]["content_id"], data["content_id"])
        self.assertGreaterEqual(len(entries[0]["signals"]), 3)

    def test_appeal_updates_status_and_log(self):
        submit_response = self.client.post(
            "/submit",
            json={
                "creator_id": "creator-appeal",
                "text": (
                    "ok so i finally tried that new ramen place downtown and honestly? "
                    "underwhelming. the broth was fine but they put WAY too much sodium in it."
                ),
            },
        )
        content_id = submit_response.get_json()["content_id"]

        appeal_response = self.client.post(
            "/appeal",
            json={
                "content_id": content_id,
                "creator_reasoning": "I wrote this from personal experience after lunch.",
            },
        )
        self.assertEqual(appeal_response.status_code, 200)
        self.assertEqual(appeal_response.get_json()["status"], "under_review")

        log_entries = self.client.get("/log").get_json()["entries"]
        self.assertEqual(log_entries[0]["event_type"], "appeal")
        self.assertEqual(log_entries[0]["status"], "under_review")
        self.assertIn("personal experience", log_entries[0]["appeal_reasoning"])

    def test_scores_vary_between_polished_ai_and_casual_human_text(self):
        ai_text = (
            "Artificial intelligence represents a transformative paradigm shift in modern society. "
            "It is important to note that while the benefits of AI are numerous, it is equally "
            "essential to consider the ethical implications. Furthermore, stakeholders across "
            "various sectors must collaborate to ensure responsible deployment."
        )
        human_text = (
            "ok so i finally tried that new ramen place downtown and honestly? underwhelming. "
            "the broth was fine but they put WAY too much sodium in it and i was thirsty for "
            "like three hours after. my friend got the spicy version and said it was better."
        )
        ai_score = run_detection_pipeline(ai_text)["ai_likelihood"]
        human_score = run_detection_pipeline(human_text)["ai_likelihood"]
        self.assertGreater(ai_score, human_score + 0.20)


if __name__ == "__main__":
    unittest.main()
