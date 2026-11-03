from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.modules.studio import derive
from app.modules.studio.derive import derive_documentation

ALLOWED_TAGS = {"math", "mrow", "mi", "mn", "mo", "mtext", "msup", "msub", "mfrac", "msqrt"}

DARCY = '''
"""Pipe loss."""
import math

G = 9.81
NEG = -2.5e-3

def friction_factor(re_number, rel_rough):
    """Darcy friction factor."""
    if re_number < 2300:
        f_darcy = 64 / re_number
    else:
        f_darcy = 0.25 / math.log10(rel_rough / 3.7 + 5.74 / re_number ** 0.9) ** 2
    return f_darcy

def evaluate(rho, mu, v, d, length, rel_rough):
    """Pressure loss."""
    re_number = rho * v * d / mu
    f_darcy = friction_factor(re_number, rel_rough)
    delta_p = f_darcy * (length / d) * rho * v ** 2 / 2
    return {"delta_p": delta_p, "re": re_number}
'''


def walk(tree: Any):
    yield tree
    for child in tree.get("children", []):
        yield from walk(child)


def texts(tree: Any) -> list[str]:
    return [n["text"] for n in walk(tree) if "text" in n]


def all_trees(doc: dict[str, Any]):
    def steps_trees(steps: list[dict[str, Any]]):
        for s in steps:
            for key in ("target", "expr", "condition"):
                if s.get(key):
                    yield s[key]
            for case in s.get("cases", []):
                for key in ("condition", "expr"):
                    if case.get(key):
                        yield case[key]
            for branch in s.get("branches", []):
                if branch["condition"]:
                    yield branch["condition"]
                yield from steps_trees(branch["steps"])
    for f in doc["functions"]:
        yield from steps_trees(f["steps"])
    yield from steps_trees(doc["module_constants"])


def test_darcy_weisbach_structure() -> None:
    doc = derive_documentation(DARCY)
    assert doc["status"] == "ok"
    assert doc["unrendered_count"] == 0
    assert [f["name"] for f in doc["functions"]] == ["evaluate", "friction_factor"]
    evaluate, helper = doc["functions"]
    assert evaluate["args"] == ["rho", "mu", "v", "d", "length", "rel_rough"]
    assert evaluate["docstring"] == "Pressure loss."
    eqs = evaluate["steps"]
    assert [s["kind"] for s in eqs] == ["equation"] * 4
    assert texts(eqs[0]["target"]) == ["re", "number"]
    assert "ρ" in texts(eqs[0]["expr"]) and "μ" in texts(eqs[0]["expr"])
    assert any(n["tag"] == "mfrac" for n in walk(eqs[0]["expr"]))
    assert texts(eqs[2]["target"]) == ["δ", "p"]
    assert [n["tag"] for n in walk(eqs[2]["target"])] == ["math", "mrow", "msub", "mi", "mi"]
    assert any(n["tag"] == "msup" for n in walk(eqs[2]["expr"]))
    assert texts(eqs[3]["target"]) == ["re"]  # the identity "delta_p": delta_p is not repeated
    # helper: if/else assigning one target -> piecewise
    cond = helper["steps"][0]
    assert cond["kind"] == "conditional" and cond["form"] == "piecewise"
    assert texts(cond["target"]) == ["f", "darcy"]
    assert [c["condition"] is None for c in cond["cases"]] == [False, True]
    assert "<" in texts(cond["cases"][0]["condition"])
    assert any(n["tag"] == "msub" for n in walk(cond["cases"][1]["expr"]))
    assert helper["steps"][1]["kind"] == "equation"
    assert doc["module_constants"][0]["kind"] == "equation"
    assert texts(doc["module_constants"][0]["expr"]) == ["9.81"]
    assert texts(doc["module_constants"][1]["expr"]) == ["−", "0.0025"]
    for tree in all_trees(doc):
        assert tree["tag"] == "math"
        assert {n["tag"] for n in walk(tree)} <= ALLOWED_TAGS


def test_branch_form_when_not_single_target() -> None:
    src = (
        "def evaluate(x):\n"
        "    if x > 1 and not x == 3:\n"
        "        y = x\n"
        "        z = y + 1\n"
        "    elif x > 0:\n"
        "        y = -x\n"
        "    return y\n"
    )
    doc = derive_documentation(src)
    cond = doc["functions"][0]["steps"][0]
    assert cond["form"] == "branches"
    assert [b["label"] for b in cond["branches"]] == ["if", "elif"]
    assert len(cond["branches"][0]["steps"]) == 2
    assert "and" in texts(cond["branches"][0]["condition"])


