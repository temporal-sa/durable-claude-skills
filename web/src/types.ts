// Types mirroring the FastAPI contract in agent/server.py.

export interface TransferPlanView {
  source_account: string;
  target_account: string;
  amount_dollars: number;
  fee_dollars: number;
  total_debit_dollars: number;
  valid: boolean;
  problems: string[];
}

// A UI event the agent emits when it starts a transfer that needs approval.
export interface TransferPlanEvent {
  type: "transfer_plan";
  workflow_id: string;
  reference_id: string;
  status: string;
  plan: TransferPlanView | null;
}

export type AgentEvent = TransferPlanEvent;

export interface ChatResponse {
  session_id: string;
  text: string;
  events: AgentEvent[];
}

// Current state of a transfer, used to reconnect the approval card after a reload.
export interface TransferState {
  workflow_id: string;
  reference_id: string;
  status: string;
  plan: TransferPlanView | null;
}

export interface TransferResult {
  status: string;
  reference_id: string;
  plan: {
    source_account: string;
    target_account: string;
    amount_cents: number;
    fee_cents: number;
    total_debit_cents: number;
    valid: boolean;
    problems: string[];
  };
  withdraw_txn_id: string | null;
  deposit_txn_id: string | null;
  refund_txn_id: string | null;
  detail: string;
}

export interface DecisionResponse {
  text: string;
  result: TransferResult;
}

export interface Account {
  account_id: string;
  name: string;
  kind: string;
  status: string;
  balance_dollars: number;
}

// A ledger entry — money a workflow Activity actually moved.
export interface Transaction {
  txn_id: string;
  account_id: string;
  kind: "withdraw" | "deposit" | "refund";
  amount_dollars: number;
  reference_id: string;
  created_at: string;
}

// What we render in the transcript.
export type ChatItem =
  | { kind: "message"; id: string; role: "user" | "assistant"; text: string }
  | {
      kind: "transfer";
      id: string;
      event: TransferPlanEvent;
      // Local lifecycle of the card, updated when the user approves/declines.
      state: "awaiting_approval" | "deciding" | "settled";
      result?: TransferResult;
    };
