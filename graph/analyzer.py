# graph/analyzer.py
# Graph link analysis — detects related-party fraud via Neptune (prod) / Neo4j (local)

from __future__ import annotations
import os
import logging
from typing import List, Optional, Dict, Any
from datetime import datetime, date, timedelta, timezone

from api.schemas.models import RelatedParty, GraphRiskOutput

logger = logging.getLogger(__name__)
UTC = timezone.utc

# ─── Driver abstraction ──────────────────────────────────────────────────────
# In production: uses boto3 + neptune-python-utils for AWS Neptune (Gremlin)
# In local dev:  uses neo4j driver pointing at docker neo4j
# Toggle via GRAPH_BACKEND env var

def _get_graph_backend():
    """Read backend at call time so env changes work without module reload."""
    return os.getenv("GRAPH_BACKEND", "neo4j")  # 'neptune' | 'neo4j' | 'mock'


# Keep module-level constant for backward compatibility with tests
GRAPH_BACKEND = _get_graph_backend()


def _get_driver(backend=None):
    backend = backend or _get_graph_backend()
    if backend == "mock":
        return None
    if backend == "neptune":
        from gremlin_python.driver.driver_remote_connection import DriverRemoteConnection
        from gremlin_python.structure.graph import Graph
        endpoint = os.environ["NEPTUNE_ENDPOINT"]
        graph = Graph()
        return graph.traversal().withRemote(
            DriverRemoteConnection(f"wss://{endpoint}:8182/gremlin", "g")
        )
    else:
        from neo4j import GraphDatabase
        uri  = os.getenv("NEO4J_URI", "bolt://localhost:7687")
        user = os.getenv("NEO4J_USER", "neo4j")
        pwd  = os.getenv("NEO4J_PASSWORD", "localdev_password")
        return GraphDatabase.driver(uri, auth=(user, pwd))