def test_ifexp_and_math_functions() -> None:
    src = (
        "import numpy as np\n"
        "def evaluate(x):\n"
        "    a = np.sqrt(x) + math.exp(-x) + abs(x) + min(x, 2) + x // 2\n"
        "    return 1 if x > 0 else 2\n"
    )
    doc = derive_documentation(src)
    steps = doc["functions"][0]["steps"]
    tags = {n["tag"] for n in walk(steps[0]["expr"])}
    assert "msqrt" in tags and "msup" in tags
    assert steps[1]["form"] == "piecewise"


def test_side_effects_are_not_executed(tmp_path: Path) -> None:
    marker = tmp_path / "x"
    src = (
        f"open({str(marker)!r}, 'w').write('boom')\n"
        "import os\n"
        "os.system('touch /tmp/should_not_exist_k188')\n"
        "raise RuntimeError('import time')\n"
        "def evaluate(a):\n"
        "    return a * 2\n"
    )
    doc = derive_documentation(src)
    assert doc["status"] == "ok"
    assert not marker.exists()
    assert not Path("/tmp/should_not_exist_k188").exists()
    assert doc["functions"][0]["steps"][0]["kind"] == "equation"


def test_unsupported_constructs_are_labelled() -> None:
    src = (
        "def evaluate(items):\n"
        "    total = 0\n"
        "    for i in items:\n"
        "        total += i\n"
        "    while total > 10:\n"
        "        total -= 1\n"
        "    try:\n"
        "        q = 1 / total\n"
        "    except ZeroDivisionError:\n"
        "        q = 0\n"
        "    w = [k for k in items]\n"
        "    return total\n"
    )
    doc = derive_documentation(src)
    steps = doc["functions"][0]["steps"]
    codes = [s for s in steps if s["kind"] == "code"]
    assert [s["line"] for s in codes] == [3, 5, 7, 11]
    assert codes[0]["text"].startswith("for i in items:") and "total += i" in codes[0]["text"]
    assert all(s["reason"] for s in codes)
    assert "For" in codes[0]["reason"]
    assert doc["unrendered_count"] == 4
    assert steps[-1]["kind"] == "equation"


def test_nested_code_counts_and_unmapped_call() -> None:
    src = (
        "def evaluate(x):\n"
        "    if x > 0:\n"
        "        y = foo.bar(x)\n"
        "        z = x\n"
        "    else:\n"
        "        y = 0\n"
        "        z = 1\n"
        "    return y\n"
    )
    doc = derive_documentation(src)
    cond = doc["functions"][0]["steps"][0]
    assert cond["form"] == "branches"
    assert cond["branches"][0]["steps"][0]["kind"] == "code"
    assert "foo.bar" in cond["branches"][0]["steps"][0]["reason"]
    assert doc["unrendered_count"] == 1


def test_syntax_error_and_missing_entry() -> None:
    bad = derive_documentation("def evaluate(:\n    pass\n")
    assert bad["status"] == "syntax_error"
    assert bad["error"]["line"] == 1 and bad["error"]["message"]
    assert bad["functions"] == []
    missing = derive_documentation("def other(x):\n    return x\n")
    assert missing["status"] == "entry_missing"
    assert derive_documentation("def other(x):\n    return x\n", entry="other")["status"] == "ok"


def test_deterministic_and_json_serialisable() -> None:
    first = derive_documentation(DARCY)
    second = derive_documentation(DARCY)
    assert first == second
    assert json.dumps(first, sort_keys=False) == json.dumps(second, sort_keys=False)
    assert len(first["source_sha256"]) == 64


def test_deep_nesting_does_not_crash() -> None:
    src = "def evaluate(x):\n    return " + "(" * 50 + "x" + "+1)" * 50 + "\n"
    assert derive_documentation(src)["status"] in {"ok", "syntax_error"}


def test_derive_module_has_no_dynamic_execution() -> None:
    text = Path(derive.__file__).read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    for pattern in (r"\bexec\(", r"\beval\(", r"runpy", r"importlib", r"__import__", r"literal_eval"):
        assert not re.search(pattern, code.split('"""', 2)[2]), pattern
