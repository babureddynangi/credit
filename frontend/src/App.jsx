import { useState, useEffect, useCallback } from "react";

// ─── Mock data for demo (replace with API calls) ──────────────────────────────
const MOCK_CASES = [
  {
    case_id: "C-2024-001",
    applicant_name: "Sarah M. Johnson",
    loan_amount: 5000,
    case_type: "suspected_proxy_borrower",
    status: "manual_review",
    priority: 10,
    fraud_score: 0.74,
    pd_score: 0.31,
    proxy_score: 0.82,
    created_at: "2024-03-11T09:15:00Z",
    sla_deadline: "2024-03-11T13:15:00Z",
    related_party: "Dan A. Johnson (defaulted Jan 2024)",
    key_reasons: [
      "Related party defaulted 47 days before this application",
      "Shared device fingerprint with recent defaulter",
      "Loan disbursement transferred to linked account within 24h",
    ],
    rule_hits: [
      { code: "R001_HOUSEHOLD_RECENT_DEFAULT", severity: "warning", desc: "Related party defaulted within 90 days" },
      { code: "R008_SHARED_DEVICE_WITH_RECENT_DEFAULTER", severity: "critical", desc: "Shared device fingerprint" },
      { code: "R003_FUNDS_TRANSFERRED_TO_RELATED_DEFAULTER", severity: "critical", desc: "Funds to linked defaulter in <24h" },
    ],
    timeline: [
      { date: "2024-01-10", event: "Dan A. Johnson defaulted on $5,000 loan", type: "default" },
      { date: "2024-02-15", event: "Device fingerprint F-8841 last seen on Dan's application", type: "device" },
      { date: "2024-03-11", event: "Sarah Johnson applied for $5,000 (same amount)", type: "application" },
      { date: "2024-03-12", event: "Disbursement initiated to account ending 4421", type: "disbursement" },
      { date: "2024-03-12", event: "Full amount transferred to account held by Dan Johnson", type: "fund_flow" },
    ],
    graph_metrics: { household_defaults: 1, cluster_density: 0.62, path_to_defaulter: 1, fund_flow: true },
    analyst_summary: "High-confidence suspected proxy borrower case. Named applicant shares device fingerprint with recent defaulter and full loan disbursement transferred to defaulter's account within 24 hours. Strong evidence that Dan Johnson is the real beneficiary.",
    missing_evidence: ["Confirm ownership of receiving bank account", "Review OTP delivery confirmation"],
    adverse_action_codes: ["RELATED_PARTY_DEFAULT", "PROXY_BORROWER_RISK", "FUND_FLOW_ANOMALY"],
    recommended_action: "fraud_escalation",
  },
  {
    case_id: "C-2024-002",
    applicant_name: "Marcus T. Williams",
    loan_amount: 12000,
    case_type: "related_party_risk",
    status: "manual_review",
    priority: 25,
    fraud_score: 0.38,
    pd_score: 0.42,
    proxy_score: 0.29,
    created_at: "2024-03-11T10:30:00Z",
    sla_deadline: "2024-03-12T10:30:00Z",
    related_party: "James Williams (defaulted Dec 2023)",
    key_reasons: [
      "Sibling defaulted 75 days before application",
      "Same residential address as defaulted borrower",
      "No shared device or fund flow signals",
    ],
    rule_hits: [
      { code: "R001_HOUSEHOLD_RECENT_DEFAULT", severity: "warning", desc: "Household member defaulted within 90 days" },
    ],
    timeline: [
      { date: "2023-12-05", event: "James Williams defaulted on $8,000 loan", type: "default" },
      { date: "2024-03-11", event: "Marcus Williams applied for $12,000", type: "application" },
    ],
    graph_metrics: { household_defaults: 1, cluster_density: 0.15, path_to_defaulter: 1, fund_flow: false },
    analyst_summary: "Related party risk with moderate concern. Applicant's sibling defaulted 75 days ago. No shared device, no fund flow signals. Appears to be independent application with family credit risk context. Verify income independently.",
    missing_evidence: ["Income verification documents", "Purpose documentation"],
    adverse_action_codes: ["RELATED_PARTY_DEFAULT"],
    recommended_action: "manual_review",
  },
  {
    case_id: "C-2024-003",
    applicant_name: "Lisa K. Chen",
    loan_amount: 3500,
    case_type: "independent_credit_risk",
    status: "open",
    priority: 50,
    fraud_score: 0.08,
    pd_score: 0.28,
    proxy_score: 0.05,
    created_at: "2024-03-11T11:00:00Z",
    sla_deadline: "2024-03-13T11:00:00Z",
    related_party: "None identified",
    key_reasons: ["Credit score 698 — moderate risk", "Income-to-debt ratio within acceptable range"],
    rule_hits: [],
    timeline: [
      { date: "2024-03-11", event: "Lisa Chen applied for $3,500 personal loan", type: "application" },
    ],
    graph_metrics: { household_defaults: 0, cluster_density: 0.0, path_to_defaulter: null, fund_flow: false },
    analyst_summary: "Standard credit risk evaluation. No fraud signals, no related-party concerns. Bureau score 698 with moderate PD. Recommend approval with standard terms.",
    missing_evidence: [],
    adverse_action_codes: [],
    recommended_action: "approved",
  },
];

