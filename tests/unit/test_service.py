# MFG-C2-014 — Unit Tests: Service (self-contained v1 compatibility contract)
#
# Service.fetch() must never raise NotImplementedError (an
# unused, ungoverned stub). It now returns a defined "no external backend"
# envelope. These tests pin that compatibility contract. asyncio.run() is used
# so no pytest-asyncio plugin is required.

import asyncio

from src.services.service import Service


class TestService:
    def test_fetch_returns_self_contained_envelope(self):
        svc = Service()
        result = asyncio.run(svc.fetch("MFG-EQ-1", {"channel": "cmms"}))
        assert result["external_backend"] is False
        assert result["data"] is None
        assert result["source"] == "mfg-c2-014-self-contained"
        assert result["query"] == "MFG-EQ-1"
        assert result["context"] == {"channel": "cmms"}

    def test_fetch_defaults_context_to_empty_dict(self):
        svc = Service()
        result = asyncio.run(svc.fetch("MFG-EQ-2"))
        assert result["context"] == {}
        assert result["external_backend"] is False
