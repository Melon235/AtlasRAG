# AtlasRAG Codex Rules

1. Architecture Manual is the highest authority.

2. Work only inside the current Stage scope.

3. Do not silently change frozen architecture or contracts.

4. Production code must never depend on `tests/` or `benchmarks/`.

5. Do not add benchmark-aware fields to production domain objects.

6. Dependencies must not be stored in LangGraph State.

7. Nodes must not contain SQL, Redis, or HTTP plumbing.

8. Repositories must not auto-commit.

9. Do not catch `Exception` and convert it to `EMPTY` or `None` unless an
   explicit architectural fallback exists.

10. Technical retry belongs below Graph orchestration. Business or semantic
    retry belongs in Graph orchestration.

11. Do not persist Chain-of-Thought.

12. Do not commit secrets, `.env`, model weights, runtime data, benchmark
    datasets, database volumes, logs, or traces.

13. Every Stage must finish with implementation, tests, verification, a Stage
    report, a Git commit, and a GitHub push.

14. A Stage is not complete until the GitHub push succeeds.

15. Never force-push Stage completion.

16. Never automatically merge a Stage into the default branch.

17. Architecture conflict: stop and report instead of redesigning silently.
