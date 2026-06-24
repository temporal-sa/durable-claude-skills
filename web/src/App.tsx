import { useEffect, useRef, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { decide, fetchAccounts, fetchTransactions, sendMessage } from "./api";
import type { Account, ChatItem, Transaction } from "./types";
import { TemporalLogo, TemporalMark } from "./components/TemporalLogo";
import { TransferPlanCard } from "./components/TransferPlanCard";

let nextId = 0;
const newId = () => `item-${nextId++}`;

const SUGGESTIONS = [
  "What can I do here?",
  "Send $250 from 85-150 to 43-812",
  "Transfer $1,500 from 85-150 to 43-812",
];

function money(n: number): string {
  return n.toLocaleString(undefined, { style: "currency", currency: "USD" });
}

// How each ledger entry reads in the activity feed: a label and a signed amount.
const TXN_LABEL: Record<Transaction["kind"], string> = {
  withdraw: "Sent",
  deposit: "Received",
  refund: "Refunded",
};

function signedMoney(t: Transaction): string {
  const sign = t.kind === "withdraw" ? "−" : "+";
  return `${sign}${money(t.amount_dollars)}`;
}

export default function App() {
  const [items, setItems] = useState<ChatItem[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [transactions, setTransactions] = useState<Transaction[]>([]);
  const [offline, setOffline] = useState(false);

  const scrollRef = useRef<HTMLDivElement>(null);

  // Pull the latest balances and ledger; called on load and after a transfer settles.
  function refreshBank() {
    fetchAccounts()
      .then((a) => {
        setAccounts(a);
        setOffline(false);
      })
      .catch(() => setOffline(true));
    fetchTransactions()
      .then(setTransactions)
      .catch(() => {});
  }

  useEffect(() => {
    refreshBank();
  }, []);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [items, sending]);

  async function handleSend(text: string) {
    const message = text.trim();
    if (!message || sending) return;
    setInput("");
    setItems((prev) => [
      ...prev,
      { kind: "message", id: newId(), role: "user", text: message },
    ]);
    setSending(true);
    try {
      const res = await sendMessage(message, sessionId);
      setSessionId(res.session_id);
      setOffline(false);

      const additions: ChatItem[] = [];
      if (res.text) {
        additions.push({ kind: "message", id: newId(), role: "assistant", text: res.text });
      }
      for (const event of res.events) {
        if (event.type === "transfer_plan" && event.status === "awaiting_approval") {
          additions.push({
            kind: "transfer",
            id: newId(),
            event,
            state: "awaiting_approval",
          });
        }
      }
      setItems((prev) => [...prev, ...additions]);
    } catch {
      setOffline(true);
      setItems((prev) => [
        ...prev,
        {
          kind: "message",
          id: newId(),
          role: "assistant",
          text: "I couldn't reach the service just now. Check that the API is running, then try again.",
        },
      ]);
    } finally {
      setSending(false);
    }
  }

  async function handleDecide(referenceId: string, approved: boolean) {
    if (!sessionId) return;
    setItems((prev) =>
      prev.map((it) =>
        it.kind === "transfer" && it.event.reference_id === referenceId
          ? { ...it, state: "deciding" }
          : it
      )
    );
    try {
      const res = await decide(sessionId, referenceId, approved);
      setItems((prev) => {
        const updated = prev.map((it) =>
          it.kind === "transfer" && it.event.reference_id === referenceId
            ? { ...it, state: "settled" as const, result: res.result }
            : it
        );
        return [
          ...updated,
          { kind: "message", id: newId(), role: "assistant", text: res.text },
        ];
      });
      // Money may have moved — refresh balances and the activity feed.
      refreshBank();
    } catch {
      // A reachable workflow reports its real outcome (the API resolves expired /
      // already-decided to a terminal status), so this catch is a transient
      // failure — the service is unreachable. Re-enable the card and say so,
      // rather than leaving it looking like the tap did nothing.
      setItems((prev) => [
        ...prev.map((it) =>
          it.kind === "transfer" && it.event.reference_id === referenceId
            ? { ...it, state: "awaiting_approval" as const }
            : it
        ),
        {
          kind: "message",
          id: newId(),
          role: "assistant",
          text: "I couldn't reach the service to record that decision. Check that the API and worker are running, then try again.",
        },
      ]);
      setOffline(true);
    }
  }

  const empty = items.length === 0;

  return (
    <div className="app">
      <header className="topbar">
        <TemporalLogo tone="light" />
        <span className="topbar__divider" aria-hidden="true" />
        <span className="topbar__product">money assistant</span>
        <span className="topbar__spacer" />
        <span className={`status ${offline ? "status--off" : "status--on"}`}>
          <span className="status__dot" />
          {offline ? "API offline" : "durable execution on"}
        </span>
      </header>

      <div className="layout">
        <aside className="rail">
          <h2 className="rail__heading">Demo accounts</h2>
          {accounts.length === 0 && (
            <p className="rail__muted">
              {offline ? "Start the API to load balances." : "Loading…"}
            </p>
          )}
          <ul className="account-list">
            {accounts.map((a) => (
              <li key={a.account_id} className="account">
                <div className="account__top">
                  <span className="account__name">{a.name}</span>
                  {a.status !== "active" && (
                    <span className="account__flag">{a.status}</span>
                  )}
                </div>
                <div className="account__bottom">
                  <span className="mono account__id">{a.account_id}</span>
                  <span className="account__balance">{money(a.balance_dollars)}</span>
                </div>
              </li>
            ))}
          </ul>
          <h2 className="rail__heading rail__heading--activity">Recent activity</h2>
          {transactions.length === 0 ? (
            <p className="rail__muted">
              No transfers yet. Approved transfers show up here.
            </p>
          ) : (
            <ul className="txn-list">
              {transactions.map((t) => (
                <li key={t.txn_id} className="txn">
                  <div className="txn__top">
                    <span className="txn__label">{TXN_LABEL[t.kind]}</span>
                    <span className={`txn__amount txn__amount--${t.kind}`}>
                      {signedMoney(t)}
                    </span>
                  </div>
                  <div className="txn__bottom">
                    <span className="mono txn__acct">{t.account_id}</span>
                    <span className="mono txn__ref">{t.reference_id}</span>
                  </div>
                </li>
              ))}
            </ul>
          )}

          <div className="rail__note">
            <p>
              The assistant talks; a Temporal workflow moves the money. Transfers
              pause for your approval and roll back on failure.
            </p>
          </div>
        </aside>

        <main className="chat">
          <div className="transcript" ref={scrollRef}>
            {empty && (
              <div className="empty">
                <TemporalMark size={40} />
                <h1 className="empty__title">Move money, safely.</h1>
                <p className="empty__sub">
                  Ask about a balance, or start a transfer. Nothing moves until you
                  approve it.
                </p>
                <div className="suggestions">
                  {SUGGESTIONS.map((s) => (
                    <button key={s} className="chip" onClick={() => handleSend(s)}>
                      {s}
                    </button>
                  ))}
                </div>
              </div>
            )}

            {items.map((item) =>
              item.kind === "message" ? (
                <div key={item.id} className={`row row--${item.role}`}>
                  {item.role === "assistant" && (
                    <span className="avatar">
                      <TemporalMark size={16} />
                    </span>
                  )}
                  <div className={`bubble bubble--${item.role}`}>
                    {item.role === "assistant" ? (
                      <div className="markdown">
                        <Markdown remarkPlugins={[remarkGfm]}>{item.text}</Markdown>
                      </div>
                    ) : (
                      item.text
                    )}
                  </div>
                </div>
              ) : (
                <div key={item.id} className="row row--assistant">
                  <span className="avatar">
                    <TemporalMark size={16} />
                  </span>
                  <TransferPlanCard item={item} onDecide={handleDecide} />
                </div>
              )
            )}

            {sending && (
              <div className="row row--assistant">
                <span className="avatar">
                  <TemporalMark size={16} className="spin" />
                </span>
                <div className="bubble bubble--assistant thinking">
                  <span /> <span /> <span />
                </div>
              </div>
            )}
          </div>

          <div className="composer">
            <div className="composer__inner">
              <textarea
                className="composer__input"
                placeholder="Send a message…"
                rows={1}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    handleSend(input);
                  }
                }}
              />
              <button
                className="btn btn--primary composer__send"
                disabled={sending || input.trim().length === 0}
                onClick={() => handleSend(input)}
              >
                Send
              </button>
            </div>
          </div>
        </main>
      </div>
    </div>
  );
}
