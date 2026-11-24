import os
import pytest
from services.api_server.topology.parser import parse_helm_dir

FIXTURE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "fixtures", "topology", "online-boutique"))


def test_parser_extracts_nodes_and_edges_from_fixture():
    nodes, edges = parse_helm_dir(FIXTURE_DIR, namespace="boutique")
    
    # 4 services: cartservice, frontend, productcatalogservice, redis-cart
    assert len(nodes) == 4
    node_ids = [n.id for n in nodes]
    assert "cartservice.boutique" in node_ids
    assert "frontend.boutique" in node_ids
    assert "productcatalogservice.boutique" in node_ids
    assert "redis-cart.boutique" in node_ids

    # Check edges
    edge_pairs = [(e.from_node, e.to_node) for e in edges]
    assert ("frontend.boutique", "cartservice.boutique") in edge_pairs
    assert ("frontend.boutique", "productcatalogservice.boutique") in edge_pairs
    assert ("cartservice.boutique", "redis-cart.boutique") in edge_pairs


def test_parser_extracts_resource_limits():
    nodes, _ = parse_helm_dir(FIXTURE_DIR, namespace="boutique")
    cart_node = next(n for n in nodes if n.id == "cartservice.boutique")
    
    assert cart_node.resource_limits == {"cpu": "300m", "memory": "256Mi"}
    assert cart_node.replicas == 1
    assert 7070 in cart_node.ports


def test_parser_creates_dependency_edge_from_env_ref():
    nodes, edges = parse_helm_dir(FIXTURE_DIR, namespace="boutique")
    frontend_node = next(n for n in nodes if n.id == "frontend.boutique")
    
    assert "cartservice.boutique" in frontend_node.dependencies
    edge = next(e for e in edges if e.from_node == "frontend.boutique" and e.to_node == "cartservice.boutique")
    assert edge.port == 7070
    assert edge.protocol == "TCP"


def test_parser_handles_missing_resource_limits_gracefully():
    nodes, _ = parse_helm_dir(FIXTURE_DIR, namespace="boutique")
    catalog_node = next(n for n in nodes if n.id == "productcatalogservice.boutique")
    
    assert catalog_node.resource_limits == {}
    assert 3550 in catalog_node.ports


def test_parser_nonexistent_dir_returns_empty():
    nodes, edges = parse_helm_dir("/non/existent/path/for/sure")
    assert nodes == []
    assert edges == []


def test_parser_resolves_configmap_env_and_real_boutique_chart(tmp_path):
    """Static parser resolves ConfigMap refs and parses the real testbeds/boutique chart."""
    (tmp_path / "manifests.yaml").write_text(
        """
apiVersion: v1
kind: ConfigMap
metadata:
  name: web-env
data:
  CART_SVC: cartservice:7070
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: frontend
spec:
  replicas: 1
  template:
    spec:
      containers:
        - name: server
          ports:
            - containerPort: 8080
          envFrom:
            - configMapRef:
                name: web-env
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: cartservice
spec:
  replicas: 1
  template:
    spec:
      containers:
        - name: server
          ports:
            - containerPort: 7070
""",
        encoding="utf-8",
    )

    nodes, edges = parse_helm_dir(str(tmp_path), namespace="otel-demo")
    edge_pairs = {(e.from_node, e.to_node) for e in edges}
    assert ("frontend.otel-demo", "cartservice.otel-demo") in edge_pairs

    boutique_chart = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "testbeds", "boutique", "chart", "templates")
    )
    real_nodes, real_edges = parse_helm_dir(boutique_chart, namespace="boutique")
    real_ids = {n.id for n in real_nodes}
    assert {"frontend.boutique", "cartservice.boutique", "redis-cart.boutique"}.issubset(real_ids)
    assert any(
        e.from_node == "cartservice.boutique" and e.to_node == "redis-cart.boutique"
        for e in real_edges
    )