const SEVERITY_COLORS = {
  critical: "#ef4444",
  warning:  "#f59e0b",
  info:     "#3b82f6",
};

const STATUS_COLORS = {
  manual_review:    { bg: "#fef3c7", text: "#92400e", border: "#fbbf24" },
  fraud_escalation: { bg: "#fee2e2", text: "#991b1b", border: "#f87171" },
  open:             { bg: "#dbeafe", text: "#1e40af", border: "#93c5fd" },
  approved:         { bg: "#d1fae5", text: "#065f46", border: "#6ee7b7" },
  hold:             { bg: "#f3e8ff", text: "#6b21a8", border: "#c084fc" },
};

const CASE_TYPE_LABELS = {
  suspected_proxy_borrower:    "Suspected Proxy Borrower",
  related_party_risk:          "Related Party Risk",
  independent_credit_risk:     "Independent Credit Risk",
  suspected_identity_misuse:   "Suspected Identity Misuse",
  suspected_coordinated_fraud: "Suspected Coordinated Fraud",
};

const EVENT_ICONS = {
  default:     "💥",
  application: "📋",
  device:      "💻",
  disbursement:"💳",
  fund_flow:   "🔴",
};

// ─── Score Bar Component ──────────────────────────────────────────────────────

function ScoreBar({ label, value, thresholds = [0.5, 0.75] }) {
  const pct   = Math.round(value * 100);
  const color = value >= thresholds[1] ? "#ef4444" : value >= thresholds[0] ? "#f59e0b" : "#22c55e";

  return (
    <div style={{ marginBottom: 10 }}>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, marginBottom: 3 }}>
        <span style={{ color: "#94a3b8" }}>{label}</span>
        <span style={{ fontWeight: 700, color }}>{pct}%</span>
      </div>
      <div style={{ background: "#1e293b", borderRadius: 4, height: 6, overflow: "hidden" }}>
        <div style={{ width: `${pct}%`, height: "100%", background: color, borderRadius: 4,
                      transition: "width 0.6s ease" }} />
      </div>
    </div>
  );
}

// ─── Timeline Component ───────────────────────────────────────────────────────

