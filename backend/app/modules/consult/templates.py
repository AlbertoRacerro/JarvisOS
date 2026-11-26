"""Fixed server templates, one per consult kind (spec 380). Inkling never edits them; they carry no project content."""

from __future__ import annotations

from typing import Literal

ConsultKind = Literal["ideas", "physics", "literature", "critique"]

TEMPLATES: dict[str, str] = {
    "ideas": ("Task: propose ideas and mechanisms for the question below. Give several distinct options with their "
              "physical reasoning and the main risk of each. Do not compute project numbers."),
    "physics": ("Task: explain the physics behind the question below: governing mechanisms, dimensionless groups, "
                "which effects dominate and why. Use orders of magnitude only. Do not compute project numbers."),
    "literature": ("Task: point to published work relevant to the question below: authors, year, venue and what each "
                   "work shows. Say plainly when you are not sure a reference exists."),
    "critique": ("Task: critique the approach described below. Name weak assumptions, missing effects and checks "
                 "that would falsify it. Do not compute project numbers."),
}
