"""Reasoner transcript/trace 双文件观测：信封 + flush 落盘。"""
from __future__ import annotations

import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from app.common import tracing as rag_trace


class TestReasonerArtifacts(unittest.TestCase):
    def test_flush_writes_transcript_and_trace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "reasoner"
            with unittest.mock.patch.dict(
                "os.environ",
                {
                    "REASONER_ARTIFACTS_ENABLED": "true",
                    "REASONER_ARTIFACTS_DIR": str(root),
                    "RAG_TRACE_ENABLED": "false",
                },
            ):
                with rag_trace.rag_request(
                    user_id=1,
                    session_id="sess_abcdef1234",
                    request_id="req_abcdef1234",
                    pipeline="askdata",
                ):
                    rag_trace.set_run_meta(
                        mode="react",
                        user_input="目前有多少退款单",
                        status="running",
                    )
                    rag_trace.append_phase(
                        "load_context",
                        node="load_context",
                        skills=["askdata"],
                    )
                    with rag_trace.span("agent", step=0) as tr:
                        tr["tool_calls"] = ["askdata_route"]
                    rag_trace.append_phase(
                        "react_agent",
                        node="react_agent",
                        step=0,
                        tool_calls=["askdata_route"],
                    )
                    rag_trace.append_phase(
                        "finalize",
                        node="finalize",
                        answer_preview="7",
                    )
                    rag_trace.set_run_meta(status="ok")

            day_dirs = list((root / "transcript").glob("*"))
            self.assertTrue(day_dirs, "transcript day dir missing")
            files = list(day_dirs[0].glob("*.json"))
            self.assertEqual(len(files), 1)
            stem = files[0].name

            self.assertFalse((root / "metrics").exists(), "metrics dir should not exist")

            for kind in ("transcript", "trace"):
                path = root / kind / day_dirs[0].name / stem
                self.assertTrue(path.is_file(), path)
                data = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(data["schema"], "reasoner.run_artifact.v1")
                self.assertEqual(data["kind"], kind)
                self.assertEqual(data["trace_id"], "req_abcdef1234")
                self.assertEqual(data["pipeline"], "askdata")
                self.assertEqual(data["mode"], "react")
                self.assertEqual(data["user_input"], "目前有多少退款单")
                self.assertEqual(data["status"], "ok")

            transcript = json.loads(
                (root / "transcript" / day_dirs[0].name / stem).read_text(
                    encoding="utf-8"
                )
            )
            phases = [p.get("phase") for p in transcript["payload"]]
            self.assertEqual(phases[0], "user")
            self.assertIn("load_context", phases)
            self.assertIn("react_agent", phases)
            self.assertIn("finalize", phases)
            self.assertGreaterEqual(transcript["steps"], 1)

            trace = json.loads(
                (root / "trace" / day_dirs[0].name / stem).read_text(encoding="utf-8")
            )
            tree = trace["payload"]
            self.assertIsInstance(tree, dict)
            self.assertEqual(tree.get("name"), "askdata.run")
            child_names = [c.get("name") for c in (tree.get("children") or [])]
            self.assertIn("agent", child_names)


if __name__ == "__main__":
    unittest.main()
