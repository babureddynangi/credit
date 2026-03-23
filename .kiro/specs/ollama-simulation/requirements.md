# Requirements Document

## Introduction

A resource-efficient Ollama simulation runner for the credit fraud detection pipeline. The current `run_ollama_sample.py` script hangs due to unbounded sequential LLM calls with no timeouts, no concurrency controls, and no backpressure. This feature replaces it with a controlled runner that limits resource consumption while still exercising the real Ollama (llama3.2) model.

## Glossary

- **Runner**: The simulation script that sends loan applications through the Ollama LLM pipeline
- **Ollama**: Local LLM inference server running llama3.2 at `http://localhost:11434`
- **Batch**: A fixed-size group of applications processed before pausing
- **Concurrency_Limit**: Maximum number of simultaneous in-flight LLM requests
- **Timeout**: Maximum wall-clock seconds allowed for a single LLM call before it is abandoned
- **Backoff**: A wait period inserted between batches to let the Ollama process recover CPU/memory
- **Application**: A synthetic loan application record passed through the 7-stage pipeline
- **Result**: The per-application output row written to the results directory

## Requirements

### Requirement 1: Configurable Run Size

**User Story:** As a developer, I want to control how many applications are simulated, so that I can run small experiments without overloading my machine.

#### Acceptance Criteria

1. THE Runner SHALL accept a `--total` CLI argument specifying the number of applications to process, defaulting to 10.
2. THE Runner SHALL accept a `--batch-size` CLI argument specifying how many applications to process per batch, defaulting to 5.
3. WHEN `--total` is less than 1, THE Runner SHALL exit with a descriptive error message.
4. WHEN `--batch-size` is less than 1 or greater than `--total`, THE Runner SHALL exit with a descriptive error message.

### Requirement 2: Per-Call Timeout

**User Story:** As a developer, I want each LLM call to have a hard timeout, so that a slow or hung Ollama response does not block the entire run indefinitely.

#### Acceptance Criteria

1. THE Runner SHALL accept a `--timeout` CLI argument (seconds, float), defaulting to 30.0.
2. WHEN an Ollama call exceeds the timeout, THE Runner SHALL record the application as a timeout error and continue to the next application.
3. WHEN a timeout occurs, THE Runner SHALL log the application index and elapsed time to stdout.
4. THE Runner SHALL count timeout errors separately from other LLM errors in the final summary.

### Requirement 3: Concurrency Control

**User Story:** As a developer, I want to limit how many LLM requests run simultaneously, so that Ollama is not overwhelmed and my machine stays responsive.

#### Acceptance Criteria

1. THE Runner SHALL accept a `--concurrency` CLI argument specifying the maximum simultaneous LLM calls, defaulting to 1.
2. WHILE the number of in-flight LLM requests equals `--concurrency`, THE Runner SHALL wait before dispatching the next request.
3. THE Runner SHALL use `concurrent.futures.ThreadPoolExecutor` with `max_workers` set to `--concurrency` to enforce the limit.

### Requirement 4: Inter-Batch Backoff

**User Story:** As a developer, I want a configurable pause between batches, so that the Ollama process has time to release memory between bursts.

#### Acceptance Criteria

1. THE Runner SHALL accept a `--batch-pause` CLI argument (seconds, float), defaulting to 2.0.
2. WHEN a batch completes, THE Runner SHALL sleep for `--batch-pause` seconds before starting the next batch.
3. WHEN `--batch-pause` is 0, THE Runner SHALL skip the sleep entirely without error.

### Requirement 5: Retry on Transient Errors

**User Story:** As a developer, I want failed LLM calls to be retried automatically, so that transient Ollama hiccups do not inflate the error count.

#### Acceptance Criteria

1. THE Runner SHALL accept a `--retries` CLI argument specifying the maximum retry attempts per call, defaulting to 2.
2. WHEN an LLM call raises a network or JSON parse error, THE Runner SHALL retry up to `--retries` times with exponential backoff starting at 1 second.
3. WHEN all retries are exhausted, THE Runner SHALL record the application as a permanent error and continue.
4. THE Runner SHALL use the `tenacity` library already present in `requirements.txt` for retry logic.

### Requirement 6: Progress Reporting

**User Story:** As a developer, I want live progress output during the run, so that I can see the simulation is making progress and estimate completion time.

#### Acceptance Criteria

1. THE Runner SHALL print a progress line after every completed batch showing: applications processed, total, elapsed time, throughput (apps/sec), and error count.
2. WHEN the run completes, THE Runner SHALL print a summary table showing operational status counts, fraud label counts, total loan volume, and error breakdown.
3. THE Runner SHALL write results to `results/ollama_run_summary.json` and `results/ollama_run_report.txt` in the same format as the existing script.

### Requirement 7: Graceful Interrupt Handling

**User Story:** As a developer, I want to press Ctrl+C to stop the simulation cleanly, so that partial results are still saved and the process exits without a traceback.

#### Acceptance Criteria

1. WHEN the user sends a keyboard interrupt (SIGINT), THE Runner SHALL stop dispatching new applications.
2. WHEN interrupted, THE Runner SHALL wait for any in-flight LLM calls to finish or timeout.
3. WHEN interrupted, THE Runner SHALL write partial results to the output files and print a summary of what was completed before exiting with code 0.
