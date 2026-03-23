-- infrastructure/docker/init_db.sql
-- Credit Fraud Platform schema
-- Compliant with ECOA, FCRA, BSA/AML, NIST AI RMF

-- ─── Extensions ──────────────────────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ─── Persons / Entity Registry ───────────────────────────────────────────────
CREATE TABLE persons (
    person_id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    external_id         TEXT UNIQUE NOT NULL,         -- LOS borrower ID
    full_name           TEXT NOT NULL,
    ssn_hash            TEXT,                         -- SHA-256, never store plain SSN
    dob                 DATE,
    household_id        UUID,
    identity_confidence NUMERIC(5,4),                 -- 0.0–1.0
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_persons_household ON persons(household_id);

-- ─── Loan Applications ───────────────────────────────────────────────────────
CREATE TABLE applications (
    application_id      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    person_id           UUID REFERENCES persons(person_id),
    external_app_id     TEXT UNIQUE NOT NULL,
    loan_amount         NUMERIC(14,2) NOT NULL,
    loan_purpose        TEXT,
    declared_income     NUMERIC(14,2),
    submitted_at        TIMESTAMPTZ NOT NULL,
    bureau_score        INTEGER,
    pd_score            NUMERIC(5,4),
    fraud_score         NUMERIC(5,4),
    proxy_borrower_score NUMERIC(5,4),
    operational_status  TEXT NOT NULL DEFAULT 'pending'
        CHECK (operational_status IN ('pending','approved','manual_review','hold','fraud_escalation','declined')),
    credit_performance  TEXT
        CHECK (credit_performance IN ('current','delinquent','defaulted','charge_off')),
    fraud_label         TEXT DEFAULT 'none'
        CHECK (fraud_label IN ('none','related_party_risk','suspected_proxy_borrower',
                               'suspected_identity_misuse','suspected_coordinated_fraud','confirmed_fraud')),
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_apps_person ON applications(person_id);
CREATE INDEX idx_apps_status ON applications(operational_status);
CREATE INDEX idx_apps_submitted ON applications(submitted_at);

-- ─── Cases (Investigation Units) ─────────────────────────────────────────────
CREATE TABLE cases (
    case_id             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    application_id      UUID REFERENCES applications(application_id),
    case_type           TEXT NOT NULL,
    priority            INTEGER DEFAULT 50,           -- 1 (highest) – 100 (lowest)
    assigned_analyst_id UUID,
    sla_deadline        TIMESTAMPTZ,
    status              TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open','manual_review','in_review','escalated','closed_approved',
                          'closed_declined','closed_fraud')),
    llm_classification  JSONB,
    evidence_bundle     JSONB,
    final_disposition   TEXT,
    disposition_reason  TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW(),
    closed_at           TIMESTAMPTZ
);

CREATE INDEX idx_cases_application ON cases(application_id);
CREATE INDEX idx_cases_status ON cases(status);
CREATE INDEX idx_cases_priority ON cases(priority);

-- ─── Rule Hits ───────────────────────────────────────────────────────────────
CREATE TABLE rule_hits (
    hit_id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    case_id         UUID REFERENCES cases(case_id),
    rule_code       TEXT NOT NULL,
    rule_description TEXT NOT NULL,
    severity        TEXT CHECK (severity IN ('info','warning','critical')),
    evidence_data   JSONB,
    fired_at        TIMESTAMPTZ DEFAULT NOW()
);

-- ─── Model Score History ──────────────────────────────────────────────────────
CREATE TABLE model_scores (
    score_id        UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    application_id  TEXT NOT NULL,                    -- TEXT to match audit logger writes
    model_name      TEXT NOT NULL,
    model_version   TEXT NOT NULL,
    score_value     NUMERIC(5,4) NOT NULL,
    feature_importances JSONB,
    computed_at     TIMESTAMPTZ DEFAULT NOW()
);

