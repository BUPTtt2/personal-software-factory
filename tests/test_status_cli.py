import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from contextlib import redirect_stdout
from pathlib import Path

from observer.cli import main
from observer.event_store import EventStore


class StatusCliTests(unittest.TestCase):
    def test_status_json_has_stable_safe_shape(self):
        root = Path(tempfile.mkdtemp())
        (root / "config").mkdir()
        (root / "config/projects.yaml").write_text(json.dumps({"projects": [
            {"id": "demo", "displayName": "Demo", "roots": ["/tmp/demo"], "gitRemotes": [], "enabled": True}
        ]}), encoding="utf-8")
        EventStore(root / "data/factory.sqlite", root / "buffer").initialize()
        output = io.StringIO()
        with redirect_stdout(output):
            result = main(["status", "--root", str(root), "--json"])
        self.assertEqual(result, 0)
        value = json.loads(output.getvalue())
        self.assertEqual(set(value), {
            "schemaVersion", "hookInstalled", "hookTrust", "databaseReady",
            "registeredProjects", "eventCount", "bufferedEventCount",
            "lastEventAt", "warnings",
        })
        self.assertEqual(value["registeredProjects"], 1)
        self.assertEqual(value["eventCount"], 0)
        self.assertEqual(value["hookTrust"], "unknown_manual_check_required")

    def test_status_output_never_prints_stored_text(self):
        root = Path(tempfile.mkdtemp())
        (root / "config").mkdir()
        (root / "config/projects.yaml").write_text('{"projects":[]}', encoding="utf-8")
        store = EventStore(root / "data/factory.sqlite", root / "buffer")
        store.initialize()
        with closing(sqlite3.connect(store.db_path)) as connection:
            with connection:
                connection.execute("INSERT INTO projects VALUES('p','a','a')")
                connection.execute("INSERT INTO threads(thread_id,project_id,first_seen_at,last_seen_at,status) VALUES('t','p','a','a','working')")
                connection.execute("INSERT INTO turns(thread_id,turn_id,status,prompt_summary,assistant_claim_summary) VALUES('t','u','working','PRIVATE_PROMPT','PRIVATE_CLAIM')")
        output = io.StringIO()
        with redirect_stdout(output):
            main(["status", "--root", str(root)])
        self.assertNotIn("PRIVATE_PROMPT", output.getvalue())
        self.assertNotIn("PRIVATE_CLAIM", output.getvalue())


if __name__ == "__main__":
    unittest.main()
