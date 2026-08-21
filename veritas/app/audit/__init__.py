"""Audit pipeline package — multi-agent core (architecture §7).

Stages (named internal agents, not bots — nothing acts outside the product):
  * normalize  — Ingest/Normalizer
  * match      — Rule Engine / Compliance Matcher
  * report     — Report Synthesizer
  * quote      — Quote Agent (stub at Phase 0.3; real logic in Phase 0.5)
"""
