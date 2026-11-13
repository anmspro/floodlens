"""Unit tests that need no network and no model. Run: python -m pytest -q"""
import networkx as nx
from shapely.geometry import LineString

from floodlens.brief import check_numbers
from floodlens.osm import edge_id, split_line
from floodlens.roads import decide, status_of

TH = {"blocked_min_conf": 0.6, "open_min_conf": 0.6}


def test_split_line_max_length():
    line = LineString([(0, 0), (350, 0)])
    parts = split_line(line, 100, 120)
    assert len(parts) == 4
    assert all(p.length <= 100.0001 for p in parts)
    assert abs(sum(p.length for p in parts) - 350) < 1e-6


def test_split_line_short_kept_whole():
    assert len(split_line(LineString([(0, 0), (110, 0)]), 100, 120)) == 1


def test_edge_id_is_direction_free():
    assert edge_id(5, 2, 0) == edge_id(2, 5, 0) == "2_5_0"


def test_jury_any_water_blocks():
    pair = {"answer": "no", "water_on_road": "none", "confidence": 0.95}
    surf = {"surface": "water", "road_visible": "yes", "confidence": 0.9}
    d = decide(pair, surf)
    assert d["passable"] == "no" and status_of(d, TH) == "blocked"


def test_jury_open_needs_both():
    pair = {"answer": "no", "water_on_road": "none", "confidence": 0.9}
    assert status_of(decide(pair, {"surface": "dry_pavement", "road_visible": "yes", "confidence": 0.8}), TH) == "open"
    assert status_of(decide(pair, {"surface": "trees", "road_visible": "partly", "confidence": 0.8}), TH) == "uncertain"


def test_jury_after_only():
    assert status_of(decide(None, {"surface": "dry_pavement", "road_visible": "yes", "confidence": 0.9}), TH) == "open"
    assert status_of(decide(None, {"surface": "mud_or_debris", "road_visible": "yes", "confidence": 0.9}), TH) == "uncertain"


def test_number_check_strikes_unknown_numbers():
    html, bad = check_numbers("About 120 people, 7 roads [1_2_0_0].", {"120"})
    assert "<del" in html and bad == ["7"]
    assert 'data-seg="1_2_0_0"' in html


def _cutoff_components(edges, blocked, targets):
    H = nx.Graph()
    H.add_nodes_from({n for e in edges for n in e[:2]})
    for u, v in edges:
        if (u, v) not in blocked:
            H.add_edge(u, v)
    return [c for c in nx.connected_components(H) if not c & targets]


def test_cutoff_logic_bridge():
    # 0-1-2-3 chain plus a triangle 3-4-5; target at node 0
    edges = [(0, 1), (1, 2), (2, 3), (3, 4), (4, 5), (5, 3)]
    assert _cutoff_components(edges, set(), {0}) == []
    zones = _cutoff_components(edges, {(2, 3)}, {0})
    assert zones == [{3, 4, 5}]
