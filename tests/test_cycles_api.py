"""GET /api/v1/registry/cycles 注册表循环依赖诊断的端到端测试。"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def empty_client(tmp_path, monkeypatch):
    """不播种示例注册表的客户端（seed 文件不存在 -> 注册表为空）。"""
    db_path = tmp_path / "empty_audits.db"
    missing_seed = tmp_path / "no_such_seed.json"
    monkeypatch.setenv("AUDIT_DB_PATH", str(db_path))
    monkeypatch.setenv("AUDIT_SEED_PATH", str(missing_seed))

    # 延迟导入，确保环境变量先生效
    from app import database, main as main_module

    monkeypatch.setattr(database, "DEFAULT_DB_PATH", str(db_path))
    monkeypatch.setattr(main_module, "DEFAULT_DB_PATH", str(db_path))
    monkeypatch.setattr(main_module, "SEED_PATH", str(missing_seed))

    with TestClient(main_module.app) as c:
        yield c


def _put_package(client, name, versions):
    resp = client.put(
        f"/api/v1/registry/packages/{name}",
        json={"name": name, "versions": versions},
    )
    assert resp.status_code == 200, resp.text


def _cycles(client):
    resp = client.get("/api/v1/registry/cycles")
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_seed_registry_has_no_cycles(client):
    body = _cycles(client)
    assert body["cycle_count"] == 0
    assert body["cycles"] == []
    # 示例注册表无环，但节点与边照常统计
    assert body["node_count"] > 0
    assert body["edge_count"] > 0


def test_empty_registry_returns_empty_list(empty_client):
    body = _cycles(empty_client)
    assert body == {"node_count": 0, "edge_count": 0, "cycle_count": 0, "cycles": []}


def test_two_package_cycle(client):
    _put_package(client, "cycle-a", [{"version": "1.0.0", "dependencies": {"cycle-b": "^1.0.0"}}])
    _put_package(client, "cycle-b", [{"version": "1.0.0", "dependencies": {"cycle-a": "^1.0.0"}}])

    body = _cycles(client)
    assert body["cycle_count"] == 1
    (cycle,) = body["cycles"]
    assert cycle["size"] == 2
    assert cycle["nodes"] == [
        {"name": "cycle-a", "version": "1.0.0"},
        {"name": "cycle-b", "version": "1.0.0"},
    ]
    assert cycle["edges"] == [
        {
            "source": {"name": "cycle-a", "version": "1.0.0"},
            "target": {"name": "cycle-b", "version": "1.0.0"},
            "range": "^1.0.0",
        },
        {
            "source": {"name": "cycle-b", "version": "1.0.0"},
            "target": {"name": "cycle-a", "version": "1.0.0"},
            "range": "^1.0.0",
        },
    ]


def test_three_node_cycle(client):
    _put_package(client, "trio-a", [{"version": "1.0.0", "dependencies": {"trio-b": "~1.0.0"}}])
    _put_package(client, "trio-b", [{"version": "1.0.0", "dependencies": {"trio-c": ">=1.0.0 <2.0.0"}}])
    _put_package(client, "trio-c", [{"version": "1.0.0", "dependencies": {"trio-a": "=1.0.0"}}])

    body = _cycles(client)
    assert body["cycle_count"] == 1
    (cycle,) = body["cycles"]
    assert cycle["size"] == 3
    assert [node["name"] for node in cycle["nodes"]] == ["trio-a", "trio-b", "trio-c"]
    assert [
        (edge["source"]["name"], edge["target"]["name"]) for edge in cycle["edges"]
    ] == [("trio-a", "trio-b"), ("trio-b", "trio-c"), ("trio-c", "trio-a")]


def test_self_dependency_reported(client):
    _put_package(client, "selfish", [{"version": "1.0.0", "dependencies": {"selfish": "^1.0.0"}}])

    body = _cycles(client)
    assert body["cycle_count"] == 1
    (cycle,) = body["cycles"]
    assert cycle["size"] == 1
    assert cycle["nodes"] == [{"name": "selfish", "version": "1.0.0"}]
    assert cycle["edges"] == [
        {
            "source": {"name": "selfish", "version": "1.0.0"},
            "target": {"name": "selfish", "version": "1.0.0"},
            "range": "^1.0.0",
        }
    ]


def test_self_range_not_matching_own_version_is_not_a_cycle(client):
    # 1.0.0 依赖自身包的 ^2.0.0：只连向 2.0.0，不构成自环，也没有回边
    _put_package(
        client,
        "selfish",
        [
            {"version": "1.0.0", "dependencies": {"selfish": "^2.0.0"}},
            {"version": "2.0.0", "dependencies": {}},
        ],
    )
    assert _cycles(client)["cycles"] == []


def test_dependency_on_missing_package_creates_no_cycle(client):
    _put_package(client, "lonely", [{"version": "1.0.0", "dependencies": {"ghost": "^1.0.0"}}])
    assert _cycles(client)["cycles"] == []


def test_only_internal_edges_reported(client):
    _put_package(
        client,
        "ring-a",
        [{"version": "1.0.0", "dependencies": {"ring-b": "^1.0.0", "core-lib": "^1.0.0"}}],
    )
    _put_package(
        client,
        "ring-b",
        [
            {"version": "1.0.0", "dependencies": {"ring-a": "^1.0.0"}},
            {"version": "1.1.0", "dependencies": {}},
        ],
    )

    body = _cycles(client)
    assert body["cycle_count"] == 1
    (cycle,) = body["cycles"]
    # ring-b@1.1.0 不在环上；ring-a 指向 core-lib 与 ring-b@1.1.0 的边不属于分量内部
    assert cycle["nodes"] == [
        {"name": "ring-a", "version": "1.0.0"},
        {"name": "ring-b", "version": "1.0.0"},
    ]
    assert {
        (edge["source"]["name"], edge["target"]["name"], edge["target"]["version"])
        for edge in cycle["edges"]
    } == {("ring-a", "ring-b", "1.0.0"), ("ring-b", "ring-a", "1.0.0")}


def test_same_package_versions_cycle_sorted_by_semver(client):
    _put_package(
        client,
        "loop",
        [
            {"version": "2.0.0", "dependencies": {"loop": "*"}},
            {"version": "1.0.0", "dependencies": {"loop": "*"}},
        ],
    )

    body = _cycles(client)
    assert body["cycle_count"] == 1
    (cycle,) = body["cycles"]
    assert [node["version"] for node in cycle["nodes"]] == ["1.0.0", "2.0.0"]
    # 自环与互跨边都在分量内部，按源/目标版本稳定排序
    assert [(edge["source"]["version"], edge["target"]["version"]) for edge in cycle["edges"]] == [
        ("1.0.0", "1.0.0"),
        ("1.0.0", "2.0.0"),
        ("2.0.0", "1.0.0"),
        ("2.0.0", "2.0.0"),
    ]


def test_multiple_cycles_sorted_by_first_node(client):
    _put_package(client, "zz-a", [{"version": "1.0.0", "dependencies": {"zz-b": "^1.0.0"}}])
    _put_package(client, "zz-b", [{"version": "1.0.0", "dependencies": {"zz-a": "^1.0.0"}}])
    _put_package(client, "aa-self", [{"version": "1.0.0", "dependencies": {"aa-self": "*"}}])

    body = _cycles(client)
    assert body["cycle_count"] == 2
    assert [cycle["nodes"][0]["name"] for cycle in body["cycles"]] == ["aa-self", "zz-a"]


def test_cycles_endpoint_is_read_only(client):
    before = client.get("/api/v1/registry/packages").json()
    _put_package(client, "cycle-a", [{"version": "1.0.0", "dependencies": {"cycle-b": "^1.0.0"}}])
    _put_package(client, "cycle-b", [{"version": "1.0.0", "dependencies": {"cycle-a": "^1.0.0"}}])
    after_writes = client.get("/api/v1/registry/packages").json()
    assert after_writes != before  # 前置：环已写入注册表

    assert _cycles(client)["cycle_count"] == 1
    _cycles(client)

    # 诊断接口不产生审计记录，也不改动注册表
    assert client.get("/api/v1/registry/packages").json() == after_writes
    audits = client.get("/api/v1/audits").json()
    assert audits["total"] == 0
    assert audits["items"] == []
