import unittest
from unittest.mock import patch

from codex_wake.adapters import PENDING_EXIT_CODE, check


class AdapterTest(unittest.TestCase):
    @staticmethod
    def request(config):
        return {
            "protocol_version": 1,
            "registration_id": "registration-1",
            "source": "test",
            "subject": "subject-1",
            "config": config,
        }

    def test_github_actions_pending_and_terminal(self):
        config = {"repository": "owner/repo", "run_id": 123}
        with (
            patch("codex_wake.adapters._executable", return_value="gh"),
            patch("codex_wake.adapters._command") as command,
        ):
            command.return_value = {
                "status": "in_progress",
                "conclusion": "",
                "url": "https://example/run/123",
            }
            self.assertEqual(
                check("github-actions", self.request(config)), {"state": "pending"}
            )
            command.return_value = {
                "status": "completed",
                "conclusion": "success",
                "url": "https://example/run/123",
            }
            event = check("github-actions", self.request(config))
        self.assertEqual(
            command.call_args.args[0][:6],
            ["gh", "run", "view", "123", "--repo", "owner/repo"],
        )
        self.assertEqual(event["state"], "terminal")
        self.assertIn("status=success", event["message"])

    def test_gitlab_pipeline_terminal(self):
        with (
            patch("codex_wake.adapters._executable", return_value="glab"),
            patch(
                "codex_wake.adapters._command",
                return_value={
                    "status": "failed",
                    "web_url": "https://example/pipelines/456",
                },
            ) as command,
        ):
            event = check(
                "gitlab-ci",
                self.request(
                    {"project": "group/project", "id": 456, "kind": "pipeline"}
                ),
            )
        self.assertEqual(
            command.call_args.args[0],
            ["glab", "api", "projects/group%2Fproject/pipelines/456"],
        )
        self.assertEqual(event["state"], "terminal")
        self.assertIn("status=failed", event["message"])

    def test_gitlab_manual_job_and_pipeline_wait_until_completion(self):
        for kind in ("job", "pipeline"):
            with (
                self.subTest(kind=kind),
                patch("codex_wake.adapters._executable", return_value="glab"),
                patch("codex_wake.adapters._command") as command,
            ):
                request = self.request(
                    {"project": "group/project", "id": 456, "kind": kind}
                )
                for status in ("manual", "pending", "running"):
                    command.return_value = {
                        "status": status,
                        "web_url": "https://example/job/456",
                    }
                    self.assertEqual(check("gitlab-ci", request), {"state": "pending"})
                command.return_value["status"] = "success"
                event = check("gitlab-ci", request)
                self.assertEqual(event["state"], "terminal")
                self.assertIn("status=success", event["message"])

    def test_status_command_uses_tempfail_for_pending(self):
        with patch("codex_wake.adapters.subprocess.run") as run:
            run.return_value.returncode = PENDING_EXIT_CODE
            run.return_value.stdout = ""
            run.return_value.stderr = ""
            self.assertEqual(
                check("status-command", self.request({"command": ["probe"]})),
                {"state": "pending"},
            )
            run.return_value.returncode = 0
            run.return_value.stdout = "external job succeeded\n"
            event = check("status-command", self.request({"command": ["probe"]}))
            self.assertEqual(event["state"], "terminal")
            self.assertIn("external job succeeded", event["message"])


if __name__ == "__main__":
    unittest.main()
