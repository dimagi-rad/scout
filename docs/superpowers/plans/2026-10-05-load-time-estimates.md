# Load time estimates implementation plan

**Goal:** Show approximate workspace loading times based only on successful history.

**Architecture:** A workspace timing ledger retains whole-load and accumulated phase durations independently of tenant schema retirement. Its workspace/success/start index bounds historical reads to ten rows. Historical successful chat jobs provide whole-load samples between the earliest run and the resume claim; failed/partial run groups are excluded. Optional estimate fields preserve old API clients. Formatting stays pure and polling supplies elapsed time.

**Tech stack:** Django async ORM, PostgreSQL, React, TypeScript, pytest, Vitest.

- [ ] Add failing tests for no history, median/outliers, failures, workspace boundaries, bounded history, phase accumulation, and historical samples.
- [ ] Add the timing model, migration and computation/recording service.
- [ ] Record start, phase transitions and successful completion through workspace model publication; enrich active job and workspace load responses.
- [ ] Add failing pure formatting tests and banner tests for both surfaces and answer suppression.
- [ ] Implement optional API types and approximate timing text without a client countdown.
- [ ] Run affected tests in a disposable Postgres, Python lint/format, frontend install/lint/build/tests.
- [ ] Commit, push, open PR, verify review summary, obtain independent adversarial review, resolve findings, merge and verify deployment.
