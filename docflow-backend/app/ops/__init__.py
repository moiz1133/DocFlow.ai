"""Operational hardening — rate limiting, metrics, tracing (Phase 8).

Distinct from app/security/ (Phase 7's HIPAA control layer: encryption,
consent, audit, TLS): this package is about running the service
reliably and observably, not about PHI protection per se — though every
module here still follows the same PHI-free-by-default discipline.
"""
