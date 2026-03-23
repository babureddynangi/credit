// infrastructure/docker/init_neo4j.cypher
// Seed data for realistic graph-based fraud detection
// Creates persons, loans, defaults, relationships, devices, accounts, and fund flows

// ─── Clean slate ─────────────────────────────────────────────────────────────
MATCH (n) DETACH DELETE n;

// ─── Persons ─────────────────────────────────────────────────────────────────
// Household 1: Johnson family (proxy borrower scenario)
CREATE (sarah:Person {
  person_id: 'PER-0000001',
  full_name: 'Sarah M. Johnson',
  dob: date('1990-05-14'),
  household_id: 'HH-001',
  recent_default: false
})
CREATE (dan:Person {
  person_id: 'PER-0000002',
  full_name: 'Dan A. Johnson',
  dob: date('1988-11-22'),
  household_id: 'HH-001',
  recent_default: true
})

// Household 2: Williams family (related party risk)
CREATE (marcus:Person {
  person_id: 'PER-0000003',
  full_name: 'Marcus T. Williams',
  dob: date('1985-03-08'),
  household_id: 'HH-002',
  recent_default: false
})
CREATE (james:Person {
  person_id: 'PER-0000004',
  full_name: 'James Williams',
  dob: date('1983-07-19'),
  household_id: 'HH-002',
  recent_default: true
})

// Household 3: Clean applicant
CREATE (lisa:Person {
  person_id: 'PER-0000005',
  full_name: 'Lisa K. Chen',
  dob: date('1992-09-30'),
  household_id: 'HH-003',
  recent_default: false
})

// Household 4: Coordinated fraud ring
CREATE (alex:Person {
  person_id: 'PER-0000006',
  full_name: 'Alex R. Martinez',
  dob: date('1991-01-15'),
  household_id: 'HH-004',
  recent_default: true
})
CREATE (maria:Person {
  person_id: 'PER-0000007',
  full_name: 'Maria L. Martinez',
  dob: date('1993-06-28'),
  household_id: 'HH-004',
  recent_default: true
})
CREATE (carlos:Person {
  person_id: 'PER-0000008',
  full_name: 'Carlos Martinez',
  dob: date('1989-12-03'),
  household_id: 'HH-004',
  recent_default: false
})

// Household 5: Identity misuse scenario
CREATE (robert:Person {
  person_id: 'PER-0000009',
  full_name: 'Robert K. Patel',
  dob: date('1987-04-11'),
  household_id: 'HH-005',
  recent_default: false
})
CREATE (unknown:Person {
  person_id: 'PER-0000010',
  full_name: 'Unknown Applicant',
  dob: date('1987-04-11'),
  household_id: 'HH-005',
  recent_default: true
})

// Additional persons for network density
CREATE (tony:Person {
  person_id: 'PER-0000011',
  full_name: 'Tony D. Brooks',
  dob: date('1994-08-20'),
  household_id: 'HH-006',
  recent_default: false
})
CREATE (nina:Person {
  person_id: 'PER-0000012',
  full_name: 'Nina V. Brooks',
  dob: date('1996-02-14'),
  household_id: 'HH-006',
  recent_default: false
})

// ─── Addresses ───────────────────────────────────────────────────────────────
CREATE (addr1:Address {address_id: 'ADDR-001', street: '1234 Elm St', state: 'CA'})
CREATE (addr2:Address {address_id: 'ADDR-002', street: '5678 Oak Ave', state: 'TX'})
CREATE (addr3:Address {address_id: 'ADDR-003', street: '9100 Pine Dr', state: 'FL'})
CREATE (addr4:Address {address_id: 'ADDR-004', street: '2200 Maple Ln', state: 'NY'})
CREATE (addr5:Address {address_id: 'ADDR-005', street: '3300 Cedar Blvd', state: 'CA'})

// ─── Devices ─────────────────────────────────────────────────────────────────
CREATE (dev1:Device {device_id: 'DEV-001', fingerprint: 'fp-884100'})
CREATE (dev2:Device {device_id: 'DEV-002', fingerprint: 'fp-229933'})
CREATE (dev3:Device {device_id: 'DEV-003', fingerprint: 'fp-551177'})
CREATE (dev4:Device {device_id: 'DEV-004', fingerprint: 'fp-443366'})

