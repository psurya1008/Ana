"""Ana — a focus enforcement and accountability system for PC.

See docs/PLAN.md for the design. The short version: the pure layer
(models, classify, avoidance, penalties, scoring, engine) knows nothing about
the OS, the clock or the screen, so all of it is testable. `runtime` and
`detect` are the only modules that touch the machine.
"""

__version__ = "0.1.0"
