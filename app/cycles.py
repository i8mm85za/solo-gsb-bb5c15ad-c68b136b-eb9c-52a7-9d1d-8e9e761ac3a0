"""注册表循环依赖诊断（只读、纯内存）。

把注册表中的每个已登记包版本视为有向图节点：若版本 ``A@x`` 声明了依赖
``B: 范围``，且注册表中版本 ``B@y`` 满足该范围，则连一条 ``A@x -> B@y``
的边（``A@x`` 的依赖范围命中自身版本时形成自环）。随后用 Tarjan 算法求
强连通分量（SCC）：

* 大小 >= 2 的分量内必然存在循环；
* 大小为 1 且带自环（自依赖）的节点同样视为循环，须一并报告。

本模块只做纯内存计算，不触碰数据库；路由层负责取出注册表并直接返回结果。
"""

from __future__ import annotations

from typing import Any, Dict, List, Set, Tuple

from .semver import Version, VersionError, VersionRange

Registry = Dict[str, Dict[str, Dict[str, str]]]
Node = Tuple[str, str]  # (包名, 版本)
Edge = Tuple[Node, Node, str]  # (源节点, 目标节点, 范围原文)


def _version_sort_key(text: str) -> tuple:
    try:
        return (0, Version.parse(text))
    except (VersionError, ValueError):
        return (1, text)


def _node_sort_key(node: Node) -> tuple:
    return (node[0], _version_sort_key(node[1]))


def _edge_sort_key(edge: Edge) -> tuple:
    source, target, range_text = edge
    return (_node_sort_key(source), _node_sort_key(target), range_text)


def _build_graph(registry: Registry) -> Tuple[List[Node], List[Edge]]:
    """把注册表展开为 (节点列表, 边列表)。

    节点覆盖全部已登记版本（含非法 SemVer 的版本，保证诊断完整）；
    非法版本不参与范围匹配，非法范围（写入时已校验，这里防御性跳过）不产生边。
    """
    nodes: List[Node] = sorted(
        ((name, version) for name, versions in registry.items() for version in versions),
        key=_node_sort_key,
    )
    parsed: Dict[Node, Version] = {}
    for node in nodes:
        try:
            parsed[node] = Version.parse(node[1])
        except (VersionError, ValueError):
            continue

    edges: List[Edge] = []
    for source in nodes:
        name, version = source
        for dep_name, range_text in registry[name][version].items():
            if dep_name not in registry:
                continue
            try:
                rng = VersionRange.parse(range_text)
            except (VersionError, ValueError):
                continue
            for target_version in registry[dep_name]:
                target = (dep_name, target_version)
                candidate = parsed.get(target)
                if candidate is not None and rng.satisfies(candidate):
                    edges.append((source, target, range_text))
    return nodes, edges


def _strongly_connected_components(nodes: List[Node], edges: List[Edge]) -> List[List[Node]]:
    """迭代版 Tarjan：返回全部强连通分量（发现顺序不定，由调用方排序）。"""
    adjacency: Dict[Node, List[Node]] = {node: [] for node in nodes}
    for source, target, _ in edges:
        adjacency[source].append(target)

    index_of: Dict[Node, int] = {}
    lowlink: Dict[Node, int] = {}
    on_stack: Set[Node] = set()
    stack: List[Node] = []
    components: List[List[Node]] = []
    counter = 0

    for root in nodes:
        if root in index_of:
            continue
        # 显式工作栈：(节点, 下一个待访问邻居下标)，避免递归深度限制
        work: List[Tuple[Node, int]] = [(root, 0)]
        while work:
            node, child_index = work[-1]
            if child_index == 0:
                index_of[node] = lowlink[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            descended = False
            neighbors = adjacency[node]
            for i in range(child_index, len(neighbors)):
                neighbor = neighbors[i]
                if neighbor not in index_of:
                    work[-1] = (node, i + 1)
                    work.append((neighbor, 0))
                    descended = True
                    break
                if neighbor in on_stack:
                    lowlink[node] = min(lowlink[node], index_of[neighbor])
            if descended:
                continue
            if lowlink[node] == index_of[node]:
                component: List[Node] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                components.append(component)
            work.pop()
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])
    return components


def build_registry_cycles(registry: Registry) -> Dict[str, Any]:
    """对注册表做循环依赖诊断，返回可直接序列化的响应体。

    无循环时 ``cycles`` 为空列表；组件、组件内节点与边均按
    （包名, SemVer 版本）稳定排序。
    """
    nodes, edges = _build_graph(registry)
    components = _strongly_connected_components(nodes, edges)

    self_loops: Set[Node] = {source for source, target, _ in edges if source == target}
    member_lists = [
        sorted(component, key=_node_sort_key)
        for component in components
        if len(component) > 1 or component[0] in self_loops
    ]
    # 组件按排序后的节点列表（首节点起）稳定排序
    member_lists.sort(key=lambda members: [_node_sort_key(node) for node in members])

    cycles: List[Dict[str, Any]] = []
    for members in member_lists:
        member_set = set(members)
        internal_edges = sorted(
            (edge for edge in edges if edge[0] in member_set and edge[1] in member_set),
            key=_edge_sort_key,
        )
        cycles.append(
            {
                "size": len(members),
                "nodes": [{"name": name, "version": version} for name, version in members],
                "edges": [
                    {
                        "source": {"name": source[0], "version": source[1]},
                        "target": {"name": target[0], "version": target[1]},
                        "range": range_text,
                    }
                    for source, target, range_text in internal_edges
                ],
            }
        )

    return {
        "node_count": len(nodes),
        "edge_count": len(edges),
        "cycle_count": len(cycles),
        "cycles": cycles,
    }