// ─── Bank Accounts ───────────────────────────────────────────────────────────
CREATE (acct1:Account {account_id: 'ACCT-001', account_hash: 'hash-4421'})
CREATE (acct2:Account {account_id: 'ACCT-002', account_hash: 'hash-7788'})
CREATE (acct3:Account {account_id: 'ACCT-003', account_hash: 'hash-1122'})
CREATE (acct4:Account {account_id: 'ACCT-004', account_hash: 'hash-9900'})
CREATE (acct5:Account {account_id: 'ACCT-005', account_hash: 'hash-5544'})

// ─── Loans (some defaulted) ──────────────────────────────────────────────────
CREATE (loan1:Loan {
  loan_id: 'LOAN-001', amount: 5000, purpose: 'personal',
  status: 'defaulted', default_date: date('2024-01-10')
})
CREATE (loan2:Loan {
  loan_id: 'LOAN-002', amount: 8000, purpose: 'debt_consolidation',
  status: 'defaulted', default_date: date('2023-12-05')
})
CREATE (loan3:Loan {
  loan_id: 'LOAN-003', amount: 45000, purpose: 'business',
  status: 'defaulted', default_date: date('2024-02-01')
})
CREATE (loan4:Loan {
  loan_id: 'LOAN-004', amount: 30000, purpose: 'auto',
  status: 'defaulted', default_date: date('2024-01-20')
})
CREATE (loan5:Loan {
  loan_id: 'LOAN-005', amount: 15000, purpose: 'home_improvement',
  status: 'defaulted', default_date: date('2024-02-15')
})

// ─── Applications ────────────────────────────────────────────────────────────
CREATE (app1:Application {application_id: 'APP-0000001', amount: 5000, purpose: 'personal', submitted: datetime('2024-03-11T09:15:00Z')})
CREATE (app2:Application {application_id: 'APP-0000002', amount: 12000, purpose: 'debt_consolidation', submitted: datetime('2024-03-11T10:30:00Z')})
CREATE (app3:Application {application_id: 'APP-0000003', amount: 3500, purpose: 'personal', submitted: datetime('2024-03-11T11:00:00Z')})
CREATE (app4:Application {application_id: 'APP-0000004', amount: 75000, purpose: 'business', submitted: datetime('2024-03-12T08:00:00Z')})
CREATE (app5:Application {application_id: 'APP-0000005', amount: 25000, purpose: 'education', submitted: datetime('2024-03-12T09:30:00Z')})

// ─── Person → Address (LIVES_AT) ─────────────────────────────────────────────
CREATE (sarah)-[:LIVES_AT]->(addr1)
CREATE (dan)-[:LIVES_AT]->(addr1)
CREATE (marcus)-[:LIVES_AT]->(addr2)
CREATE (james)-[:LIVES_AT]->(addr2)
CREATE (lisa)-[:LIVES_AT]->(addr3)
CREATE (alex)-[:LIVES_AT]->(addr4)
CREATE (maria)-[:LIVES_AT]->(addr4)
CREATE (carlos)-[:LIVES_AT]->(addr4)
CREATE (robert)-[:LIVES_AT]->(addr5)
CREATE (tony)-[:LIVES_AT]->(addr5)

// ─── Person → Device (USES_DEVICE) ───────────────────────────────────────────
// Sarah and Dan share device (proxy borrower signal)
CREATE (sarah)-[:USES_DEVICE]->(dev1)
CREATE (dan)-[:USES_DEVICE]->(dev1)
// Fraud ring shares device
CREATE (alex)-[:USES_DEVICE]->(dev3)
CREATE (maria)-[:USES_DEVICE]->(dev3)
CREATE (carlos)-[:USES_DEVICE]->(dev3)
// Identity misuse: unknown uses Robert's device
CREATE (robert)-[:USES_DEVICE]->(dev4)
CREATE (unknown)-[:USES_DEVICE]->(dev4)
// Clean users
CREATE (lisa)-[:USES_DEVICE]->(dev2)
CREATE (marcus)-[:USES_DEVICE]->(dev2)

