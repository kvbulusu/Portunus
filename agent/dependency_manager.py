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
    # ---- CVA6+Ara core partition macros (L0.5 sub-level) --------------------
    # The flat CVA6+Ara core (ara_integration_wrapper, ~1.00M instances)
    # OOM-killed DETAIL ROUTING at 27.3 GB even on a 28 GB machine (Error 247,
    # user_core (2).log). Per docs/USER_DOMAIN_HIERARCHICAL_METHODOLOGY.md the
    # documented contingency is a MINIMAL 2-way split: harden the CVA6 scalar
    # host and the Ara vector unit as SEPARATE macros, then let
    # ara_integration_wrapper (built with -D JANUS_PARTITIONED) consume both as
    # .lef/.lib/.gds blackboxes. This drops the largest single route job from
    # ~1.00M to the Ara partition (~620K). These two are leaves in the same
    # sense as any hardened block — they only need the logical fabric deps.
    "user_core_cva6": {"security", "noc"},
    # ---- Ara LANE sub-partition macro (L0.25, EARNED by measured OOM) --------
    # The flat user_core_ara (~620K inst) OOM'd in detail route on the 32GB box,
    # earning the lane-level split per the methodology. This leaf hardens ONE
    # `lane` once; ara.sv reuses it 4× when built with JANUS_ARA_LANE_MACRO=1.
    # It is now a HARD dependency of user_core_ara (not opt-in) since the split
    # is required to avoid OOM. Set `export JANUS_ARA_LANE_MACRO=1` in
    # janus_asic_env.sh to enable the macro path in user_core_ara builds.
    "user_core_ara_lane": {"security", "noc"},
    "user_core_ara":  {"security", "noc", "user_core_ara_lane"},
    # user_core = ONE hardened CVA6+Ara core (ara_integration_wrapper). It is a
    # physical sub-block of the user cluster: user_cluster_top instantiates it 2x
    # and consumes its .lef/.lib abstract as a hard macro. Hardening the 2-core
    # cluster flat OOMs synth AND APR, so we build the core first, then the
    # cluster blackboxes it. Post-split, user_core is an ASSEMBLY of its two
    # partition macros (glue + AXI adapt), so it now depends on them being
    # hardened first.
    "user_core":     {"security", "noc", "user_core_cva6", "user_core_ara"},
    "user_domain":   {"security", "noc", "user_core"},
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
    # security + noc done: the two core partitions + ampo + agent_domain are
    # ready. user_core is NOT (needs its partition macros first); user_domain is
    # NOT (needs user_core).
    assert set(dm.ready()) >= {"ampo", "agent_domain",
                               "user_core_cva6", "user_core_ara"}, dm.ready()
    assert "user_core" not in dm.ready()     # needs both partition macros
    assert "user_domain" not in dm.ready()   # needs user_core
    dm.mark("user_core_cva6", Status.DONE)
    assert "user_core" not in dm.ready()     # still needs user_core_ara
    dm.mark("user_core_ara", Status.DONE)
    assert "user_core" in dm.ready(), dm.ready()   # both partitions done
    dm.mark("user_core", Status.DONE)
    assert "user_domain" in dm.ready()       # user_core done -> cluster ready
    # a failed partition macro must block the whole upward chain
    dm.mark("user_domain", Status.FAILED)
    assert dm.status["janus_soc_top"] == Status.BLOCKED  # dependent aborted
    print("dependency_manager self-test OK:", dm.summary())
