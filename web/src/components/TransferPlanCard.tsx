import type { ChatItem } from "../types";
import { TemporalMark } from "./TemporalLogo";

const TEMPORAL_UI =
  import.meta.env.VITE_TEMPORAL_UI ?? "http://localhost:8233";

function money(n: number): string {
  return n.toLocaleString(undefined, { style: "currency", currency: "USD" });
}

interface Props {
  item: Extract<ChatItem, { kind: "transfer" }>;
  onDecide: (referenceId: string, approved: boolean) => void;
}

const STATUS_LABEL: Record<string, string> = {
  completed: "Transfer complete",
  declined: "Transfer declined",
  failed: "Transfer failed — funds returned",
  expired: "Transfer expired",
};

export function TransferPlanCard({ item, onDecide }: Props) {
  const { event, state, result } = item;
  const plan = event.plan;
  const workflowUrl = `${TEMPORAL_UI}/namespaces/default/workflows/${event.workflow_id}`;
  const settledStatus = result?.status ?? "";
  const isComplete = settledStatus === "completed";
  const isFailed = settledStatus === "failed" || settledStatus === "expired";

  return (
    <div
      className={`plan-card ${state === "settled" ? `plan-card--${settledStatus}` : ""}`}
    >
      <div className="plan-card__head">
        <TemporalMark size={16} />
        <span className="plan-card__title">
          {state === "settled"
            ? STATUS_LABEL[settledStatus] ?? "Transfer updated"
            : "Confirm transfer"}
        </span>
        <span className="plan-card__pill">
          {state === "settled" ? settledStatus : "awaiting approval"}
        </span>
      </div>

      {plan && (
        <dl className="plan-card__rows">
          <div className="plan-card__row plan-card__row--amount">
            <dt>Amount</dt>
            <dd>{money(plan.amount_dollars)}</dd>
          </div>
          <div className="plan-card__row">
            <dt>From</dt>
            <dd className="mono">{plan.source_account}</dd>
          </div>
          <div className="plan-card__row">
            <dt>To</dt>
            <dd className="mono">{plan.target_account}</dd>
          </div>
          <div className="plan-card__row">
            <dt>Fee</dt>
            <dd>{plan.fee_dollars === 0 ? "No fee" : money(plan.fee_dollars)}</dd>
          </div>
          <div className="plan-card__row plan-card__row--total">
            <dt>Total debited</dt>
            <dd>{money(plan.total_debit_dollars)}</dd>
          </div>
        </dl>
      )}

      {state !== "settled" && (
        <div className="plan-card__actions">
          <button
            className="btn btn--primary"
            disabled={state === "deciding"}
            onClick={() => onDecide(event.reference_id, true)}
          >
            {state === "deciding" ? "Working…" : "Approve transfer"}
          </button>
          <button
            className="btn btn--ghost"
            disabled={state === "deciding"}
            onClick={() => onDecide(event.reference_id, false)}
          >
            Decline
          </button>
        </div>
      )}

      {state === "settled" && result && (
        <div className="plan-card__receipt">
          {isComplete && result.deposit_txn_id && (
            <span className="mono">Confirmation {result.deposit_txn_id}</span>
          )}
          {isFailed && result.refund_txn_id && (
            <span className="mono">Refund {result.refund_txn_id}</span>
          )}
          {isFailed && !result.refund_txn_id && (
            <span>No money left your account.</span>
          )}
        </div>
      )}

      <a className="plan-card__link" href={workflowUrl} target="_blank" rel="noreferrer">
        <span className="mono">{event.workflow_id}</span>
        <span aria-hidden="true">↗</span>
      </a>
    </div>
  );
}
