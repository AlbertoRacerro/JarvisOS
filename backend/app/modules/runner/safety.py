import ast
import hashlib
import json
from math import isfinite
from pathlib import Path
from typing import Any

from app.core.paths import build_paths
from app.modules.bluecad.spec import SpecValidationError, canonicalize_geometry_spec

MAX_INPUT_JSON_BYTES = 64 * 1024
MAX_OUTPUT_JSON_BYTES = 1024 * 1024
MAX_STDOUT_BYTES = 64 * 1024
MAX_STDERR_BYTES = 64 * 1024
MAX_ARTIFACT_BYTES = 10 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 10
MAX_TIMEOUT_SECONDS = 60

REQUIRED_BATCH_GROWTH_PARAMETERS = ("mu_max", "X0", "t_final", "dt")

ALLOWED_CALC_V0_IMPORT_ROOTS = frozenset({"json", "math", "statistics"})

ALLOWED_BLUECAD_L2_IMPORT_ROOTS = frozenset({
    "build123d",
    "collections",
    "dataclasses",
    "decimal",
    "enum",
    "functools",
    "itertools",
    "json",
    "math",
    "operator",
    "pathlib",
    "statistics",
    "typing",
})
BLUECAD_L2_REQUIRED_ARTIFACTS = {
    "bluecad_step": "model.step",
    "bluecad_stl": "model.stl",
    "bluecad_glb": "model.glb",
    "bluecad_manifest": "manifest.json",
}
SANDBOX_VIOLATION = "SANDBOX_VIOLATION"
FORBIDDEN_DYNAMIC_NAME_REFERENCES = frozenset({
    "__import__",
    "breakpoint",
    "compile",
    "delattr",
    "eval",
    "exec",
    "getattr",
    "globals",
    "locals",
    "setattr",
    "vars",
})
FORBIDDEN_BLUECAD_L2_NAME_REFERENCES = FORBIDDEN_DYNAMIC_NAME_REFERENCES
FORBIDDEN_CALC_V0_NAME_REFERENCES = FORBIDDEN_DYNAMIC_NAME_REFERENCES | frozenset({"__builtins__"})
FORBIDDEN_SCRIPT_MARKERS = (
    "import socket",
    "from socket",
    "import requests",
    "from requests",
    "import httpx",
    "from httpx",
    "import urllib",
    "from urllib",
    "import subprocess",
    "from subprocess",
    "from os import system",
    "os.system",
    "os.popen",
    "popen(",
    "shutil.rmtree",
    "os.remove",
    "os.unlink",
    ".unlink(",
    "rmtree(",
    ".env",
    "os.environ",
    "getenv(",
    "environ[",
    "api_key",
    "api key",
    "password",
    "token",
    "secret",
    "private_key",
    "private key",
)


class RunnerSafetyError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def canonical_json(data: dict[str, Any]) -> str:
    try:
        return json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise RunnerSafetyError("runner_input_invalid", "Input set must be finite JSON.") from exc


def validate_batch_growth_input(input_set: dict[str, Any]) -> tuple[str, str]:
    encoded = canonical_json(input_set)
    if len(encoded.encode("utf-8")) > MAX_INPUT_JSON_BYTES:
        raise RunnerSafetyError("runner_input_too_large", "Input JSON exceeds the V0 runner size limit.")

    schema_version = input_set.get("schema_version", 1)
    if isinstance(schema_version, bool):
        raise RunnerSafetyError("runner_input_invalid", "schema_version must be an integer.")
    try:
        schema_version_number = int(schema_version)
    except (TypeError, ValueError) as exc:
        raise RunnerSafetyError("runner_input_invalid", "schema_version must be an integer.") from exc

    parameters = input_set.get("parameters")
    if not isinstance(parameters, dict):
        raise RunnerSafetyError("runner_input_invalid", "Input set must include a parameters object.")

    input_artifact_ids = input_set.get("input_artifact_ids", [])
    if not isinstance(input_artifact_ids, list) or not all(isinstance(item, str) for item in input_artifact_ids):
        raise RunnerSafetyError("runner_input_invalid", "input_artifact_ids must be a list of artifact id strings.")

    normalized: dict[str, float] = {}
    for key in REQUIRED_BATCH_GROWTH_PARAMETERS:
        if key not in parameters:
            raise RunnerSafetyError("runner_input_invalid", f"Missing required parameter: {key}.")
        value = parameters[key]
        if isinstance(value, bool):
            raise RunnerSafetyError("runner_input_invalid", f"{key} must be numeric.")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise RunnerSafetyError("runner_input_invalid", f"{key} must be numeric.") from exc
        if not isfinite(number):
            raise RunnerSafetyError("runner_input_invalid", f"{key} must be finite.")
        normalized[key] = number

    if normalized["dt"] <= 0:
        raise RunnerSafetyError("runner_input_invalid", "dt must be greater than zero.")
    if normalized["mu_max"] < 0:
        raise RunnerSafetyError("runner_input_invalid", "mu_max must be nonnegative.")
    if normalized["X0"] < 0:
        raise RunnerSafetyError("runner_input_invalid", "X0 must be nonnegative.")
    if normalized["t_final"] < 0:
        raise RunnerSafetyError("runner_input_invalid", "t_final must be nonnegative.")
    if normalized["t_final"] / normalized["dt"] > 10000:
        raise RunnerSafetyError("runner_input_invalid", "The requested time grid is too large for V0.")

    normalized_input = {
        "schema_version": schema_version_number,
        "parameters": normalized,
        "input_artifact_ids": input_artifact_ids,
    }
    return canonical_json(normalized_input), canonical_json(normalized)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_within_directory(path: Path, root: Path, code: str = "runner_path_outside_allowed_root") -> Path:
    resolved_path = path.resolve()
    resolved_root = root.resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise RunnerSafetyError(code, "Path is outside the allowed runner directory.") from exc
    return resolved_path


