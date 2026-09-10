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
    crc: str | None = None
    node_id: str = ""
    parent_id: str | None = None

    @property
    def key(self) -> str:
        """Identity for clustering: two files can share a path in MEGA."""
        return self.node_id or self.path


@dataclass(frozen=True)
class Cluster:
    kind: str                      # "exact" | "identical" | "variant"
    members: tuple[Node, ...]
    keeper: Node

    @property
    def content_groups(self) -> tuple[tuple[Node, ...], ...]:
        """Members partitioned by actual content, singletons included.

        Redundancy exists only inside one of these partitions. A variant match
        says "same photo", which for burst frames or edits is not the same file
        -- treating those as deletable would lose real pictures.
        """
        buckets: dict[str, list[Node]] = defaultdict(list)
        for member in self.members:
            buckets[member.sig or f"crc:{member.crc}" or member.key].append(member)
        groups = [tuple(sorted(g, key=lambda n: (n.path, n.key)))
                  for g in buckets.values()]
        groups.sort(key=lambda g: g[0].path)
        return tuple(groups)

    prefer: tuple[str, ...] = ()
    avoid: tuple[str, ...] = ()

    @property
    def keeper_keys(self) -> frozenset[str]:
        """One keeper per distinct content; everything else is truly redundant."""
        return frozenset(choose_keeper(list(g), self.prefer, self.avoid).key
                         for g in self.content_groups)

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
        groups = [tuple(sorted(g, key=lambda n: (n.path, n.key)))
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


def choose_keeper(nodes: Sequence[Node], prefer: Sequence[str] = (),
                  avoid: Sequence[str] = ()) -> Node:
    """Which copy to keep: largest pixels, then bytes, then oldest, then shallowest.

    `prefer` lists path prefixes of canonical folders, best first. It ranks
    below image quality on purpose -- a bigger version of a photo is the better
    copy wherever it happens to sit -- but above path depth, so a curated
    library beats a shallower auto-sync inbox.

    `avoid` lists folders a folder-level plan is removing. A copy inside one
    of those must not be the survivor, or the file plan and the folder plan
    would contradict each other. It ranks above `prefer`, below quality.
    """
    def preference(n: Node) -> int:
        for i, prefix in enumerate(prefer):
            if n.path.startswith(prefix):
                return i
        return len(prefer)

    def doomed(n: Node) -> int:
        return 1 if any(n.path.startswith(a) for a in avoid) else 0

    def rank(n: Node):
        area = (n.width or 0) * (n.height or 0)
        return (-area, -n.size, doomed(n), preference(n), n.mtime or "",
                n.path.count("/"), n.path, n.key)
    return min(nodes, key=rank)


def _kind_of(members: Sequence[Node]) -> str:
    """How strong is the claim that these are the same file?

    "exact" means their bytes were compared. "identical" means MEGA's stored
    fingerprint matches -- a sparse CRC, right about 79 times in 80 on this
    account, so it is a candidate rather than a proof. "variant" is the
    heuristic tier: same shot, not the same bytes.
    """
    sigs = {m.sig for m in members if m.sig}
    if sigs and len(sigs) == 1 and all(m.sig for m in members):
        return "exact"
    crcs = {m.crc for m in members if m.crc}
    if crcs and len(crcs) == 1 and all(m.crc for m in members) and not sigs:
        return "identical"
    if sigs and len(sigs) > 1:
        return "variant"
    return "identical" if crcs and len(crcs) == 1 else "variant"


def cluster_nodes(nodes: Sequence[Node], *, phash_threshold: int = 6,
                  prefer: Sequence[str] = (),
                  avoid: Sequence[str] = ()) -> list[Cluster]:
    uf = _UnionFind()
    for n in nodes:
        uf.find(n.key)

    for key in ("sig", "crc", "exif_key"):
        buckets: dict[str, list[str]] = defaultdict(list)
        for n in nodes:
            value = getattr(n, key)
            if value:
                buckets[value].append(n.key)
        for keys in buckets.values():
            for other in keys[1:]:
                uf.union(keys[0], other)

    # One tree per provenance class: thumbnail and full-image hashes describe
    # the same photo differently and must never be compared to each other.
    for src in ("thumb", "full"):
        members = [n for n in nodes if n.phash and n.phash_src == src]
        if len(members) < 2:
            continue
        tree, owners = BKTree(), defaultdict(list)
        for n in members:
            tree.add(n.phash)
            owners[n.phash].append(n.key)
        for n in members:
            for neighbour in tree.query(n.phash, phash_threshold):
                for key in owners[neighbour]:
                    uf.union(n.key, key)

    groups: dict[str, list[Node]] = defaultdict(list)
    for n in nodes:
        groups[uf.find(n.key)].append(n)

    clusters = []
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda n: (n.path, n.key))
        kind = _kind_of(members)
        clusters.append(Cluster(kind=kind, members=tuple(members),
                                keeper=choose_keeper(members, prefer, avoid),
                                prefer=tuple(prefer), avoid=tuple(avoid)))
    clusters.sort(key=lambda c: (c.kind, c.members[0].path))
    return clusters


def nodes_from_rows(rows: Iterable[Mapping]) -> list[Node]:
    """Build Nodes from index rows, dropping anything not yet fingerprinted."""
    nodes = []
    for row in rows:
        has_crc = "crc" in row.keys() and row["crc"]
        if not row["sig"] and not has_crc:
            continue
        nodes.append(Node(
            path=row["path"], size=row["size"], sig=row["sig"] or "",
            exif_key=row["exif_key"], phash=row["phash"],
            phash_src=row["phash_src"], width=row["width"],
            height=row["height"], mtime=row["mtime"] or "",
            crc=row["crc"] if "crc" in row.keys() else None,
            node_id=(row["node_id"] if "node_id" in row.keys() else "") or "",
            parent_id=(row["parent_id"] if "parent_id" in row.keys() else None),
        ))
    return nodes
