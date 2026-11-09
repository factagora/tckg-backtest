# Contract v0 (byte copies)

Byte copies of factagora/tckg-trader `docs/tckg-backtest/v0/` at commit
`a2b2830f97e3fefc79f3ffb1b4f61b195d708fd2` (last change to that folder: `bdacc20`, 2026-11-08).
The prose contract is tckg-trader `docs/tckg-backtest/io-v0.md` (revision v0.3).
v0.3 (2026-11-09): goldens 01-04 re-timed from the hosted ledger (`known_at`, verdict write time); trades unchanged.
Until the tckg-trader change lands, these files are ahead of its `docs/tckg-backtest/v0/`.

Do not edit these files here: change them in tckg-trader and copy again. `tests/test_contract.py` checks that the
schemas and goldens parse and that every golden passes its schema.
