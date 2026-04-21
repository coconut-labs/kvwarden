# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in KVWarden, please report it responsibly.

**Email:** patelshrey77@gmail.com

Please include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

We will acknowledge receipt within 48 hours and provide a timeline for resolution.

## Scope

KVWarden is an inference orchestration middleware. Security concerns include:
- Unauthorized access to model endpoints
- Resource exhaustion via tenant isolation bypass
- Data leakage between tenants
- Arbitrary code execution via malformed requests

## Supported Versions

| Version | Supported |
|---------|-----------|
| main    | Yes       |
| < main  | No        |