class GraphAnalyzer:
    """
    Queries the graph DB to:
    1. Find all persons linked to the applicant
    2. Identify recent defaults in the related-party network
    3. Compute graph risk features (path distance, cluster density, fund flow)
    4. Return a GraphRiskOutput for the evidence bundle
    """

    def __init__(self):
        self._driver = None

    @property
    def driver(self):
        if self._driver is None:
            self._driver = _get_driver()
        return self._driver

    def analyze(self, person_id: str, application_id: str) -> GraphRiskOutput:
        """Main entry point — returns full graph risk output."""
        backend = _get_graph_backend()
        if backend == "mock":
            return GraphRiskOutput(
                related_parties=[],
                household_default_count=0,
                shortest_path_to_defaulter=None,
                fund_flow_to_defaulter=False,
                cluster_density=0.0,
                graph_risk_score=0.0,
            )
        elif backend == "neo4j":
            return self._analyze_neo4j(person_id, application_id)
        else:
            return self._analyze_neptune(person_id, application_id)

    # ─── Neo4j Implementation ────────────────────────────────────────────────

    def _analyze_neo4j(self, person_id: str, application_id: str) -> GraphRiskOutput:
        with self.driver.session() as session:
            related  = session.execute_read(self._query_related_parties, person_id)
            defaults = session.execute_read(self._query_household_defaults, person_id)
            path_len = session.execute_read(self._query_shortest_path_to_defaulter, person_id)
            fund_flow= session.execute_read(self._query_fund_flow_to_defaulter, application_id)
            density  = session.execute_read(self._query_cluster_density, person_id)

        graph_risk_score = self._compute_graph_risk_score(
            related_parties=related,
            household_default_count=defaults,
            shortest_path=path_len,
            fund_flow_to_defaulter=fund_flow,
            cluster_density=density,
        )

        return GraphRiskOutput(
            related_parties=related,
            household_default_count=defaults,
            shortest_path_to_defaulter=path_len,
            fund_flow_to_defaulter=fund_flow,
            cluster_density=density,
            graph_risk_score=graph_risk_score,
        )

    @staticmethod
    def _query_related_parties(tx, person_id: str) -> List[RelatedParty]:
        """Find all persons within 2 hops sharing key attributes."""
        result = tx.run("""
            MATCH (p:Person {person_id: $person_id})-[r:RELATED_TO|LIVES_AT|USES_PHONE|
                  USES_DEVICE|REPAYS_FROM|WORKS_AT*1..2]-(related:Person)
            WHERE related.person_id <> $person_id
            WITH DISTINCT related, r,
                 [attr IN ['address','phone','email','device','bank_account','employer']
                  WHERE EXISTS((p)-[:SHARES {attribute: attr}]-(related))] AS shared
            OPTIONAL MATCH (related)-[:DEFAULTED_ON]->(loan:Loan)
            WHERE loan.default_date >= date() - duration({days: 180})
            RETURN DISTINCT
                related.person_id       AS person_id,
                related.full_name       AS name,
                type(r[-1])             AS relationship_type,
                shared                  AS shared_attributes,
                size(shared) * 0.2      AS link_strength,
                loan IS NOT NULL        AS recent_default,
                loan.default_date       AS default_date,
                loan.amount             AS default_amount
            LIMIT 50
        """, person_id=person_id)

        parties = []
        for rec in result:
            try:
                parties.append(RelatedParty(
                    person_id=rec["person_id"],
                    name=rec["name"],
                    relationship_type=rec["relationship_type"] or "linked",
                    shared_attributes=rec["shared_attributes"] or [],
                    link_strength=min(float(rec["link_strength"] or 0.1), 1.0),
                    recent_default=bool(rec["recent_default"]),
                    default_date=rec["default_date"],
                    default_amount=float(rec["default_amount"]) if rec["default_amount"] else None,
                ))
            except Exception as e:
                logger.warning(f"Error parsing related party record: {e}")
        return parties

    @staticmethod
    def _query_household_defaults(tx, person_id: str) -> int:
        result = tx.run("""
            MATCH (p:Person {person_id: $person_id})-[:LIVES_AT|RELATED_TO*1..2]-(h:Person)
            MATCH (h)-[:DEFAULTED_ON]->(l:Loan)
            WHERE l.default_date >= date() - duration({days: 365})
            RETURN count(DISTINCT l) AS default_count
        """, person_id=person_id)
        rec = result.single()
        return int(rec["default_count"]) if rec else 0

    @staticmethod
    def _query_shortest_path_to_defaulter(tx, person_id: str) -> Optional[int]:
        result = tx.run("""
            MATCH (p:Person {person_id: $person_id}),
                  (d:Person)-[:DEFAULTED_ON]->(l:Loan)
            WHERE l.default_date >= date() - duration({days: 365})
              AND d.person_id <> $person_id
            MATCH path = shortestPath((p)-[*..5]-(d))
            RETURN length(path) AS path_length
            ORDER BY path_length ASC
            LIMIT 1
        """, person_id=person_id)
        rec = result.single()
        return int(rec["path_length"]) if rec else None

    @staticmethod
    def _query_fund_flow_to_defaulter(tx, application_id: str) -> bool:
        result = tx.run("""
            MATCH (a:Application {application_id: $application_id})
            MATCH (a)-[:DISBURSED_TO]->(acct:Account)-[:TRANSFERRED_TO*1..3]->(dest:Account)
            MATCH (dest)<-[:OWNS_ACCOUNT]-(defaulter:Person)
            WHERE (defaulter)-[:DEFAULTED_ON]->(:Loan)
            RETURN count(defaulter) > 0 AS has_flow
        """, application_id=application_id)
        rec = result.single()
        return bool(rec["has_flow"]) if rec else False

    @staticmethod
    def _query_cluster_density(tx, person_id: str) -> float:
        """Ratio of actual to possible edges in local neighborhood."""
        result = tx.run("""
            MATCH (p:Person {person_id: $person_id})-[*1..2]-(neighbor:Person)
            WITH collect(DISTINCT neighbor) AS neighbors
            UNWIND neighbors AS n1
            UNWIND neighbors AS n2
            WHERE n1 <> n2
            OPTIONAL MATCH (n1)-[e]-(n2)
            WITH count(e) AS edges, size(neighbors) AS n
            RETURN CASE WHEN n > 1 THEN toFloat(edges) / (n * (n-1)) ELSE 0.0 END AS density
        """, person_id=person_id)
        rec = result.single()
        return float(rec["density"]) if rec else 0.0

    # ─── Neptune (Gremlin) Implementation ───────────────────────────────────

    def _analyze_neptune(self, person_id: str, application_id: str) -> GraphRiskOutput:
        """
        Gremlin traversal for AWS Neptune.
        Mirrors the Neo4j logic above using Gremlin steps.
        """
        from gremlin_python.process.anonymous_traversal import traversal
        from gremlin_python.process.graph_traversal import __

        g = self.driver

        # Related parties within 2 hops
        related_ids = (
            g.V().has("Person", "person_id", person_id)
             .both("RELATED_TO", "LIVES_AT", "USES_DEVICE", "USES_PHONE")
             .both("RELATED_TO", "LIVES_AT", "USES_DEVICE", "USES_PHONE")
             .has("Person", "person_id", __.neq(person_id))
             .dedup()
             .limit(50)
             .values("person_id")
             .toList()
        )

        # Build RelatedParty objects (simplified for Neptune)
        parties = []
        for rid in related_ids:
            props = (
                g.V().has("Person", "person_id", rid)
                 .elementMap("full_name", "recent_default", "default_date", "default_amount")
                 .next()
            )
            parties.append(RelatedParty(
                person_id=rid,
                name=props.get("full_name", "Unknown"),
                relationship_type="linked",
                shared_attributes=[],
                link_strength=0.5,
                recent_default=bool(props.get("recent_default", False)),
                default_date=props.get("default_date"),
                default_amount=props.get("default_amount"),
            ))

        household_defaults = len([p for p in parties if p.recent_default])
        graph_risk = self._compute_graph_risk_score(
            related_parties=parties,
            household_default_count=household_defaults,
            shortest_path=1 if any(p.recent_default for p in parties) else None,
            fund_flow_to_defaulter=False,
            cluster_density=0.0,
        )

        return GraphRiskOutput(
            related_parties=parties,
            household_default_count=household_defaults,
            shortest_path_to_defaulter=1 if any(p.recent_default for p in parties) else None,
            fund_flow_to_defaulter=False,
            cluster_density=0.0,
            graph_risk_score=graph_risk,
        )

    # ─── Risk Score Computation ───────────────────────────────────────────────

    def _compute_graph_risk_score(
        self,
        related_parties: List[RelatedParty],
        household_default_count: int,
        shortest_path: Optional[int],
        fund_flow_to_defaulter: bool,
        cluster_density: float,
    ) -> float:
        score = 0.0

        # Recent defaults in network
        recent_defaulters = [p for p in related_parties if p.recent_default]
        score += min(len(recent_defaulters) * 0.15, 0.45)

        # Household default concentration
        score += min(household_default_count * 0.10, 0.30)

        # Path distance — closer = higher risk
        if shortest_path is not None:
            score += max(0, 0.20 - (shortest_path - 1) * 0.05)

        # Direct fund flow
        if fund_flow_to_defaulter:
            score += 0.30

        # Cluster density
        score += cluster_density * 0.20

        return min(round(score, 4), 1.0)

    # ─── Graph Setup (Neptune node/edge creation) ────────────────────────────

    def upsert_person_node(self, person_id: str, attributes: Dict[str, Any]):
        """Create or update a Person node in the graph."""
        if _get_graph_backend() == "neo4j":
            with self.driver.session() as session:
                session.execute_write(lambda tx: tx.run("""
                    MERGE (p:Person {person_id: $person_id})
                    SET p += $attrs, p.updated_at = datetime()
                """, person_id=person_id, attrs=attributes))

    def upsert_application_node(self, app_id: str, person_id: str, attributes: Dict[str, Any]):
        """Create Application node and link to Person."""
        if _get_graph_backend() == "neo4j":
            with self.driver.session() as session:
                session.execute_write(lambda tx: tx.run("""
                    MERGE (a:Application {application_id: $app_id})
                    SET a += $attrs
                    WITH a
                    MATCH (p:Person {person_id: $person_id})
                    MERGE (p)-[:APPLIED_FOR]->(a)
                """, app_id=app_id, person_id=person_id, attrs=attributes))

    def link_persons(self, person_id_a: str, person_id_b: str,
                     link_type: str, shared_attributes: List[str]):
        """Create or strengthen a link between two persons."""
        if _get_graph_backend() == "neo4j":
            with self.driver.session() as session:
                session.execute_write(lambda tx: tx.run("""
                    MATCH (a:Person {person_id: $pid_a})
                    MATCH (b:Person {person_id: $pid_b})
                    MERGE (a)-[r:RELATED_TO {type: $link_type}]-(b)
                    SET r.shared_attributes = $attrs,
                        r.updated_at = datetime()
                """, pid_a=person_id_a, pid_b=person_id_b,
                     link_type=link_type, attrs=shared_attributes))
