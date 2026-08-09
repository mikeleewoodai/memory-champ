"""memory-agent — a CoALA-based memory service for agent orchestrations.

Three long-term memory modules (episodic, semantic, procedural) plus working
memory, exposed over MCP as nine tools, with an independent daemon that runs the
CoALA decision cycle over the store itself.

No grounding actions: this package never reaches the network, never touches the
filesystem outside its database, and never talks to a user.

    from memory_agent import MemoryService, Policy

    svc = MemoryService(Policy.load("policy.yaml"))
    svc.remember(scope="acme.crm", type="semantic", content="Acme wants PDF invoices.")
    print(svc.recall(scope="acme.crm", query="invoice format")["context_block"])

See docs/memory-agent-coala-spec.md for the full contract.
"""

from .config import Policy
from .errors import MemoryAgentError
from .service import MemoryService

__version__ = "1.0.0"
__all__ = ["MemoryService", "Policy", "MemoryAgentError", "__version__"]
