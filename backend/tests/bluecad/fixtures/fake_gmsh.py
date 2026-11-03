#!/usr/bin/env python3
from __future__ import annotations

import pathlib
import sys

args = sys.argv[1:]
out = pathlib.Path(args[args.index("-o") + 1])
cwd = str(pathlib.Path.cwd())
mode = "fail" if "failcase" in cwd else ("empty" if "emptycase" in cwd else "ok")
if "ignoreorder" in cwd:
    mode = "ignoreorder"
elif "negativejac" in cwd:
    mode = "negativejac"
elif "zerojac" in cwd:
    mode = "zerojac"

if mode == "fail":
    print("Error: meshing failed", file=sys.stderr)
    sys.exit(2)

requested_order = 2 if "-order" in args and args[args.index("-order") + 1] == "2" else 1
effective_order = 1 if mode == "ignoreorder" else requested_order

nodes = "\n".join(f" {node_id},{node_id % 2},{(node_id // 2) % 2},{(node_id // 4) % 2}" for node_id in range(1, 11))
if effective_order == 2:
    element = "*ELEMENT, TYPE=C3D10, ELSET=Volume1\n 1,1,2,3,4,5,6,7,8,9,10"
else:
    element = "*ELEMENT, TYPE=C3D4, ELSET=Volume1\n 1,1,2,3,4"

bc_members = "" if mode == "empty" else " 1,2,3,"
ok_mesh = f"""*Heading
 fake real-style Gmsh Abaqus export
*NODE
{nodes}
{element}
*ELSET, ELSET=BODY
 1,
*NSET, NSET=BC_run1_port_a
{bc_members}
*NSET, NSET=LOAD_joint1_port_b
 1,3,4,
"""

out.write_text(ok_mesh, encoding="utf-8")
(pathlib.Path.cwd() / "mesh.msh").write_text("$MeshFormat\n", encoding="utf-8")
if mode == "negativejac":
    print("Warning: 244 elements with jac. < 0")
elif mode == "zerojac":
    print("Info: 0 elements with jac. < 0")
print(f"Info: fake gmsh quality ok requested_order={requested_order} effective_order={effective_order}")
print("ARGS: " + " ".join(args))
