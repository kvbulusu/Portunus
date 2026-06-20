"""
dependency_manager.py — Block dependency graph for the JANUS ASIC flow.

Defines which JANUS blocks depend on which, tracks per-block status, and tells
the orchestrator which blocks are ready to run (all deps done) so independent
blocks can run in parallel.

Dependency model (logical build order for the hierarchical SoC assembly):

    security        : no deps           (leaf)
    noc             : no deps           (leaf)
    ampo            : security          (lives in agent power domain)
    agent_domain    : security, noc
    user_domain     : security, noc
    janus_soc_top   : security, noc, ampo, agent_domain, user_domain

Status values: PENDING -> RUNNING -> {DONE | FAILED}. A block whose dependency
FAILED becomes BLOCKED (its dependents are aborted, never silently "passed").

This module has NO external dependencies (pure stdlib) so it is trivially
unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Set


class Status(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"   # a dependency failed; this block cannot run


# The canonical dependency graph. Keys are block names; values are the set of
# blocks that must reach DONE before the key block may start.
DEFAULT_GRAPH: Dict[str, Set[str]] = {
    "security":      set(),
    "noc":           set(),
    "ampo":          {"security"},
    "agent_domain":  {"security", "noc"},
    "user_domain":   {"security", "noc"},
    "janus_soc_top": {"security", "noc", "ampo", "agent_domain", "user_domain"},
}


@dataclass
class DependencyManager:
    """Tracks block completion and computes ready / parallel sets."""

    graph: Dict[str, Set[str]] = field(default_factory=lambda: {k: set(v) for k, v in DEFAULT_GRAPH.items()})
    status: Dict[str, Status] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Validate the graph references only known blocks and is acyclic.
        for blk, deps in self.graph.items():
            for d in deps:
                if d not in self.graph:
                    raise ValueError(f"Block '{blk}' depends on unknown block '{d}'")
        self._assert_acyclic()
        for blk in self.graph:
            self.status.setdefault(blk, Status.PENDING)

    # ---- graph validation ---------------------------------------------------
    def _assert_acyclic(self) -> None:
        visited: Dict[str, int] = {}  # 0=visiting, 1=done

        def visit(node: str, stack: List[str]) -> None:
            state = visited.get(node)
            if state == 1:
                return
            if state == 0:
                cycle = " -> ".join(stack + [node])
                raise ValueError(f"Dependency cycle detected: {cycle}")
            visited[node] = 0
            for dep in self.graph[node]:
                visit(dep, stack + [node])
            visited[node] = 1

        for n in self.graph:
            visit(n, [])

    # ---- subsetting (for --blocks a,b,c) ------------------------------------
    def restrict_to(self, blocks: List[str]) -> "DependencyManager":
        """Return a new manager containing only `blocks` and the deps among them.

        Dependencies on blocks NOT in the requested subset are dropped (assumed
        already built / not needed for this run). Unknown names raise.
        """
        wanted = set(blocks)
        for b in wanted:
            if b not in self.graph:
                raise ValueError(f"Unknown block '{b}'. Known: {sorted(self.graph)}")
        sub = {b: (self.graph[b] & wanted) for b in wanted}
        return DependencyManager(graph=sub)

    # ---- status transitions -------------------------------------------------
    def mark(self, block: str, status: Status) -> None:
        if block not in self.status:
            raise KeyError(block)
        self.status[block] = status
        if status == Status.FAILED:
            self._propagate_block(block)

    def _propagate_block(self, failed: str) -> None:
        """Mark every (transitive) dependent of `failed` as BLOCKED."""
        changed = True
        while changed:
            changed = False
            for blk, deps in self.graph.items():
                if self.status[blk] in (Status.PENDING,) and (
                    failed in deps or any(self.status[d] in (Status.FAILED, Status.BLOCKED) for d in deps)
                ):
                    self.status[blk] = Status.BLOCKED
                    changed = True

    # ---- scheduling queries -------------------------------------------------
    def ready(self) -> List[str]:
        """Blocks whose deps are all DONE and that are still PENDING."""
        out = []
        for blk, deps in self.graph.items():
            if self.status[blk] != Status.PENDING:
                continue
            if all(self.status[d] == Status.DONE for d in deps):
                out.append(blk)
        return sorted(out)

    def remaining(self) -> List[str]:
        return sorted(b for b, s in self.status.items() if s in (Status.PENDING, Status.RUNNING))

    def all_settled(self) -> bool:
        return all(s in (Status.DONE, Status.FAILED, Status.BLOCKED) for s in self.status.values())

    def summary(self) -> Dict[str, str]:
        return {b: s.value for b, s in self.status.items()}


# ---- tiny self-test (run: python3 dependency_manager.py) --------------------
if __name__ == "__main__":
    dm = DependencyManager()
    assert dm.ready() == ["noc", "security"], dm.ready()
    dm.mark("security", Status.DONE)
    assert "ampo" in dm.ready()              # security done -> ampo ready
    assert "agent_domain" not in dm.ready()  # still needs noc
    dm.mark("noc", Status.DONE)
    assert set(dm.ready()) >= {"ampo", "agent_domain", "user_domain"}
    dm.mark("user_domain", Status.FAILED)
    assert dm.status["janus_soc_top"] == Status.BLOCKED  # dependent aborted
    print("dependency_manager self-test OK:", dm.summary())
