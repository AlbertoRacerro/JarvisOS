from __future__ import annotations

from pathlib import Path

from app.modules.runner import local_python


def test_caller_lock_keeps_liveness_after_helper_dies_post_child(tmp_path: Path) -> None:
    working_dir = tmp_path / "run"
    output_dir = working_dir / "output"
    working_dir.mkdir()
    output_dir.mkdir()
    input_file = working_dir / "input.json"
    input_file.write_text("{}", encoding="utf-8")
    script_path = tmp_path / "quick_child.py"
    script_path.write_text("print('done')\n", encoding="utf-8")

    with local_python.prepare_execution_owner(working_dir):
        result = local_python.execute_python_script(
            script_path=script_path,
            input_file=input_file,
            output_dir=output_dir,
            working_dir=working_dir,
            timeout_seconds=5,
            max_stdout_bytes=1024,
            max_stderr_bytes=1024,
        )
        assert result.return_code == 0

        session = local_python._sessions[working_dir.resolve()]
        process = session.process
        assert process is not None
        process.kill()
        process.wait(timeout=5)

        assert local_python.execution_ownership_state(working_dir) == "live"

    assert local_python.execution_ownership_state(working_dir) == "gone"
