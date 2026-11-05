"""Contract tests for the offline semantic-judgment case set."""

import importlib.util
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "eval/local/semantic_judgment/semantic_eval.py"
SPEC = importlib.util.spec_from_file_location("semantic_eval", MODULE_PATH)
assert SPEC and SPEC.loader
semantic_eval = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(semantic_eval)
CASES = semantic_eval.load_cases()


def reply(case, **changes):
    answer = {**case["answer"], **changes}
    return json.dumps(answer)


def numeric_inputs(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from numeric_inputs(item)
    elif isinstance(value, list):
        for item in value:
            yield from numeric_inputs(item)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        yield value


def test_schema_and_category_minimums():
    data = json.loads(semantic_eval.CASES_PATH.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    assert len(CASES) >= 30
    counts = Counter(case["category"] for case in CASES)
    for category, minimum in {
        "validity_domain": 8,
        "insufficient_data": 8,
        "contradiction": 8,
        "control": 6,
    }.items():
        assert counts[category] >= minimum
    assert set(counts) == {"validity_domain", "insufficient_data", "contradiction", "control"}
    assert len({case["id"] for case in CASES}) == len(CASES)
    for case in CASES:
        assert set(case) == {
            "id", "category", "title", "context", "question", "options", "answer", "check"
        }
        assert 3 <= len(case["options"]) <= 6
        labels = [option["label"] for option in case["options"]]
        assert len(labels) == len(set(labels))
        assert set(case["answer"]) == {"verdict", "options", "missing"}
        assert case["answer"]["verdict"] in semantic_eval.VERDICTS
        assert set(case["answer"]["options"] + case["answer"]["missing"]) <= set(labels)
        if case["category"] == "insufficient_data":
            assert case["answer"]["missing"]
            assert any(option.get("distractor") for option in case["options"])
            assert {option["label"] for option in case["options"] if not option["distractor"]} == set(
                case["answer"]["missing"]
            )


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_checks_and_context_numbers(case):
    check = case["check"]
    assert set(check) == {"function", "inputs", "comparison", "threshold", "expected"}
    assert check["function"] in semantic_eval.CHECK_FUNCTIONS
    assert semantic_eval.check_case(case)
    for number in numeric_inputs(check["inputs"]):
        pattern = rf"(?<![\d.]){re.escape(str(number))}(?![\d.])"
        assert re.search(pattern, case["context"]), (case["id"], number)
    if case["category"] == "insufficient_data":
        assert check["function"] == "required_data_present"
        missing_names = set(check["inputs"]["required"]) - set(check["inputs"]["known"])
        assert missing_names
        assert all(name in case["context"] for name in missing_names)


def test_prompts_do_not_supply_the_judgment():
    for case in CASES:
        context = case["context"].lower()
        if case["category"] in ("validity_domain", "control"):
            assert "criterion" not in context
            assert "requirement" not in context
            assert "validated only" not in context
        elif case["category"] == "insufficient_data":
            assert "not supplied" not in context
            assert "not given" not in context
            assert "missing" not in context
        else:
            assert "residual" not in context
            assert "exceeds the tolerance" not in context
            assert "larger than the" not in context


def test_critical_and_scattering_examples():
    cases = {case["id"]: case for case in CASES}
    for case_id in ("vd09", "co07"):
        assert cases[case_id]["check"]["function"] == "reduced_temperature"
    for case_id in ("vd10", "co08"):
        assert cases[case_id]["check"]["function"] == "optical_depth"
    assert semantic_eval.reduced_temperature(310, 304.13) == pytest.approx(1.0193, abs=0.0001)
    assert semantic_eval.reduced_temperature(450, 304.13) == pytest.approx(1.4796, abs=0.0001)
    assert semantic_eval.optical_depth(20, 0.2) == pytest.approx(4)
    assert semantic_eval.optical_depth(0.01, 1) == pytest.approx(0.01)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_known_answer_and_wrong_verdict(case):
    assert semantic_eval.grade(case, reply(case))["score"] == 1
    wrong = next(v for v in semantic_eval.VERDICTS if v != case["answer"]["verdict"])
    assert semantic_eval.grade(case, reply(case, verdict=wrong))["score"] == 0
    result = semantic_eval.grade(case, "not JSON")
    assert result["score"] == 0 and result["parse_error"] is True


@pytest.mark.parametrize("category", ["validity_domain", "contradiction"])
def test_wrong_issue_option(category):
    case = next(case for case in CASES if case["category"] == category)
    wrong = next(
        option["label"] for option in case["options"]
        if option["label"] not in case["answer"]["options"]
    )
    result = semantic_eval.grade(case, reply(case, options=[wrong]))
    assert result["score"] == 0 and result["options_ok"] is False


def test_missing_required_and_included_distractor():
    case = next(case for case in CASES if case["category"] == "insufficient_data")
    required = case["answer"]["missing"]
    distractor = next(
        option["label"] for option in case["options"] if option["distractor"]
    )
    assert semantic_eval.grade(case, reply(case, missing=required[:-1]))["score"] == 0
    assert semantic_eval.grade(case, reply(case, missing=required + [distractor]))["score"] == 0


def test_control_rejects_option():
    case = next(case for case in CASES if case["category"] == "control")
    assert semantic_eval.grade(case, reply(case, options=["A"]))["score"] == 0


def test_thinking_and_fenced_json():
    case = CASES[0]
    bare = reply(case)
    expected = semantic_eval.grade(case, bare)
    assert semantic_eval.grade(case, f"I checked the domain.\n{bare}") == expected
    assert semantic_eval.grade(case, f"```json\n{bare}\n```") == expected


def test_export_is_complete_and_deterministic(tmp_path):
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    for path in (first, second):
        subprocess.run(
            [sys.executable, str(MODULE_PATH), "--emit-prompts", str(path)],
            check=True,
            cwd=ROOT,
        )
    assert first.read_bytes() == second.read_bytes()
    entries = json.loads(first.read_text(encoding="utf-8"))
    assert len(entries) == len(CASES)
    assert {entry["name"] for entry in entries} == {case["id"] for case in CASES}
    for case, entry in zip(CASES, entries, strict=True):
        assert entry["answer"] == case["answer"]
        assert '{"verdict": "...", "options": [...], "missing": [...]}' in entry["prompt"]
        for option in case["options"]:
            assert f'{option["label"]}:' in entry["prompt"]


def test_grade_cli_totals(tmp_path):
    replies_path = tmp_path / "replies.json"
    replies_path.write_text(
        json.dumps({case["id"]: reply(case) for case in CASES}), encoding="utf-8"
    )
    process = subprocess.run(
        [sys.executable, str(MODULE_PATH), "--grade", str(replies_path)],
        check=True,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    result = json.loads(process.stdout)
    assert sum(item["score"] for item in result["categories"].values()) == len(CASES)
    assert sum(item["count"] for item in result["categories"].values()) == len(CASES)


def test_grade_cli_consolidates_attempts_and_ties(tmp_path):
    case = CASES[0]
    correct = reply(case)
    wrong = reply(case, verdict=next(v for v in semantic_eval.VERDICTS
                                     if v != case["answer"]["verdict"]))
    replies_path = tmp_path / "replies.json"
    replies_path.write_text(json.dumps({case["id"]: [correct, wrong, correct]}))
    process = subprocess.run(
        [sys.executable, str(MODULE_PATH), "--grade", str(replies_path)],
        check=True, capture_output=True, text=True, cwd=ROOT,
    )
    row = json.loads(process.stdout)["cases"][case["id"]]
    assert [item["score"] for item in row["attempts"]] == [1, 0, 1]
    assert row["majority"]["verdict"] == case["answer"]["verdict"]
    assert row["majority"]["score"] == row["score"] == 1
    assert row["attempts_scored"] == 2
    assert row["disagreement"] is True
    tied = semantic_eval.grade_replies([case], {case["id"]: [correct, wrong]})["cases"][case["id"]]
    assert tied["disagreement"] is True
    assert tied["attempts_scored"] == 1
