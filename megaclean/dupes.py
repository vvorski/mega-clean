"""Group nodes into duplicate and variant clusters. Reports only; deletes never."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from .signature import hamming_hex


@dataclass(frozen=True)
class Node:
    path: str
    size: int
    sig: str
    exif_key: str | None
    phash: str | None
    phash_src: str | None
    width: int | None
    height: int | None
    mtime: str


@dataclass(frozen=True)
class Cluster:
    kind: str                      # "exact" | "variant"
    members: tuple[Node, ...]
    keeper: Node

    @property
    def exact_groups(self) -> tuple[tuple[Node, ...], ...]:
        """Members that are byte-identical to each other, grouped.

        A variant cluster reports as a whole that its members are the same
        photo, which is a heuristic. Any subgroup sharing a signature is a
        stronger claim than that -- provably the same bytes -- and the report
        would otherwise bury it.
        """
        buckets: dict[str, list[Node]] = defaultdict(list)
        for member in self.members:
            buckets[member.sig].append(member)
        groups = [tuple(sorted(g, key=lambda n: n.path))
                  for g in buckets.values() if len(g) > 1]
        groups.sort(key=lambda g: g[0].path)
        return tuple(groups)


class BKTree:
    """Metric tree over Hamming distance, so near-neighbour lookup is not O(n^2)."""

    def __init__(self) -> None:
        self._root: str | None = None
        self._children: dict[str, dict[int, str]] = {}

    def add(self, key: str) -> None:
        if self._root is None:
            self._root, self._children[key] = key, {}
            return
        node = self._root
        while True:
            dist = hamming_hex(key, node)
            if dist == 0:
                return                      # already present
            child = self._children[node].get(dist)
            if child is None:
                self._children[node][dist] = key
                self._children.setdefault(key, {})
                return
            node = child

    def query(self, key: str, max_distance: int) -> list[str]:
        if self._root is None:
            return []
        found: list[str] = []
        stack = [self._root]
        while stack:
            node = stack.pop()
            dist = hamming_hex(key, node)
            if dist <= max_distance:
                found.append(node)
            for edge, child in self._children[node].items():
                if dist - max_distance <= edge <= dist + max_distance:
                    stack.append(child)
        return found


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, item: str) -> str:
        self.parent.setdefault(item, item)
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:      # path compression
            self.parent[item], item = root, self.parent[item]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def choose_keeper(nodes: Sequence[Node]) -> Node:
    """Deterministic: largest pixel area, then bytes, then oldest, then shallowest."""
    def rank(n: Node):
        area = (n.width or 0) * (n.height or 0)
        return (-area, -n.size, n.mtime or "", n.path.count("/"), n.path)
    return min(nodes, key=rank)


def cluster_nodes(nodes: Sequence[Node], *,
                  phash_threshold: int = 6) -> list[Cluster]:
    by_path = {n.path: n for n in nodes}
    uf = _UnionFind()
    for path in by_path:
        uf.find(path)

    for key in ("sig", "exif_key"):
        buckets: dict[str, list[str]] = defaultdict(list)
        for n in nodes:
            value = getattr(n, key)
            if value:
                buckets[value].append(n.path)
        for paths in buckets.values():
            for other in paths[1:]:
                uf.union(paths[0], other)

    # One tree per provenance class: thumbnail and full-image hashes describe
    # the same photo differently and must never be compared to each other.
    for src in ("thumb", "full"):
        members = [n for n in nodes if n.phash and n.phash_src == src]
        if len(members) < 2:
            continue
        tree, owners = BKTree(), defaultdict(list)
        for n in members:
            tree.add(n.phash)
            owners[n.phash].append(n.path)
        for n in members:
            for neighbour in tree.query(n.phash, phash_threshold):
                for path in owners[neighbour]:
                    uf.union(n.path, path)

    groups: dict[str, list[Node]] = defaultdict(list)
    for n in nodes:
        groups[uf.find(n.path)].append(n)

    clusters = []
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda n: n.path)
        kind = "exact" if len({m.sig for m in members}) == 1 else "variant"
        clusters.append(Cluster(kind=kind, members=tuple(members),
                                keeper=choose_keeper(members)))
    clusters.sort(key=lambda c: (c.kind, c.members[0].path))
    return clusters


def nodes_from_rows(rows: Iterable[Mapping]) -> list[Node]:
    """Build Nodes from index rows, dropping anything not yet fingerprinted."""
    nodes = []
    for row in rows:
        if not row["sig"]:
            continue
        nodes.append(Node(
            path=row["path"], size=row["size"], sig=row["sig"],
            exif_key=row["exif_key"], phash=row["phash"],
            phash_src=row["phash_src"], width=row["width"],
            height=row["height"], mtime=row["mtime"] or "",
        ))
    return nodes