def model_implementation_root(workspace_id: str) -> Path:
    return build_paths().workspaces_dir / workspace_id / "model_implementations"


def run_root(workspace_id: str, simulation_run_id: str) -> Path:
    return build_paths().workspaces_dir / workspace_id / "runs" / simulation_run_id


def validate_script_path(workspace_id: str, script_path: str) -> Path:
    path = ensure_within_directory(
        Path(script_path),
        model_implementation_root(workspace_id),
        code="runner_script_path_outside_allowed_root",
    )
    if path.suffix.lower() != ".py":
        raise RunnerSafetyError("runner_script_not_python", "Runner script must be a Python file.")
    if not path.exists():
        raise RunnerSafetyError("runner_script_artifact_missing", "Runner script artifact is missing.")
    return path


def validate_run_paths(
    workspace_id: str,
    simulation_run_id: str,
    *,
    working_dir: str,
    input_file: str | None,
    output_dir: str,
) -> tuple[Path, Path, Path]:
    root = run_root(workspace_id, simulation_run_id)
    resolved_root = root.resolve()
    resolved_working_dir = ensure_within_directory(
        Path(working_dir),
        root,
        code="runner_working_dir_outside_run_root",
    )
    resolved_output_dir = ensure_within_directory(
        Path(output_dir),
        root,
        code="runner_output_dir_outside_run_root",
    )
    if input_file is None:
        raise RunnerSafetyError("runner_input_file_missing", "Runner input file path is missing.")
    resolved_input_file = ensure_within_directory(
        Path(input_file),
        root,
        code="runner_input_file_outside_run_root",
    )

    if resolved_working_dir != resolved_root:
        raise RunnerSafetyError("runner_working_dir_invalid", "Runner working directory must match the run root.")
    if resolved_output_dir != resolved_root:
        raise RunnerSafetyError("runner_output_dir_invalid", "Runner output directory must match the run root.")
    if resolved_input_file != resolved_root / "input.json":
        raise RunnerSafetyError("runner_input_file_invalid", "Runner input file must be input.json in the run root.")

    return resolved_working_dir, resolved_input_file, resolved_output_dir


def validate_calc_v0_input(input_set: dict[str, Any]) -> tuple[str, str]:
    encoded = canonical_json(input_set)
    if len(encoded.encode("utf-8")) > MAX_INPUT_JSON_BYTES:
        raise RunnerSafetyError("runner_input_too_large", "Input JSON exceeds the V0 runner size limit.")
    if not isinstance(input_set, dict):
        raise RunnerSafetyError("runner_input_invalid", "calc_v0 input set must be an object.")
    normalized: dict[str, dict[str, object]] = {}
    for name, item in input_set.items():
        if not isinstance(name, str) or not name.strip():
            raise RunnerSafetyError("runner_input_invalid", "calc_v0 input names must be non-empty strings.")
        if not isinstance(item, dict):
            raise RunnerSafetyError("runner_input_invalid", f"calc_v0 input {name} must be an object.")
        value = item.get("value")
        if isinstance(value, bool):
            raise RunnerSafetyError("runner_input_invalid", f"calc_v0 input {name} value must be numeric.")
        try:
            number = float(value)
        except (OverflowError, TypeError, ValueError) as exc:
            raise RunnerSafetyError("runner_input_invalid", f"calc_v0 input {name} value must be numeric.") from exc
        if not isfinite(number):
            raise RunnerSafetyError("runner_input_invalid", f"calc_v0 input {name} value must be finite.")
        unit = item.get("unit")
        if not isinstance(unit, str) or not unit.strip():
            raise RunnerSafetyError("runner_input_invalid", f"calc_v0 input {name} unit must be a non-empty string.")
        normalized_item: dict[str, object] = {"value": number, "unit": unit}
        source_parameter_id = item.get("source_parameter_id")
        if source_parameter_id is not None:
            if not isinstance(source_parameter_id, str) or not source_parameter_id.strip():
                raise RunnerSafetyError(
                    "runner_input_invalid",
                    f"calc_v0 input {name} source_parameter_id must be a non-empty string.",
                )
            normalized_item["source_parameter_id"] = source_parameter_id
        source_parameter_updated_at = item.get("source_parameter_updated_at")
        if source_parameter_updated_at is not None:
            if source_parameter_id is None:
                raise RunnerSafetyError(
                    "runner_input_invalid",
                    f"calc_v0 input {name} source_parameter_updated_at requires source_parameter_id.",
                )
            if not isinstance(source_parameter_updated_at, str) or not source_parameter_updated_at.strip():
                raise RunnerSafetyError(
                    "runner_input_invalid",
                    f"calc_v0 input {name} source_parameter_updated_at must be a non-empty string.",
                )
            normalized_item["source_parameter_updated_at"] = source_parameter_updated_at
        normalized[name] = normalized_item
    normalized_encoded = canonical_json(normalized)
    return normalized_encoded, normalized_encoded


