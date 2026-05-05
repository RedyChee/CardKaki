# CardKaki documentation

Engineering documentation for CardKaki. The project [`README.md`](../README.md) is the pitch and minimum-viable-getting-started; this folder is where the depth lives.

- **[architecture.md](architecture.md)** — system overview, module map, the rule engine, period and posting-date models, pool & cap tracking, data model, invariants.
- **[decisions.md](decisions.md)** — numbered ADR-style log of the load-bearing design decisions and their rationale.
- **[operations.md](operations.md)** — Railway deployment, backups, exit strategy.
- **[roadmap.md](roadmap.md)** — v1–v4 history with ship gates and remaining work.

Cross-links inside `docs/` use relative paths (`[#3](decisions.md#3-approach-b-for-posting-date-threading)`). Source-of-truth for technical claims is the code in `cardkaki/`; this folder explains the *shape* and *rationale*, not every line.
