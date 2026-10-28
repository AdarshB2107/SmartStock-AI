from app.services.replenishment import (
    round_order_qty, evaluate_ground_truth, recommendation_decision,
    is_demand_observation_usable, is_feature_allowed,
)


def test_pack_size_and_moq():
    assert round_order_qty(13, 6, 12) == 18
    assert round_order_qty(0, 6, 12) == 0
    assert round_order_qty(1, 4, 8) == 8


def test_production_rules_for_edge_cases():
    assert not is_demand_observation_usable(1, 1)
    assert is_demand_observation_usable(0, 0)
    assert not is_feature_allowed("2025-03-02", "2025-03-01")
    assert is_feature_allowed("2025-03-01", "2025-03-01")
    assert recommendation_decision("DISCONTINUED", 4, 20, 6, 12)["status"] == "BLOCKED"
    assert recommendation_decision("ACTIVE", -2, 20, 6, 12)["status"] == "BLOCKED"
    assert recommendation_decision("ACTIVE", 4, 20, 6, 12, 10)["status"] == "CAPACITY_EXCEPTION"
    capped = recommendation_decision("ACTIVE", 4, 20, 6, 12, 18)
    assert capped["status"] == "ORDER" and capped["recommended_order_qty"] == 18


def test_ground_truth_fixtures_use_production_rules(tmp_path):
    from app.services.generator import generate_data
    generate_data(n_skus=4, n_stores=2, n_days=10, seed=20261022)
    results = evaluate_ground_truth()
    assert len(results) == 5
    assert all(row["passed"] for row in results)