def validate_bluecad_l2_input(input_set: dict[str, Any]) -> tuple[str, str]:
    try:
        normalized = canonicalize_geometry_spec(input_set)
    except SpecValidationError as exc:
        raise RunnerSafetyError("runner_input_invalid", f"Invalid GeometrySpec v0 payload: {exc.detail}") from exc
    encoded = canonical_json(normalized)
    if len(encoded.encode("utf-8")) > MAX_INPUT_JSON_BYTES:
        raise RunnerSafetyError("runner_input_too_large", "Input JSON exceeds the V0 runner size limit.")
    return encoded, encoded


def preflight_script_policy(script_path: Path, *, ast_policy: str | None = None, ast_import_allowlist: bool = False) -> None:
    text = script_path.read_text(encoding="utf-8")
    lowered = text.lower()
    for marker in FORBIDDEN_SCRIPT_MARKERS:
        if marker in lowered:
            code = SANDBOX_VIOLATION if (ast_import_allowlist or ast_policy) else "runner_policy_blocked"
            raise RunnerSafetyError(code, f"Script contains blocked marker: {marker}.")
    if ast_import_allowlist or ast_policy == "bluecad_l2_v0":
        preflight_bluecad_l2_ast_policy(text)
    elif ast_policy == "calc_v0":
        preflight_calc_v0_ast_policy(text)


def preflight_calc_v0_ast_policy(source: str) -> None:
    _preflight_ast_policy(
        source,
        ALLOWED_CALC_V0_IMPORT_ROOTS,
        FORBIDDEN_CALC_V0_NAME_REFERENCES,
        enforce_calc_file_contract=True,
    )


def preflight_bluecad_l2_ast_policy(source: str) -> None:
    _preflight_ast_policy(source, ALLOWED_BLUECAD_L2_IMPORT_ROOTS, FORBIDDEN_BLUECAD_L2_NAME_REFERENCES)


