"""Independent analysis agents and their shared invocation protocol."""

from . import cluster, explorer, extractor, report, reviewer, world
from .base import AgentPipelineError, AgentSpec, compact_nodes, invoke_agent


AGENTS = {
    module.SPEC.name: module.SPEC
    for module in (extractor, cluster, explorer, world, reviewer, report)
}

__all__ = [
    "AGENTS",
    "AgentPipelineError",
    "AgentSpec",
    "cluster",
    "compact_nodes",
    "explorer",
    "extractor",
    "invoke_agent",
    "report",
    "reviewer",
    "world",
]
