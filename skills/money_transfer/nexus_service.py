"""The Nexus service contract for the money-transfer skill.

This is the *service-to-service* contract, deliberately kept separate from the
implementation. A caller (here, ``StartTransferWorkflow``) imports only this
interface and the shared Pydantic models; it never imports the workflow or the
banking code. That separation is the whole point of Nexus: the skill could live
in another namespace, repository, or team, and callers would still depend on
nothing but this typed contract.

This is a *third* contract layered onto the skill, not a replacement for the
others:

  * ``SKILL.md``                 — the model-facing contract (loaded into the prompt)
  * ``MoneyTransferService``     — this service-to-service contract (Nexus)
  * ``MoneyTransferWorkflow``    — the deterministic implementation

It is only used when ``USE_NEXUS`` is set; the default path starts the workflow
directly and never touches Nexus.
"""

from __future__ import annotations

import nexusrpc

from skills.money_transfer.shared import TransferRequest, TransferResult

#: Name of the Nexus endpoint that routes ``MoneyTransferService`` calls to the
#: worker handling them. Created once with ``temporal operator nexus endpoint
#: create`` (or ``scripts/setup_nexus.py``).
NEXUS_ENDPOINT = "money-transfer-endpoint"


@nexusrpc.service
class MoneyTransferService:
    """Starts a durable money transfer behind a typed Nexus operation.

    ``start_transfer`` is an *asynchronous* operation backed by
    ``MoneyTransferWorkflow``: it returns once the workflow has been started, and
    its result resolves to the workflow's ``TransferResult`` when the transfer
    settles. The human-approval gate still lives in the workflow, reached by a
    direct Update on the deterministic workflow id — Nexus only starts the work.
    """

    start_transfer: nexusrpc.Operation[TransferRequest, TransferResult]
