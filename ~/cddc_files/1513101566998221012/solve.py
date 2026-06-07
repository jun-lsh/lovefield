#!/usr/bin/env python3
"""
Solver for the `rsa-abc` challenge.

Vulnerability: the two RSA primes are linearly related, q = a*p + b, and a, b
are public. Therefore n = p*q = a*p^2 + b*p, a quadratic in p:

        a*p^2 + b*p - n = 0   ==>   p = (-b + sqrt(b^2 + 4*a*n)) / (2*a)

Because p is an integer root, the discriminant is a perfect square, so we can
recover p exactly with integer arithmetic, then do textbook RSA decryption.
"""

from math import isqrt

# ---- Public values from chall.py output ----
n = 294312631336817645497301082322154554240169094802464958727391665069791072117766147688663795779102482415529910414666073067471219835152624493177079306762140706831
a = 35317
b = 66854
c = 207223624788194872360942285358435084695571853530695552156870002418526375800472563533912325138458477569873878816555043575388996977720183144666404444478261365640
e = 65537


def solve_quadratic_for_p(a, b, n):
    """Return the integer prime p satisfying a*p^2 + b*p - n = 0."""
    disc = b * b + 4 * a * n
    s = isqrt(disc)
    if s * s != disc:
        raise ValueError("discriminant is not a perfect square; assumptions wrong")
    num = -b + s
    if num % (2 * a) != 0:
        raise ValueError("no integer root for p")
    return num // (2 * a)


def main():
    p = solve_quadratic_for_p(a, b, n)
    q = a * p + b

    assert p * q == n, "recovered factors do not multiply to n"

    phi = (p - 1) * (q - 1)
    d = pow(e, -1, phi)
    m = pow(c, d, n)

    flag = m.to_bytes((m.bit_length() + 7) // 8, "big")

    print(f"p    = {p}")
    print(f"q    = {q}")
    print(f"flag = {flag.decode()}")


if __name__ == "__main__":
    main()
