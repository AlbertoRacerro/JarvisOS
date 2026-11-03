#!/usr/bin/env python3
"""Generate the bounded frontend contract selected by spec 133.

`ParameterRead` is the source authority. Unsupported annotation shapes fail
closed instead of silently widening generated TypeScript.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
import tempfile
import types
import typing
from pathlib import Path
from typing import Literal, Union, get_args, get_origin

from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.modules.engineering.operator_models import (  # noqa: E402
    CapabilityRead,
    EvaluatorRead,
)
from app.modules.modeling.models import ParameterRead  # noqa: E402

SOURCE_LABEL = "backend/app/modules/modeling/models.py::ParameterRead"
TARGET = REPO_ROOT / "frontend" / "src" / "api" / "generated" / "modeling.ts"
ENGINEERING_TARGET = REPO_ROOT / "frontend" / "src" / "api" / "generated" / "engineering.ts"
EDITOR_TARGET = REPO_ROOT / "frontend" / "src" / "api" / "generated" / "dwsimEditor.ts"
HEADER = (
    "// GENERATED FILE — DO NOT EDIT.\n"
    f"// Source: {SOURCE_LABEL}\n"
    "// Regenerate with: python scripts/generate_frontend_contracts.py\n\n"
)
EDITOR_HEADER = (
    "// GENERATED FILE — DO NOT EDIT.\n"
    "// Source: backend/app/modules/process_stack/editor_models.py\n"
    "// Regenerate with: python scripts/generate_frontend_contracts.py\n\n"
)


class ContractGenerationError(ValueError):
    """Raised when the selected backend contract cannot be mapped safely."""


def _literal(value: object) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return repr(value)
    raise ContractGenerationError(f"unsupported Literal value: {value!r}")


def _typescript_type(annotation: object) -> str:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation.__name__
    if annotation is str:
        return "string"
    if annotation in {int, float}:
        return "number"
    if annotation is bool:
        return "boolean"
    if annotation is object:
        return "unknown"

    origin = get_origin(annotation)
    if origin is list:
        args = get_args(annotation)
        if len(args) != 1:
            raise ContractGenerationError(f"unsupported list annotation: {annotation!r}")
        item = _typescript_type(args[0])
        return f"({item})[]" if " | " in item else f"{item}[]"
    if origin is dict:
        args = get_args(annotation)
        if len(args) != 2 or args[0] is not str:
            raise ContractGenerationError(f"unsupported mapping annotation: {annotation!r}")
        return f"Record<string, {_typescript_type(args[1])}>"
    if origin is Literal:
        values = get_args(annotation)
        if not values:
            raise ContractGenerationError("empty Literal is unsupported")
        return " | ".join(_literal(value) for value in values)

    if origin in {types.UnionType, Union}:
        args = get_args(annotation)
        nullable = type(None) in args
        concrete = [arg for arg in args if arg is not type(None)]
        if len(concrete) != 1:
            raise ContractGenerationError(f"unsupported union annotation: {annotation!r}")
        rendered = _typescript_type(concrete[0])
        return f"{rendered} | null" if nullable else rendered

    raise ContractGenerationError(f"unsupported annotation: {annotation!r}")


def render_model(model: type[BaseModel], export_name: str, *, header: str = HEADER) -> str:
    lines = [header, f"export type {export_name} = {{\n"]
    for name, field in model.model_fields.items():
        try:
            rendered = _typescript_type(field.annotation)
        except ContractGenerationError as exc:
            raise ContractGenerationError(f"{model.__name__}.{name}: {exc}") from exc
        # This is a response contract. Pydantic defaults do not imply that a
        # serialized response property may be omitted, so no '?' is emitted.
        lines.append(f"  {name}: {rendered};\n")
    lines.append("};\n")
    return "".join(lines)


def render_parameter_read() -> str:
    return render_model(ParameterRead, "ParameterRead")


def render_engineering_reads() -> str:
    header = HEADER.replace(SOURCE_LABEL, "backend/app/modules/engineering/operator_models.py")
    return render_model(EvaluatorRead, "EvaluatorRead", header=header) + "\n" + render_model(
        CapabilityRead, "CapabilityRead", header=""
    )


def render_editor_contracts() -> str:
    from app.modules.process_stack import editor_models
    from app.modules.process_stack.editor_models import (
        CommandResult,
        DynamicsProjectionRead,
        EditorCaseRead,
        EditorConnectionRead,
        EditorObjectRead,
        EditorProjectionRead,
        EditorQuantity,
        RevisionRead,
    )

    models = [
        EditorQuantity,
        DynamicsProjectionRead,
        EditorCaseRead,
        RevisionRead,
        EditorObjectRead,
        EditorConnectionRead,
        EditorProjectionRead,
        CommandResult,
    ]
    commands = typing.get_args(editor_models.EditorCommand)
    rendered = [
        render_model(model, model.__name__, header=EDITOR_HEADER if index == 0 else "")
        for index, model in enumerate([*models, *commands])
    ]
    union = "export type EditorCommand =\n" + "".join(f"  | {model.__name__}\n" for model in commands)
    return "\n".join(rendered) + "\n" + union.rstrip("\n") + ";\n"


def _matches(path: Path, expected: str, *, report: bool = False) -> bool:
    try:
        actual = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if report:
            print(f"generated contract is missing: {path}", file=sys.stderr)
        return False
    if actual == expected:
        return True
    if report:
        diff = difflib.unified_diff(
            actual.splitlines(keepends=True),
            expected.splitlines(keepends=True),
            fromfile=str(path),
            tofile=f"{path} (generated)",
        )
        sys.stderr.writelines(diff)
    return False


def self_test() -> None:
    from app.modules.modeling.models import ParameterRead

    rendered = render_parameter_read()
    assert rendered == render_parameter_read()
    expected_names = [
        "name",
        "symbol",
        "value",
        "unit",
        "value_status",
        "value_min",
        "value_max",
        "source_ref",
        "confidence",
        "status",
        "notes",
        "supersedes_parameter_id",
        "id",
        "workspace_id",
        "created_at",
        "updated_at",
        "lifecycle_state",
    ]
    actual_names = [line.strip().split(":", 1)[0] for line in rendered.splitlines() if line.startswith("  ")]
    assert actual_names == expected_names
    assert "  unit: string;" in rendered
    assert "  symbol: string | null;" in rendered
    assert "  value_min: number | null;" in rendered
    assert "  confidence: number | null;" in rendered
    assert (
        '  value_status: "candidate" | "literature" | "measured" | "validated" | "accepted";'
        in rendered
    )
    assert (
        '  lifecycle_state: "active" | "inactive" | "superseded" | "archived" | "deleted";'
        in rendered
    )
    assert "?:" not in rendered

    class ChangedParameterRead(ParameterRead):
        codegen_probe: str

    assert render_model(ChangedParameterRead, "ParameterRead") != rendered

    class Unsupported(BaseModel):
        items: set[str]

    try:
        render_model(Unsupported, "Unsupported")
    except ContractGenerationError as exc:
        assert "Unsupported.items" in str(exc)
    else:
        raise AssertionError("unsupported schema shape must fail closed")

    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / "modeling.ts"
        target.write_text(rendered, encoding="utf-8", newline="\n")
        assert _matches(target, rendered)
        target.write_text(rendered + "// tampered\n", encoding="utf-8", newline="\n")
        assert not _matches(target, rendered)
        target.write_text(render_model(ChangedParameterRead, "ParameterRead"), encoding="utf-8", newline="\n")
        assert not _matches(target, rendered)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail if the checked-in generated file is stale")
    parser.add_argument("--self-test", action="store_true", help="run deterministic generator self-tests")
    args = parser.parse_args()

    if args.self_test:
        self_test()
        print("frontend contract generator self-test: OK")
        return 0

    expected = render_parameter_read()
    engineering_expected = render_engineering_reads()
    editor_expected = render_editor_contracts()
    if args.check:
        if (
            _matches(TARGET, expected, report=True)
            and _matches(ENGINEERING_TARGET, engineering_expected, report=True)
            and _matches(EDITOR_TARGET, editor_expected, report=True)
        ):
            print("frontend generated contracts are current")
            return 0
        print("frontend generated contracts are stale; regenerate before committing", file=sys.stderr)
        return 1

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(expected, encoding="utf-8", newline="\n")
    ENGINEERING_TARGET.write_text(engineering_expected, encoding="utf-8", newline="\n")
    EDITOR_TARGET.parent.mkdir(parents=True, exist_ok=True)
    EDITOR_TARGET.write_text(editor_expected, encoding="utf-8", newline="\n")
    print(f"wrote {TARGET.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
