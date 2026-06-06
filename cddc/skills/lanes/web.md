Web. Find the flaw in the app and reach the flag (a file, an admin route, a DB
row, an SSRF target).

- Recon: fingerprint the stack, enumerate routes/endpoints, read any provided
  source - source review beats blind probing.
- Classify the bug class: injection (SQLi/NoSQLi/template/command), auth/session
  flaws, IDOR/access control, SSRF, deserialization, path traversal, file upload.
- Draft the request/payload precisely.

NOTE: this lane is analysis-only on the remote tier - LIVE exploitation against a
running target is handed to an on-site operator. Locate the vuln, write up the
exact exploit steps/payload, and hand off; don't assume you can fire it from here.
