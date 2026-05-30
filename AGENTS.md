We are allergic to enterprise code.
Keep code you write simple, straightforward and minimal.

- Do NOT unnecessarily create inline functions. Small callbacks for threads/frameworks are fine.
- Do NOT unnecessarily include enterprise-like checking against error conditions that would obviously not happen.
- Do NOT unnecessarily create walls of local variable declarations to hold a value for just one use.
- DO follow our existing variable naming pattern. Prefer more readable names to C++ style acronym or single letter names.
- No abstractions until the third use. Don't create a helper, util or base class until you've written the same thing three times, unless a tiny named function makes safety-critical code or tests clearly simpler.
- No defensive coding against impossible internal states. Do handle external inputs, hardware failures, sockets, subprocesses, and files explicitly.
- No enterprise patterns. Avoid dependency injection, factory functions, service layers, and manager classes by default. Limited constructor injection is allowed at hardware/process/time boundaries when it materially improves deterministic tests and stays local.
- Stateful classes are allowed when they own real lifecycle or concurrency state. Don't create them just to group functions.
- Flat over nested. A file with five exported functions beats a class hierarchy. A switch statement beats a strategy pattern.
- No speculative generality. Don't build for hypothetical future requirements.
- Prefer the smaller change, generally.

These are soft rules, but the hard rule is: if you break one of the rules above, you MUST GET APPROVAL.

Our goal is simple, readable, easy to follow code. Follow existing code styles. The code should feel friendly, welcoming. It should feel like it WANTS you to understand.

Run tests with `python3 -m unittest ...`; for example, `python3 -m unittest tests.test_voice_core`.