function Timeline({ events }) {
  return (
    <div style={{ position: "relative", paddingLeft: 24 }}>
      <div style={{ position: "absolute", left: 10, top: 0, bottom: 0,
                    width: 2, background: "#1e293b" }} />
      {events.map((ev, i) => (
        <div key={i} style={{ position: "relative", marginBottom: 16 }}>
          <div style={{ position: "absolute", left: -20, top: 2, width: 16, height: 16,
                        borderRadius: "50%",
                        background: ev.type === "fund_flow" || ev.type === "default" ? "#ef4444" : "#334155",
                        border: "2px solid #0f172a",
                        display: "flex", alignItems: "center", justifyContent: "center",
                        fontSize: 8 }}>
            {EVENT_ICONS[ev.type] || "•"}
          </div>
          <div style={{ background: "#1e293b", borderRadius: 6, padding: "8px 12px",
                        borderLeft: `3px solid ${ev.type === "fund_flow" || ev.type === "default" ? "#ef4444" : "#334155"}` }}>
            <div style={{ fontSize: 11, color: "#64748b", marginBottom: 2 }}>{ev.date}</div>
            <div style={{ fontSize: 13, color: ev.type === "fund_flow" ? "#fca5a5" : "#e2e8f0" }}>
              {ev.event}
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

// ─── Case Card Component ──────────────────────────────────────────────────────

function CaseCard({ c, selected, onClick }) {
  const statusStyle = STATUS_COLORS[c.status] || STATUS_COLORS.open;
  const isHighRisk  = c.fraud_score >= 0.65 || c.proxy_score >= 0.7;

  return (
    <div onClick={() => onClick(c)}
      style={{
        background: selected ? "#1e3a5f" : "#0f172a",
        border: selected ? "1px solid #3b82f6" : "1px solid #1e293b",
        borderRadius: 8, padding: "14px 16px", cursor: "pointer",
        marginBottom: 8, transition: "all 0.15s ease",
        boxShadow: selected ? "0 0 0 2px rgba(59,130,246,0.3)" : "none",
      }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 8 }}>
        <div>
          <div style={{ fontSize: 13, fontWeight: 700, color: "#f1f5f9" }}>{c.applicant_name}</div>
          <div style={{ fontSize: 11, color: "#64748b", marginTop: 2 }}>{c.case_id}</div>
        </div>
        <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
          {isHighRisk && <span style={{ fontSize: 10, background: "#450a0a", color: "#fca5a5",
                                         padding: "2px 6px", borderRadius: 4, fontWeight: 700 }}>HIGH RISK</span>}
          <span style={{
            fontSize: 11, padding: "3px 8px", borderRadius: 4, fontWeight: 600,
            background: statusStyle.bg, color: statusStyle.text, border: `1px solid ${statusStyle.border}`
          }}>{c.status.replace("_", " ").toUpperCase()}</span>
        </div>
      </div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div style={{ fontSize: 12, color: "#94a3b8" }}>
          ${c.loan_amount.toLocaleString()} · {CASE_TYPE_LABELS[c.case_type] || c.case_type}
        </div>
        <div style={{ display: "flex", gap: 10 }}>
          {[["F", c.fraud_score], ["P", c.pd_score], ["PB", c.proxy_score]].map(([lbl, val]) => (
            <div key={lbl} style={{ textAlign: "center" }}>
              <div style={{ fontSize: 9, color: "#475569" }}>{lbl}</div>
              <div style={{ fontSize: 11, fontWeight: 700,
                            color: val >= 0.65 ? "#ef4444" : val >= 0.4 ? "#f59e0b" : "#22c55e" }}>
                {Math.round(val * 100)}%
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// ─── Case Detail Panel ────────────────────────────────────────────────────────

function CaseDetail({ c, onDecision }) {
  const [tab, setTab] = useState("evidence");
  const [disposition, setDisposition] = useState("");
  const [notes, setNotes] = useState("");
  const [submitting, setSubmitting] = useState(false);

  if (!c) return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "center",
                  height: "100%", color: "#334155", flexDirection: "column", gap: 12 }}>
      <div style={{ fontSize: 40 }}>🔍</div>
      <div style={{ fontSize: 16, fontWeight: 600 }}>Select a case to review</div>
    </div>
  );

  const handleSubmit = () => {
    if (!disposition) return;
    setSubmitting(true);
    setTimeout(() => {
      onDecision(c.case_id, disposition, notes);
      setSubmitting(false);
      setDisposition("");
      setNotes("");
    }, 600);
  };

  const tabs = ["evidence", "timeline", "scores", "decision"];

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column", overflow: "hidden" }}>

      {/* Header */}
      <div style={{ padding: "20px 24px 0", borderBottom: "1px solid #1e293b" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 12 }}>
          <div>
            <div style={{ fontSize: 20, fontWeight: 800, color: "#f8fafc", letterSpacing: -0.5 }}>
              {c.applicant_name}
            </div>
            <div style={{ fontSize: 13, color: "#64748b", marginTop: 2 }}>
              {c.case_id} · ${c.loan_amount.toLocaleString()} · {new Date(c.created_at).toLocaleDateString()}
            </div>
          </div>
          <div style={{ textAlign: "right" }}>
            <div style={{ fontSize: 12, fontWeight: 700, color: "#f59e0b",
                          background: "#451a03", padding: "4px 10px", borderRadius: 6, marginBottom: 4 }}>
              {CASE_TYPE_LABELS[c.case_type]}
            </div>
            <div style={{ fontSize: 11, color: "#ef4444" }}>
              SLA: {new Date(c.sla_deadline).toLocaleTimeString()}
            </div>
          </div>
        </div>

        {/* Tab Bar */}
        <div style={{ display: "flex", gap: 0 }}>
          {tabs.map(t => (
            <button key={t} onClick={() => setTab(t)}
              style={{
                padding: "8px 16px", fontSize: 13, fontWeight: 600,
                background: "transparent", border: "none", cursor: "pointer",
                color: tab === t ? "#3b82f6" : "#475569",
                borderBottom: `2px solid ${tab === t ? "#3b82f6" : "transparent"}`,
                textTransform: "capitalize",
              }}>
              {t}
            </button>
          ))}
        </div>
      </div>

      {/* Tab Content */}
      <div style={{ flex: 1, overflow: "auto", padding: "20px 24px" }}>

        {tab === "evidence" && (
          <div>
            {/* LLM Summary */}
            <div style={{ background: "#0c1a2e", border: "1px solid #1e3a5f", borderRadius: 8,
                          padding: 16, marginBottom: 20 }}>
              <div style={{ fontSize: 11, fontWeight: 700, color: "#3b82f6",
                            letterSpacing: 1, marginBottom: 8 }}>AI INVESTIGATION SUMMARY</div>
              <div style={{ fontSize: 13, color: "#cbd5e1", lineHeight: 1.6 }}>{c.analyst_summary}</div>
            </div>

            {/* Key Reasons */}
            <div style={{ marginBottom: 20 }}>
              <div style={{ fontSize: 12, fontWeight: 700, color: "#64748b",
                            letterSpacing: 1, marginBottom: 10 }}>KEY RISK FACTORS</div>
              {c.key_reasons.map((r, i) => (
                <div key={i} style={{ display: "flex", gap: 8, marginBottom: 8 }}>
                  <span style={{ color: "#ef4444", flexShrink: 0 }}>▸</span>
                  <span style={{ fontSize: 13, color: "#e2e8f0" }}>{r}</span>
                </div>
              ))}
            </div>

            {/* Rule Hits */}
            {c.rule_hits.length > 0 && (
              <div style={{ marginBottom: 20 }}>
                <div style={{ fontSize: 12, fontWeight: 700, color: "#64748b",
                              letterSpacing: 1, marginBottom: 10 }}>RULE ENGINE HITS</div>
                {c.rule_hits.map((h, i) => (
                  <div key={i} style={{
                    background: "#0f172a", borderRadius: 6, padding: "10px 12px",
                    marginBottom: 6, borderLeft: `3px solid ${SEVERITY_COLORS[h.severity]}`,
                    display: "flex", justifyContent: "space-between", alignItems: "center",
                  }}>
                    <div>
                      <div style={{ fontSize: 11, fontWeight: 700, color: SEVERITY_COLORS[h.severity],
                                    marginBottom: 2 }}>{h.code}</div>
                      <div style={{ fontSize: 12, color: "#94a3b8" }}>{h.desc}</div>
                    </div>
                    <span style={{ fontSize: 10, padding: "2px 6px", borderRadius: 4, fontWeight: 700,
                                   background: `${SEVERITY_COLORS[h.severity]}22`,
                                   color: SEVERITY_COLORS[h.severity], textTransform: "uppercase" }}>
                      {h.severity}
                    </span>
                  </div>
                ))}
              </div>
            )}

            {/* Related Party */}
            <div style={{ marginBottom: 20 }}>
              <div style={{ fontSize: 12, fontWeight: 700, color: "#64748b",
                            letterSpacing: 1, marginBottom: 10 }}>RELATED PARTIES</div>
              <div style={{ background: "#0f172a", borderRadius: 6, padding: "10px 12px" }}>
                <div style={{ fontSize: 13, color: "#fca5a5" }}>{c.related_party}</div>
              </div>
            </div>

            {/* Missing Evidence */}
            {c.missing_evidence.length > 0 && (
              <div>
                <div style={{ fontSize: 12, fontWeight: 700, color: "#64748b",
                              letterSpacing: 1, marginBottom: 10 }}>MISSING EVIDENCE (NEXT STEPS)</div>
                {c.missing_evidence.map((m, i) => (
                  <div key={i} style={{ display: "flex", gap: 8, marginBottom: 6 }}>
                    <span style={{ color: "#f59e0b", flexShrink: 0 }}>○</span>
                    <span style={{ fontSize: 13, color: "#94a3b8" }}>{m}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {tab === "timeline" && (
          <div>
            <div style={{ fontSize: 12, fontWeight: 700, color: "#64748b",
                          letterSpacing: 1, marginBottom: 16 }}>EVENT TIMELINE</div>
            <Timeline events={c.timeline} />
            {c.graph_metrics.fund_flow && (
              <div style={{ background: "#450a0a", border: "1px solid #7f1d1d", borderRadius: 8,
                            padding: 14, marginTop: 16 }}>
                <div style={{ fontSize: 12, fontWeight: 700, color: "#fca5a5", marginBottom: 4 }}>
                  ⚠ FUND FLOW ALERT
                </div>
                <div style={{ fontSize: 13, color: "#fecaca" }}>
                  Loan disbursement traced to related defaulter's account.
                  This is the strongest proxy borrower signal.
                </div>
              </div>
            )}
          </div>
        )}

        {tab === "scores" && (
          <div>
            <div style={{ fontSize: 12, fontWeight: 700, color: "#64748b",
                          letterSpacing: 1, marginBottom: 16 }}>MODEL SCORES</div>
            <ScoreBar label="Fraud Risk Score"            value={c.fraud_score} />
            <ScoreBar label="Probability of Default (PD)" value={c.pd_score} />
            <ScoreBar label="Proxy Borrower Score"         value={c.proxy_score} />

            <div style={{ marginTop: 24 }}>
              <div style={{ fontSize: 12, fontWeight: 700, color: "#64748b",
                            letterSpacing: 1, marginBottom: 12 }}>GRAPH RISK METRICS</div>
              {[
                ["Household Defaults (12mo)", c.graph_metrics.household_defaults, c.graph_metrics.household_defaults > 0 ? "#ef4444" : "#22c55e"],
                ["Cluster Density", `${Math.round(c.graph_metrics.cluster_density * 100)}%`, c.graph_metrics.cluster_density > 0.5 ? "#f59e0b" : "#22c55e"],
                ["Shortest Path to Defaulter", c.graph_metrics.path_to_defaulter ?? "N/A", c.graph_metrics.path_to_defaulter === 1 ? "#ef4444" : "#94a3b8"],
                ["Fund Flow to Defaulter", c.graph_metrics.fund_flow ? "YES" : "No", c.graph_metrics.fund_flow ? "#ef4444" : "#22c55e"],
              ].map(([label, val, color]) => (
                <div key={label} style={{ display: "flex", justifyContent: "space-between",
                                          padding: "10px 0", borderBottom: "1px solid #1e293b" }}>
                  <span style={{ fontSize: 13, color: "#94a3b8" }}>{label}</span>
                  <span style={{ fontSize: 13, fontWeight: 700, color }}>{val}</span>
                </div>
              ))}
            </div>

            {/* Adverse Action Codes */}
            {c.adverse_action_codes.length > 0 && (
              <div style={{ marginTop: 24 }}>
                <div style={{ fontSize: 12, fontWeight: 700, color: "#64748b",
                              letterSpacing: 1, marginBottom: 10 }}>ADVERSE ACTION CODES (ECOA/Reg B)</div>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                  {c.adverse_action_codes.map(code => (
                    <span key={code} style={{ fontSize: 11, padding: "4px 8px",
                                             background: "#1e293b", color: "#94a3b8",
                                             borderRadius: 4, fontFamily: "monospace" }}>
                      {code}
                    </span>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}

        {tab === "decision" && (
          <div>
            <div style={{ fontSize: 12, fontWeight: 700, color: "#64748b",
                          letterSpacing: 1, marginBottom: 16 }}>AI RECOMMENDATION</div>
            <div style={{ background: "#0c1a2e", border: "1px solid #1e3a5f", borderRadius: 8,
                          padding: 14, marginBottom: 24 }}>
              <div style={{ fontSize: 13, color: "#94a3b8", marginBottom: 4 }}>Recommended Action</div>
              <div style={{ fontSize: 18, fontWeight: 800, color: "#f59e0b", textTransform: "uppercase" }}>
                {c.recommended_action.replace("_", " ")}
              </div>
            </div>

            <div style={{ fontSize: 12, fontWeight: 700, color: "#64748b",
                          letterSpacing: 1, marginBottom: 12 }}>ANALYST DISPOSITION</div>

            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8, marginBottom: 16 }}>
              {[
                ["approved",        "✓ Approve",        "#16a34a", "#052e16"],
                ["declined",        "✗ Decline",         "#dc2626", "#450a0a"],
                ["escalated",       "⬆ Escalate",       "#d97706", "#451a03"],
                ["fraud_confirmed", "⚠ Confirm Fraud",  "#7c3aed", "#2e1065"],
              ].map(([val, label, color, bg]) => (
                <button key={val} onClick={() => setDisposition(val)}
                  style={{
                    padding: "12px", borderRadius: 8, cursor: "pointer",
                    border: `2px solid ${disposition === val ? color : "#1e293b"}`,
                    background: disposition === val ? bg : "#0f172a",
                    color: disposition === val ? color : "#475569",
                    fontWeight: 700, fontSize: 13, transition: "all 0.15s",
                  }}>
                  {label}
                </button>
              ))}
            </div>

            <textarea
              placeholder="Analyst notes (required for override)..."
              value={notes}
              onChange={e => setNotes(e.target.value)}
              style={{
                width: "100%", minHeight: 80, padding: 12, borderRadius: 8,
                background: "#0f172a", border: "1px solid #1e293b",
                color: "#e2e8f0", fontSize: 13, resize: "vertical",
                fontFamily: "inherit", boxSizing: "border-box",
              }}
            />

            <button onClick={handleSubmit} disabled={!disposition || submitting}
              style={{
                width: "100%", marginTop: 12, padding: "14px",
                borderRadius: 8, border: "none", cursor: disposition ? "pointer" : "not-allowed",
                background: disposition ? "#3b82f6" : "#1e293b",
                color: disposition ? "#fff" : "#475569",
                fontWeight: 800, fontSize: 14, letterSpacing: 0.5,
                transition: "all 0.15s",
              }}>
              {submitting ? "Submitting..." : "Submit Decision"}
            </button>

            <div style={{ marginTop: 12, fontSize: 11, color: "#334155", textAlign: "center" }}>
              Your decision will be recorded in the immutable audit trail (FCRA compliance).
              All dispositions are logged with analyst ID, timestamp, and reason codes.
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ─── Main App ─────────────────────────────────────────────────────────────────

export default function App() {
  const [cases, setCases]       = useState(MOCK_CASES);
  const [selected, setSelected] = useState(null);
  const [filter, setFilter]     = useState("all");
  const [search, setSearch]     = useState("");
  const [toast, setToast]       = useState(null);

  const showToast = (msg, color = "#22c55e") => {
    setToast({ msg, color });
    setTimeout(() => setToast(null), 3500);
  };

  const handleDecision = (caseId, disposition, notes) => {
    setCases(prev => prev.map(c =>
      c.case_id === caseId
        ? { ...c, status: disposition === "approved" ? "approved"
                         : disposition === "declined" ? "declined"
                         : disposition === "escalated" ? "fraud_escalation"
                         : "closed" }
        : c
    ));
    showToast(`Case ${caseId}: ${disposition.replace("_", " ")} recorded`);
  };

  const filtered = cases.filter(c => {
    const matchFilter = filter === "all" || c.status === filter || c.case_type === filter;
    const matchSearch = !search || c.applicant_name.toLowerCase().includes(search.toLowerCase())
                        || c.case_id.includes(search);
    return matchFilter && matchSearch;
  });

  const stats = {
    total:       cases.length,
    highRisk:    cases.filter(c => c.fraud_score >= 0.65 || c.proxy_score >= 0.7).length,
    review:      cases.filter(c => c.status === "manual_review").length,
    escalated:   cases.filter(c => c.status === "fraud_escalation").length,
  };

  return (
    <div style={{ display: "flex", height: "100vh", background: "#020617",
                  fontFamily: "'IBM Plex Mono', 'JetBrains Mono', 'Courier New', monospace",
                  overflow: "hidden" }}>

      {/* Toast */}
      {toast && (
        <div style={{ position: "fixed", top: 20, right: 20, zIndex: 9999,
                      background: toast.color, color: "#fff",
                      padding: "12px 20px", borderRadius: 8, fontWeight: 700,
                      fontSize: 13, boxShadow: "0 4px 20px rgba(0,0,0,0.5)",
                      animation: "fadeIn 0.2s ease" }}>
          {toast.msg}
        </div>
      )}

      {/* Left Sidebar */}
      <div style={{ width: 340, borderRight: "1px solid #0f172a", display: "flex",
                    flexDirection: "column", background: "#040d1a" }}>

        {/* Logo / Header */}
        <div style={{ padding: "20px 20px 16px", borderBottom: "1px solid #0f172a" }}>
          <div style={{ fontSize: 11, color: "#3b82f6", fontWeight: 700, letterSpacing: 2, marginBottom: 4 }}>
            CREDIT FRAUD PLATFORM
          </div>
          <div style={{ fontSize: 18, fontWeight: 800, color: "#f8fafc", letterSpacing: -0.5 }}>
            Analyst Workbench
          </div>

          {/* Stats Row */}
          <div style={{ display: "flex", gap: 12, marginTop: 14 }}>
            {[
              ["QUEUE",     stats.review,   "#f59e0b"],
              ["HIGH RISK", stats.highRisk, "#ef4444"],
              ["ESCALATED", stats.escalated,"#7c3aed"],
            ].map(([lbl, val, color]) => (
              <div key={lbl} style={{ flex: 1, background: "#0a1628", borderRadius: 6,
                                      padding: "8px 10px", textAlign: "center" }}>
                <div style={{ fontSize: 18, fontWeight: 800, color }}>{val}</div>
                <div style={{ fontSize: 9, color: "#475569", letterSpacing: 1 }}>{lbl}</div>
              </div>
            ))}
          </div>
        </div>

        {/* Search */}
        <div style={{ padding: "12px 16px 8px" }}>
          <input
            placeholder="Search cases or applicants..."
            value={search}
            onChange={e => setSearch(e.target.value)}
            style={{
              width: "100%", padding: "8px 12px", borderRadius: 6,
              background: "#0f172a", border: "1px solid #1e293b",
              color: "#e2e8f0", fontSize: 12, boxSizing: "border-box",
              fontFamily: "inherit",
            }}
          />
        </div>

        {/* Filter Pills */}
        <div style={{ padding: "0 16px 12px", display: "flex", gap: 6, flexWrap: "wrap" }}>
          {["all", "manual_review", "fraud_escalation", "suspected_proxy_borrower"].map(f => (
            <button key={f} onClick={() => setFilter(f)}
              style={{
                padding: "3px 8px", fontSize: 10, borderRadius: 4, cursor: "pointer",
                border: "1px solid", fontWeight: 600, fontFamily: "inherit",
                borderColor: filter === f ? "#3b82f6" : "#1e293b",
                background:  filter === f ? "#1e3a5f" : "transparent",
                color:       filter === f ? "#93c5fd" : "#475569",
              }}>
              {f.replace("_", " ").toUpperCase()}
            </button>
          ))}
        </div>

        {/* Case List */}
        <div style={{ flex: 1, overflow: "auto", padding: "0 12px 12px" }}>
          {filtered.length === 0 ? (
            <div style={{ color: "#334155", fontSize: 13, textAlign: "center", padding: 20 }}>
              No cases match filter
            </div>
          ) : (
            filtered.map(c => (
              <CaseCard key={c.case_id} c={c}
                selected={selected?.case_id === c.case_id}
                onClick={setSelected} />
            ))
          )}
        </div>
      </div>

      {/* Main Panel */}
      <div style={{ flex: 1, overflow: "hidden", background: "#06111f" }}>
        <CaseDetail c={selected} onDecision={handleDecision} />
      </div>

      <style>{`
        * { box-sizing: border-box; }
        ::-webkit-scrollbar { width: 6px; }
        ::-webkit-scrollbar-track { background: #0f172a; }
        ::-webkit-scrollbar-thumb { background: #1e293b; border-radius: 3px; }
        textarea:focus, input:focus { outline: 1px solid #3b82f6; }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(-8px); } to { opacity: 1; transform: translateY(0); } }
      `}</style>
    </div>
  );
}
