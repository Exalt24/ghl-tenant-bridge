"""Relay: takes a GoHighLevel lead delivery and fans it out to the sinks a tenant enabled.

At least once, with a stored outcome per (event, sink). Never "exactly once": where a vendor offers no idempotency
key the README says so per sink.
"""

__version__ = "0.1.0"
