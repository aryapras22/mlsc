"""Detectors: each takes a series and a baseline, returns a ``TestResult`` if
its own test is significant, and raises nothing that escapes — a detector
that cannot run on a given series returns no result rather than failing the
whole ensemble (design.md, "Failure strategy": ``DetectorFailed`` falls
back).
"""