-- ─── Adverse Action Reason Codes (ECOA / Reg B compliance) ───────────────────
CREATE TABLE adverse_action_notices (
    notice_id       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    application_id  UUID REFERENCES applications(application_id),
    reason_codes    TEXT[] NOT NULL,                  -- CFPB standard reason codes
    notice_type     TEXT NOT NULL,                    -- 'denial','counter_offer','incomplete'
    delivery_method TEXT,
    delivered_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ─── Audit Trail (immutable, append-only) ────────────────────────────────────
CREATE TABLE audit_trail (
    audit_id        UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    entity_type     TEXT NOT NULL,                    -- 'application','case','model_score', etc.
    entity_id       TEXT NOT NULL,                    -- TEXT to accept both UUID strings and other IDs
    action          TEXT NOT NULL,
    actor_id        TEXT NOT NULL,                    -- human analyst or 'system:model_name'
    actor_type      TEXT CHECK (actor_type IN ('human','model','rule_engine','llm','system')),
    before_state    JSONB,
    after_state     JSONB,
    rationale       TEXT,
    ip_address      INET,
    session_id      TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Make audit trail append-only
CREATE RULE no_update_audit AS ON UPDATE TO audit_trail DO INSTEAD NOTHING;
CREATE RULE no_delete_audit AS ON DELETE TO audit_trail DO INSTEAD NOTHING;

CREATE INDEX idx_audit_entity ON audit_trail(entity_type, entity_id);
CREATE INDEX idx_audit_created ON audit_trail(created_at);

-- ─── LLM Prompt/Output Log (NIST AI RMF traceability) ────────────────────────
CREATE TABLE llm_logs (
    log_id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    case_id         TEXT NOT NULL,                    -- TEXT to match audit logger writes
    agent_name      TEXT NOT NULL,
    model_name      TEXT NOT NULL,
    prompt_hash     TEXT NOT NULL,                    -- SHA-256 of full prompt
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    structured_output JSONB,
    latency_ms      INTEGER,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ─── Related Party Links ──────────────────────────────────────────────────────
CREATE TABLE related_party_links (
    link_id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    person_id_a     UUID REFERENCES persons(person_id),
    person_id_b     UUID REFERENCES persons(person_id),
    link_type       TEXT NOT NULL,
    shared_attributes TEXT[],
    link_strength   NUMERIC(5,4),
    detected_at     TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_links_person_a ON related_party_links(person_id_a);
CREATE INDEX idx_links_person_b ON related_party_links(person_id_b);

-- ─── Monitoring / Drift Snapshots ────────────────────────────────────────────
CREATE TABLE model_monitoring (
    snapshot_id     UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    model_name      TEXT NOT NULL,
    model_version   TEXT NOT NULL,
    metric_name     TEXT NOT NULL,
    metric_value    NUMERIC,
    threshold       NUMERIC,
    alert_triggered BOOLEAN DEFAULT FALSE,
    snapshot_date   DATE NOT NULL DEFAULT CURRENT_DATE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ─── Seed: CFPB Standard Adverse Action Reason Codes ─────────────────────────
CREATE TABLE adverse_action_reason_codes (
    code        TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    category    TEXT NOT NULL
);

INSERT INTO adverse_action_reason_codes VALUES
('1',  'Derogatory credit history', 'credit'),
('2',  'Collection action or judgment', 'credit'),
('3',  'Delinquent past/present credit obligations', 'credit'),
('4',  'Insufficient number of credit references', 'credit'),
('5',  'Unacceptable type of credit references', 'credit'),
('6',  'Unable to verify credit references', 'credit'),
('7',  'Length of residence', 'stability'),
('8',  'Temporary residence', 'stability'),
('9',  'Unable to verify residence', 'stability'),
('10', 'No credit file', 'credit'),
('14', 'Limited credit experience', 'credit'),
('19', 'Excessive obligations in relation to income', 'income'),
('20', 'Unable to verify income', 'income'),
('22', 'Unacceptable payment record on previous mortgage', 'credit'),
('25', 'Number of recent inquiries on credit bureau report', 'credit'),
('RELATED_PARTY_DEFAULT', 'Related party recent default (internal)', 'fraud'),
('PROXY_BORROWER_RISK',   'Suspected proxy borrower pattern (internal)', 'fraud'),
('FUND_FLOW_ANOMALY',     'Disbursement fund flow anomaly (internal)', 'fraud');