// ─── Person → Account (OWNS_ACCOUNT) ────────────────────────────────────────
CREATE (sarah)-[:OWNS_ACCOUNT]->(acct1)
CREATE (dan)-[:OWNS_ACCOUNT]->(acct1)
CREATE (marcus)-[:OWNS_ACCOUNT]->(acct2)
CREATE (james)-[:OWNS_ACCOUNT]->(acct3)
CREATE (lisa)-[:OWNS_ACCOUNT]->(acct4)
CREATE (alex)-[:OWNS_ACCOUNT]->(acct5)
CREATE (maria)-[:OWNS_ACCOUNT]->(acct5)

// ─── Person → Loan defaults (DEFAULTED_ON) ───────────────────────────────────
CREATE (dan)-[:DEFAULTED_ON]->(loan1)
CREATE (james)-[:DEFAULTED_ON]->(loan2)
CREATE (alex)-[:DEFAULTED_ON]->(loan3)
CREATE (maria)-[:DEFAULTED_ON]->(loan4)
CREATE (unknown)-[:DEFAULTED_ON]->(loan5)

// ─── Person ↔ Person relationships (RELATED_TO) ─────────────────────────────
CREATE (sarah)-[:RELATED_TO {type: 'sibling', shared_attributes: ['address', 'device_fingerprint', 'bank_account']}]->(dan)
CREATE (marcus)-[:RELATED_TO {type: 'sibling', shared_attributes: ['address']}]->(james)
CREATE (alex)-[:RELATED_TO {type: 'spouse', shared_attributes: ['address', 'device_fingerprint', 'bank_account']}]->(maria)
CREATE (alex)-[:RELATED_TO {type: 'sibling', shared_attributes: ['address', 'device_fingerprint']}]->(carlos)
CREATE (maria)-[:RELATED_TO {type: 'sibling', shared_attributes: ['address', 'device_fingerprint']}]->(carlos)
CREATE (robert)-[:RELATED_TO {type: 'unknown', shared_attributes: ['device_fingerprint']}]->(unknown)
CREATE (tony)-[:RELATED_TO {type: 'spouse', shared_attributes: ['address']}]->(nina)

// ─── Shared attribute edges (SHARES) — for graph query compatibility ─────────
CREATE (sarah)-[:SHARES {attribute: 'address'}]->(dan)
CREATE (sarah)-[:SHARES {attribute: 'device'}]->(dan)
CREATE (sarah)-[:SHARES {attribute: 'bank_account'}]->(dan)
CREATE (marcus)-[:SHARES {attribute: 'address'}]->(james)
CREATE (alex)-[:SHARES {attribute: 'address'}]->(maria)
CREATE (alex)-[:SHARES {attribute: 'device'}]->(maria)
CREATE (alex)-[:SHARES {attribute: 'bank_account'}]->(maria)
CREATE (alex)-[:SHARES {attribute: 'device'}]->(carlos)
CREATE (robert)-[:SHARES {attribute: 'device'}]->(unknown)

// ─── Application → Person (APPLIED_FOR) ──────────────────────────────────────
CREATE (sarah)-[:APPLIED_FOR]->(app1)
CREATE (marcus)-[:APPLIED_FOR]->(app2)
CREATE (lisa)-[:APPLIED_FOR]->(app3)
CREATE (carlos)-[:APPLIED_FOR]->(app4)
CREATE (robert)-[:APPLIED_FOR]->(app5)

// ─── Fund flow: Application → Account → Account (disbursement transfer) ──────
// Sarah's loan transferred to Dan's account (proxy borrower fund flow)
CREATE (app1)-[:DISBURSED_TO]->(acct1)
CREATE (acct1)-[:TRANSFERRED_TO {amount: 5000, hours_after: 18}]->(acct1)

// Carlos's application fund flow to Alex (fraud ring)
CREATE (app4)-[:DISBURSED_TO]->(acct5)
CREATE (acct5)-[:TRANSFERRED_TO {amount: 75000, hours_after: 6}]->(acct5)

// ─── Indexes for query performance ───────────────────────────────────────────
CREATE INDEX person_id_idx IF NOT EXISTS FOR (p:Person) ON (p.person_id);
CREATE INDEX loan_default_idx IF NOT EXISTS FOR (l:Loan) ON (l.default_date);
CREATE INDEX app_id_idx IF NOT EXISTS FOR (a:Application) ON (a.application_id);