def _preflight_ast_policy(
    source: str,
    allowed_import_roots: frozenset[str],
    forbidden_name_references: frozenset[str],
    *,
    enforce_calc_file_contract: bool = False,
) -> None:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise RunnerSafetyError(SANDBOX_VIOLATION, "Script source must be parseable Python.") from exc
    parents = _ast_parent_map(tree) if enforce_calc_file_contract else {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _validate_ast_import(alias.name, allowed_import_roots)
        elif isinstance(node, ast.ImportFrom):
            if node.level != 0 or node.module is None:
                raise RunnerSafetyError(SANDBOX_VIOLATION, "Relative imports are not allowed.")
            if any(alias.name == "*" for alias in node.names):
                raise RunnerSafetyError(SANDBOX_VIOLATION, "Star imports are not allowed.")
            _validate_ast_import(node.module, allowed_import_roots)
        elif isinstance(node, ast.Call):
            name = _call_name(node.func)
            if (
                name in forbidden_name_references
                or name.rsplit(".", 1)[-1] in forbidden_name_references
                or name.startswith("importlib.")
            ):
                raise RunnerSafetyError(SANDBOX_VIOLATION, f"Dynamic code loading is not allowed: {name}.")
            if enforce_calc_file_contract:
                _validate_calc_call_file_contract(node)
        elif enforce_calc_file_contract and _is_forbidden_calc_open_reference(node, parents):
            raise RunnerSafetyError(SANDBOX_VIOLATION, "calc_v0 open() access must be a direct checked call.")
        elif enforce_calc_file_contract and _is_loaded_dunder_reference(node):
            raise RunnerSafetyError(SANDBOX_VIOLATION, "calc_v0 dunder introspection is not allowed.")
        elif (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in forbidden_name_references
        ):
            raise RunnerSafetyError(
                SANDBOX_VIOLATION,
                f"Dangerous builtin reference is not allowed: {node.id}.",
            )


def _validate_ast_import(module_name: str, allowed_import_roots: frozenset[str]) -> None:
    root = module_name.split(".", 1)[0]
    if root not in allowed_import_roots:
        raise RunnerSafetyError(SANDBOX_VIOLATION, f"Import is not allowlisted: {module_name}.")
    if root == "collections" and module_name not in {"collections", "collections.abc"}:
        raise RunnerSafetyError(SANDBOX_VIOLATION, f"Import is not allowlisted: {module_name}.")
    if root != "collections" and "." in module_name and root != "build123d":
        raise RunnerSafetyError(SANDBOX_VIOLATION, f"Submodule import is not allowlisted: {module_name}.")


def _ast_parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


def _is_loaded_dunder_reference(node: ast.AST) -> bool:
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
        return _is_dunder_name(node.id)
    if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
        return _is_dunder_name(node.attr)
    return False


def _is_dunder_name(name: str) -> bool:
    return len(name) > 4 and name.startswith("__") and name.endswith("__")


def _is_forbidden_calc_open_reference(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id == "open":
        parent = parents.get(node)
        return not (isinstance(parent, ast.Call) and parent.func is node)
    if isinstance(node, ast.Attribute) and node.attr == "open":
        return True
    if _is_builtins_open_subscript(node):
        return True
    return False


def _validate_calc_call_file_contract(node: ast.Call) -> None:
    name = _call_name(node.func)
    if name == "open":
        _validate_calc_open_call(node)
        return
    if name.endswith(".open") or _is_builtins_open_subscript(node.func):
        raise RunnerSafetyError(SANDBOX_VIOLATION, "calc_v0 open() access must be a direct checked call.")


def _validate_calc_open_call(node: ast.Call) -> None:
    if not node.args or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
        raise RunnerSafetyError(
            SANDBOX_VIOLATION,
            "calc_v0 open() paths must be literal allowlisted filenames.",
        )
    path = node.args[0].value
    if path not in {"input.json", "result.json"}:
        raise RunnerSafetyError(
            SANDBOX_VIOLATION,
            "calc_v0 script open() path is not allowlisted.",
        )
    mode = _calc_open_mode(node)
    if path == "input.json" and mode != "r":
        raise RunnerSafetyError(SANDBOX_VIOLATION, "calc_v0 input.json must be opened read-only text mode.")
    if path == "result.json" and mode != "w":
        raise RunnerSafetyError(
            SANDBOX_VIOLATION,
            f"calc_v0 {path} must be opened write-only text truncate mode.",
        )


def _calc_open_mode(node: ast.Call) -> str:
    mode_node: ast.AST | None = None
    if len(node.args) >= 2:
        mode_node = node.args[1]
    for keyword in node.keywords:
        if keyword.arg is None:
            raise RunnerSafetyError(SANDBOX_VIOLATION, "calc_v0 open() does not allow dynamic keyword expansion.")
        if keyword.arg == "mode":
            if mode_node is not None:
                raise RunnerSafetyError(SANDBOX_VIOLATION, "calc_v0 open() mode must be declared once.")
            mode_node = keyword.value
    if mode_node is None:
        return "r"
    if not isinstance(mode_node, ast.Constant) or not isinstance(mode_node.value, str):
        raise RunnerSafetyError(SANDBOX_VIOLATION, "calc_v0 open() mode must be a literal string.")
    mode = mode_node.value
    if any(marker in mode for marker in ("+", "a", "x", "b")):
        raise RunnerSafetyError(SANDBOX_VIOLATION, "calc_v0 open() mode is not allowed.")
    if mode not in {"r", "rt", "w", "wt"}:
        raise RunnerSafetyError(SANDBOX_VIOLATION, "calc_v0 open() mode is not allowed.")
    return mode[0]


def _is_builtins_open_subscript(node: ast.AST) -> bool:
    if not isinstance(node, ast.Subscript):
        return False
    value = _call_name(node.value)
    if value != "__builtins__":
        return False
    slice_node = node.slice
    return isinstance(slice_node, ast.Constant) and slice_node.value == "open"


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _call_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def safe_artifact_path(output_dir: Path, relative_path: str) -> Path:
    if not relative_path or Path(relative_path).is_absolute():
        raise RunnerSafetyError("runner_artifact_path_outside_output_dir", "Artifact path must be relative.")
    return ensure_within_directory(
        output_dir / relative_path,
        output_dir,
        code="runner_artifact_path_outside_output_dir",
    )
