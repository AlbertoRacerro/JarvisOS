"""Verification-only public surface for analytic BLUECAD FEM benchmarks.

The implementation is deliberately separate from the production FEM result
summary parser. It provides closed-form references, component-aware parsing,
location sampling, tensor transforms, pressure-surface audits, fixture integrity
checks, and the bounded spec 024-C2 real-tool battery.
"""

from app.modules.bluecad.fem_verification_analytics import (
    beam_tip_displacement,
    finite_width_hole_factor,
    finite_width_hole_reference,
    lame_open_end_bore_stresses,
)
from app.modules.bluecad.fem_verification_battery import (
    build_battery_report,
    cantilever_spec,
    evaluate_cantilever,
    evaluate_lame,
    evaluate_plate,
    lame_spec,
    nonzero_resultant_balance,
    plate_spec,
    render_battery_report,
    self_equilibrated_reaction_balance,
)
from app.modules.bluecad.fem_verification_common import (
    FemVerificationError,
    comparison_record,
    deterministic_mean,
    relative_error,
)
from app.modules.bluecad.fem_verification_fixtures import verify_fixture_index
from app.modules.bluecad.fem_verification_parsers import (
    displacement_block,
    latest_frd_block,
    parse_frd_blocks,
    parse_inp_mesh,
    stress_block,
)
from app.modules.bluecad.fem_verification_runner import (
    run_fem_verification_battery,
)
from app.modules.bluecad.fem_verification_sampling import (
    audit_segmented_pressure_surface,
    cylindrical_stress_components,
    select_nodes_near,
)

__all__ = [
    "FemVerificationError",
    "audit_segmented_pressure_surface",
    "beam_tip_displacement",
    "build_battery_report",
    "cantilever_spec",
    "comparison_record",
    "cylindrical_stress_components",
    "deterministic_mean",
    "displacement_block",
    "evaluate_cantilever",
    "evaluate_lame",
    "evaluate_plate",
    "finite_width_hole_factor",
    "finite_width_hole_reference",
    "lame_open_end_bore_stresses",
    "lame_spec",
    "latest_frd_block",
    "nonzero_resultant_balance",
    "parse_frd_blocks",
    "parse_inp_mesh",
    "plate_spec",
    "relative_error",
    "render_battery_report",
    "run_fem_verification_battery",
    "select_nodes_near",
    "self_equilibrated_reaction_balance",
    "stress_block",
    "verify_fixture_index",
]
