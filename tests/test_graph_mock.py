# tests/test_graph_mock.py
# Unit tests for mock graph backend

import os
import sys
import pytest

sys.path.insert(0, ".")


def test_mock_graph_returns_zero_risk(monkeypatch):
    """GRAPH_BACKEND=mock returns zero-risk GraphRiskOutput with no related parties."""
    monkeypatch.setenv("GRAPH_BACKEND", "mock")

    # Re-import after env var is set so module-level constant picks it up
    import importlib
    import graph.analyzer as ga
    importlib.reload(ga)

    analyzer = ga.GraphAnalyzer()
    result = analyzer.analyze("person-123", "app-456")

    assert result.graph_risk_score == 0.0
    assert result.related_parties == []
    assert result.household_default_count == 0
    assert result.fund_flow_to_defaulter is False
    assert result.cluster_density == 0.0
    assert result.shortest_path_to_defaulter is None


def test_mock_graph_score_is_bounded(monkeypatch):
    """Mock graph risk score must be in [0.0, 1.0]."""
    monkeypatch.setenv("GRAPH_BACKEND", "mock")

    import importlib
    import graph.analyzer as ga
    importlib.reload(ga)

    analyzer = ga.GraphAnalyzer()
    result = analyzer.analyze("any-person", "any-app")

    assert 0.0 <= result.graph_risk_score <= 1.0
