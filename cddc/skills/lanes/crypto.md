Crypto. The flag is recovered by breaking or misusing a cipher, not by guessing.

Recon checklist:
- Read every provided file, especially any `*.py` / `*.sage` source and the
  output/params file. The source IS the spec - find exactly how plaintext maps to
  ciphertext and where the weakness is.
- Pull the public parameters (n, e, c for RSA; p, g, etc. for DH/ECC; mode/IV/key
  handling for symmetric).

Classify, then reach for the known attack:
- RSA: small e + small message (cube root), shared/related modulus, common
  factors across moduli (batch-GCD), Wiener / small-d, Hastad broadcast, partial
  key exposure, related-message / Franklin-Reiter, LSB/parity oracle.
- ECC: singular/anomalous curve, small subgroup, invalid-curve, nonce reuse in
  ECDSA.
- Symmetric: ECB cut-and-paste, CBC bit-flip / padding oracle, CTR/OTP nonce or
  keystream reuse, weak/fixed IV.
- Encodings/classical first if it smells easy: base64/32/85, hex, XOR (single +
  repeating-key), substitution, RC4.

Tools here: pycryptodome, sympy, gmpy2, z3 (no Sage on this box - if you need
lattice reduction / poly-GCD over Z_n[x] / heavy curves, say so and escalate).
Write the solver to a file and run it; verify the recovered plaintext decodes to
a real `CDDC{...}` before submitting.
