import importlib.util
from pathlib import Path
import unittest

MODULE_PATH = Path(__file__).parents[1] / "get-env-requirements.py"
SPEC = importlib.util.spec_from_file_location("env_requirements", MODULE_PATH)
env_requirements = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(env_requirements)


class EnvironmentRequirementsTest(unittest.TestCase):
    def test_mailgun_still_requires_its_api_key(self):
        result = env_requirements.get_env_requirements("mailgun")
        self.assertTrue(result["found"])
        self.assertEqual(result["required_env_vars"], ["MAILGUN_API_KEY"])
        self.assertEqual([field["key"] for field in result["env_vars"]],
                         ["MAILGUN_API_KEY", "MAILGUN_API_REGION"])
        self.assertTrue(result["env_vars"][0]["sensitive"])

    def test_remote_header_is_not_an_environment_requirement(self):
        result = env_requirements.get_env_requirements("remotes/exa_search")
        self.assertTrue(result["found"])
        self.assertEqual(result["required_env_vars"], ["EXA_TOOLS"])
        self.assertEqual([field["key"] for field in result["env_vars"]], ["EXA_TOOLS"])

    def test_oauth_only_entry_needs_no_environment(self):
        result = env_requirements.get_env_requirements("fabric-core")
        self.assertTrue(result["found"])
        self.assertEqual(result["env_vars"], [])


if __name__ == "__main__":
    unittest.main()
