"""Tests for orchestrator.py — codename generation, make_worktree, and Store.set/get."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import orchestrator


class TestCodename(unittest.TestCase):
    def test_format_is_adj_hyphen_noun(self):
        name = orchestrator._codename()
        parts = name.split("-")
        self.assertEqual(len(parts), 2, f"expected 'adj-noun', got {name!r}")
        adj, noun = parts
        self.assertIn(adj, orchestrator._CODENAME_ADJ,
                      f"{adj!r} not in _CODENAME_ADJ")
        self.assertIn(noun, orchestrator._CODENAME_NOUN,
                      f"{noun!r} not in _CODENAME_NOUN")

    def test_produces_varied_output(self):
        # Draws from 15*15=225 combinations; 20 trials have <1e-40 chance of all-same
        names = {orchestrator._codename() for _ in range(20)}
        self.assertGreater(len(names), 1)


class TestMakeWorktree(unittest.TestCase):
    def _mock_run(self, returncode=0):
        """Return a patchable stand-in for _run that always succeeds."""
        result = MagicMock()
        result.returncode = returncode
        result.stderr = ""
        result.stdout = ""
        return MagicMock(return_value=result)

    def test_returns_3_tuple_codename_in_branch_and_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_root = Path(tmpdir)
            with patch.object(orchestrator, "_run", self._mock_run()), \
                 patch.object(orchestrator, "ROOT", tmp_root):
                result = orchestrator.make_worktree(7)

        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 3)

        wt_path, branch, codename = result

        # codename has the right shape
        parts = codename.split("-")
        self.assertEqual(len(parts), 2)
        self.assertIn(parts[0], orchestrator._CODENAME_ADJ)
        self.assertIn(parts[1], orchestrator._CODENAME_NOUN)

        # codename appears in the branch name
        self.assertIn(codename, branch,
                      f"codename {codename!r} not found in branch {branch!r}")
        self.assertIn("issue-7", branch)

        # codename appears in the worktree path
        self.assertIn(codename, wt_path,
                      f"codename {codename!r} not found in wt_path {wt_path!r}")
        self.assertIn("issue-7", wt_path)


class TestStoreAgentName(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "pipeline.db"
        self.store = orchestrator.Store(db_path)
        self.store.upsert_issue(99, "Test issue", "Some body", 2)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_set_agent_name_is_readable_via_get(self):
        self.store.set(99, agent_name="brave-fox")
        row = self.store.get(99)
        self.assertIsNotNone(row)
        self.assertEqual(row["agent_name"], "brave-fox")

    def test_set_agent_name_can_be_overwritten(self):
        self.store.set(99, agent_name="calm-owl")
        self.store.set(99, agent_name="swift-wolf")
        self.assertEqual(self.store.get(99)["agent_name"], "swift-wolf")

    def test_get_missing_issue_returns_none(self):
        self.assertIsNone(self.store.get(12345))


if __name__ == "__main__":
    unittest.main()
