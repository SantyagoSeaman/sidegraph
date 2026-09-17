# Feature Specification: payment retries

**Feature Branch**: `003-payment-retries`

## User Scenarios & Testing *(mandatory)*

A payment fails against a downstream outage and the worker retries without synchronizing with
every other in-flight retry.

## Requirements *(mandatory)*

### Functional Requirements

The system MUST retry a failed payment call up to 3 times before landing in a dead-letter state.

## Success Criteria

### Measurable Outcomes

95% of retried payments succeed within 3 attempts during a downstream outage.

## Assumptions

Downstream failures are transient and clear within the retry window.
