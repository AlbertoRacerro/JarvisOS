"""104 selected upstream process stack behind the 106 evaluator boundary.

103 evidence (spec 103; not carried into this repository) selected
upstream owners for generic property, correlation and time-integration work:
CoolProp for pure-fluid properties, fluids/ht for pipe friction and internal
convection correlations, and SUNDIALS CVODE (scikit-sundae) as the single
time/state owner for ODE models. New process work uses these owners instead
of growing custom solver code; the 047 process kernel stays a frozen screening
fixture with its exact bundle identity.

Availability and convergence are runtime facts, never scientific qualification.
"""
