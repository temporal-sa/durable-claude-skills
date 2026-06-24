"""Nexus implementation for the money-transfer skill (optional, ``USE_NEXUS``).

Two pieces live here, both only loaded when the Nexus path is enabled:

  * ``MoneyTransferServiceHandler`` — the operation handler. ``start_transfer`` is
    a ``workflow_run_operation``: it starts ``MoneyTransferWorkflow`` and hands
    back a handle, so the operation resolves to the workflow's result. Crucially
    it starts the workflow at the **same deterministic id** the direct path uses
    (``transfer-{reference_id}``), so approval Updates and status/plan queries are
    identical regardless of which path started the workflow.

  * ``StartTransferWorkflow`` — a thin caller workflow. The agent is not itself a
    workflow, and Nexus operations are invoked from workflows, so this is the
    minimal caller: it opens a Nexus client to ``MoneyTransferService`` and starts
    the operation. It mirrors the backing workflow's lifecycle and otherwise does
    nothing — the human-approval gate and the saga stay in
    ``MoneyTransferWorkflow``.

The default (direct) path does not import or use any of this.
"""

from __future__ import annotations

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    import nexusrpc

    from temporalio import nexus

    from skills.money_transfer.client import workflow_id_for
    from skills.money_transfer.nexus_service import (
        NEXUS_ENDPOINT,
        MoneyTransferService,
    )
    from skills.money_transfer.shared import TransferRequest, TransferResult
    from skills.money_transfer.workflow import MoneyTransferWorkflow


@nexusrpc.handler.service_handler(service=MoneyTransferService)
class MoneyTransferServiceHandler:
    @nexus.workflow_run_operation
    async def start_transfer(
        self, ctx: nexus.WorkflowRunOperationContext, request: TransferRequest
    ) -> nexus.WorkflowHandle[TransferResult]:
        # Start the same workflow the direct path starts, at the same id. The
        # task queue defaults to the one this operation is handled on, which is
        # where MoneyTransferWorkflow runs.
        return await ctx.start_workflow(
            MoneyTransferWorkflow.run,
            request,
            id=workflow_id_for(request.reference_id),
        )


@workflow.defn
class StartTransferWorkflow:
    """Caller workflow: the only job is to invoke the Nexus operation.

    It runs for the transfer's lifetime (``execute_operation`` resolves when the
    backing workflow settles), but the agent never waits on it — the agent polls
    and updates the backing ``transfer-{reference_id}`` workflow directly.
    """

    @workflow.run
    async def run(self, request: TransferRequest) -> TransferResult:
        nexus_client = workflow.create_nexus_client(
            service=MoneyTransferService, endpoint=NEXUS_ENDPOINT
        )
        return await nexus_client.execute_operation(
            MoneyTransferService.start_transfer, request
        )
