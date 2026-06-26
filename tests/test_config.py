import pytest
from pydantic import ValidationError

from solfleet.config import Fleet, load_fleet

VALID = {
    "clusters": {
        "devnet": {
            "reference_rpc": "https://api.devnet.solana.com",
            "nodes": [
                {
                    "name": "dev-rpc-1",
                    "role": "rpc",
                    "host": "1.2.3.4",
                    "rpc_url": "http://1.2.3.4:8899",
                }
            ],
        }
    }
}


def test_valid_fleet_parses():
    fleet = Fleet.model_validate(VALID)
    assert fleet.find_node("dev-rpc-1")[0] == "devnet"
    assert fleet.find_node("nope") is None


def test_rpc_node_requires_rpc_url():
    bad = {
        "clusters": {
            "devnet": {
                "reference_rpc": "https://api.devnet.solana.com",
                "nodes": [{"name": "n1", "role": "rpc", "host": "1.2.3.4"}],
            }
        }
    }
    with pytest.raises(ValidationError, match="requires rpc_url"):
        Fleet.model_validate(bad)


def test_validator_requires_identity():
    bad = {
        "clusters": {
            "mainnet": {
                "reference_rpc": "https://api.mainnet-beta.solana.com",
                "nodes": [{"name": "v1", "role": "validator", "host": "1.2.3.4"}],
            }
        }
    }
    with pytest.raises(ValidationError, match="requires identity"):
        Fleet.model_validate(bad)


def test_build_strategy_requires_builder():
    bad = dict(VALID)
    bad["clusters"]["devnet"]["install"] = {"strategy": "build"}
    with pytest.raises(ValidationError, match="requires install.builder"):
        Fleet.model_validate(bad)


def test_duplicate_node_names_rejected():
    bad = {
        "clusters": {
            "devnet": {
                "reference_rpc": "https://api.devnet.solana.com",
                "nodes": [
                    {"name": "n1", "role": "rpc", "host": "a", "rpc_url": "http://a:8899"},
                    {"name": "n1", "role": "rpc", "host": "b", "rpc_url": "http://b:8899"},
                ],
            }
        }
    }
    with pytest.raises(ValidationError, match="duplicate node names"):
        Fleet.model_validate(bad)


def test_example_config_is_valid(tmp_path, monkeypatch):
    import shutil
    from pathlib import Path

    example = Path(__file__).parent.parent / "fleet.example.yaml"
    shutil.copy(example, tmp_path / "fleet.yaml")
    monkeypatch.chdir(tmp_path)
    fleet = load_fleet()
    assert "devnet" in fleet.clusters
    assert fleet.clusters["mainnet"].install.strategy == "build"
