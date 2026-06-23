# BLACK HOLE — the compression doctrine for Claude Code

## The philosophy

Most codebases are gas. Diffuse, sprawling, low-density clouds where capability is spread thin across a hundred files and a dozen folders, where every feature is wrapped in the dead space of indirection it never needed, where the same idea is written four times in four places because nobody could find it the first three. Gas is easy to write and impossible to hold. You cannot fit a gas cloud in your head, and a codebase you cannot fit in your head is a codebase you do not actually control.

A black hole is the opposite. It is the densest object in the universe: maximum mass, minimum volume, all of it collapsed past the point where empty space can survive between the parts. That is the target for every project. Not fewer features. Not less power. The same mass, crushed into a fraction of the volume, until what remains is so dense it bends everything around it.

This is the compression philosophy: small but overwhelmingly powerful. A project where the entire thing fits in one mind at once, where every file is load-bearing, where there is nowhere for a bug to hide because there is no slack left for it to hide in. The kind of codebase a developer opens, reads top to bottom in an afternoon, and closes thinking *that is it? that is the whole thing?* That reaction is the target. Simplicity so total it reads like a magic trick, a power-to-size ratio so far past what anyone expected that it looks like it should not be possible. You are not writing less software. You are writing the same software with all the air pressed out of it.

Compression is not subtraction of capability, and that single sentence is the line between this discipline and vandalism. You are compressing the representation, never the behavior. The program does exactly what it did before, identical at its boundary, and it does it in a tenth of the code. A change that removes behavior is not a smaller program, it is a different and worse one, and it does not count as compression at all. Mass is conserved. Only volume collapses. Hold that distinction and you can be merciless, because the thing you are being merciless toward is empty space, never function.

The collapse is provable or it did not happen. Every act of compression leaves a number behind: folders before and after, files before and after, lines, dependencies, milliseconds. The number is the proof and the only proof. A refactor that feels cleaner but moves no number is a story you told yourself, and stories are not deliverables. Watch the numbers fall. That is the work, and it is the most satisfying part of it.

There is a safety living inside the aggression, and it is the entire reason the aggression is allowed: simplicity is the safest state a codebase can be in. Complexity is the habitat of bugs. Dead code, duplicate logic, deep folder trees, layers of indirection no one fully understands, these are not neutral mass, they are where defects breed and hide. When you collapse them you are not taking a risk, you are demolishing the places risk lives. The cautious engineer who leaves the sprawl untouched to play it safe has it exactly backwards. The sprawl is the danger. The collapse is the safety. So you collapse hard, and the thing that protects you while you do it is not hesitation, it is the discipline of conserving behavior and proving every single step with a number.

A black hole is not a finished object sitting still. It accretes. It grows by pulling new mass inward, and it stays dense while it does, because nothing it takes in is allowed to spread back out into gas. That is what continued development looks like here: the project is never frozen into a final crystalline shape that the next feature has to be smashed apart to extend. It stays dense as it grows, and each new thing is added already compressed. The simplest, densest codebase is also the most extensible one, because there is the least standing between you and the next change, so compression is the provision for continued development, not its enemy. The one carve-out is being honest about which future is real: a seam or a scaffold that a named, active, or scheduled line of work actually needs is load-bearing and stays, while structure built for a future nobody has committed to is gas like any other.

And not all the mass in a repo is gas, or even code. A project also holds matter that the code operates on: downloaded models and weights, datasets, caches, generated artifacts, anything expensive or impossible to regenerate. None of it is part of the collapse. You are not cleaning the models, you are changing the structure of the code that reaches them. Restructure the loaders, the resolvers, the routing, the whole access path, and collapse that code as hard as anything else, while the artifacts themselves, where they sit on disk, and the project's ability to find the ones a user already has are left exactly as they were.

Four things are sacred and survive every collapse. Name them once, and then everything else is gas you are free to crush:

- **The horizon.** The public contract: the API, the file formats, the wire and stream shapes. This is what the universe outside the project sees, and it does not move unless you are explicitly told to move it. The interior collapses freely. The horizon holds.
- **The behavior.** Same inputs, same outputs. The tests are how you know, so they are green before you start and green when you finish, every time, with no exception and no "I will fix it after." If a module you are about to collapse has thin coverage, you write the characterization test first, then collapse into it.
- **The build.** Everything that compiled and ran on every platform still compiles and runs. A faster, smaller project that no longer builds is not a faster, smaller project. It is rubble.
- **The assets.** Downloaded models and weights, datasets, caches, generated artifacts, anything that is not source and is costly or impossible to regenerate. The collapse restructures the code that locates and loads them and never touches the artifacts, their on-disk location, or the project's ability to find the ones already downloaded. A user who pulled a model yesterday still has it working today. Deleting, moving, reformatting, or orphaning those artifacts is not compression and is never in scope.

Everything outside those four is gas. Find it and collapse it.

## What collapses (the universal targets)

The same for every project, in every language, no exceptions. You are driving each toward its floor and pushing speed toward its ceiling, and these two motions are the same motion:

- **Folders, toward flat.** A directory tree is a claim that you have that many genuinely independent modules, and you almost never do. Most folders are organizational theater, a tidy-looking lie about structure that does not exist. Collapse them until every survivor is a real boundary with its own reason to change.
- **Files, toward few.** A file should hold a whole idea, not a shard of one. Ten files that are each twenty lines of the same concept are nine files of pure overhead and a reader forced to keep nine tabs open to understand one thing. Fuse them into the idea they were always trying to be.
- **Lines, toward the fewest that still read clearly.** Same behavior, a fraction of the code. This is the headline number, the one a developer feels in the first thirty seconds, and the one that makes the simplicity visible.
- **Dependencies, toward zero you do not need.** Every dependency is mass you did not write, bytes you ship, build time you pay, version churn you inherit, and attack surface you now own. Cut every one you could replace in an afternoon. Keep every one you would get wrong by hand.
- **Indirection, toward none that is not load-bearing.** Every wrapper, layer, interface, manager, factory, and coordinator is a hop the reader has to make and the machine has to pay. A thing with one implementation and one caller is not abstraction, it is ceremony, and ceremony is gas wearing a suit.
- **Latency and allocation, toward the floor on the hot path.** Fewer allocations, fewer copies, fewer branches, fewer syscalls in the inner loop. The project gets faster as it gets denser. That is not a happy side effect, it is the same act seen from a different angle.

## The levers

Each lever states how hard to collapse and the one boundary the collapse must not cross. The collapse is aggressive. The boundary is absolute. The boundary is the guardrail, and it lives inside the lever, not in a warning somewhere else.

- **Delete, before anything else.** The heaviest lever, and it always swings first. Dead code, unreachable branches, unused exports, commented-out graveyards, one-caller wrappers, abstractions built for a future that never arrived, dependencies you can replace in thirty lines. Rip all of it out before you rewrite a single line, because half of what you were about to carefully refactor should simply cease to exist. Boundary: a real, reachable capability stays, the contract stays, and unused means unused code, never unused data. Never delete a model, weight, dataset, or cache because no code happens to point at it mid-refactor, and never delete a seam a named, active, or scheduled line of work depends on. Speculative scaffolding for a future nobody committed to is gas. Scaffolding for a future that is arriving is structure. Everything else is fair game and most of it is going.
- **Flatten the folders.** Collapse nested trees toward a single level. Dissolve every single-child folder and every `utils`, `helpers`, `common`, and `misc` junk drawer, and push the contents down to the one place that actually uses them. Boundary: a folder earns its life only by being a genuine module that changes for its own reasons. If it is just a label on a pile, delete the label and keep the pile where it is used.
- **Fuse the files.** Merge fragment files into whole ones. The grain is the idea, not the line count, so what is read together lives together. Boundary: do not weld two unrelated ideas into one file to win a count, and do not shatter one idea across many files to feel organized. One idea, one file, at the size the idea actually is.
- **Collapse the indirection.** Inline every single-use wrapper. Delete every interface with exactly one implementation. Fold pass-through layers into the thing they pass through. Kill the managers and factories that exist only to call one other thing. Boundary: a seam survives only where two sides genuinely change for different reasons or at different rates, or where a named, scheduled next step is about to plug into it. Everywhere else the seam is not separation of concerns, it is just distance, and distance is volume.
- **One source of truth.** Every concept, constant, type, and rule lives in exactly one place that the whole project points at. Hunt the duplicates and collapse them to a single definition. Boundary: two things identical today by coincidence, that will diverge tomorrow for real reasons, are not duplicates, and merging them is a future bug dressed as a present win. Collapse what is one thing. Leave alone what is two things briefly wearing the same face.
- **Starve the dependencies.** Treat every line of the manifest as guilty until it earns its mass. Cut or vendor-trim the ones that cost more in bytes, build time, and surface than they return. Boundary: crypto, parsers, allocators, time and date handling, anything subtle enough that your hand-rolled version would be a quieter and worse bug than the library. Own the trivial. Never own the treacherous.
- **Crush the hot path.** Find the inner loop the program spends its life in, and take the work out of it: fewer allocations, fewer copies, fewer branches, fewer syscalls. Better still, do less work rather than the same work faster: cache it, memoize it, compute it once, exit early the moment the answer is known. Boundary: spend nothing on the cold path. Effort poured into code the program almost never runs is effort that moves no number, and a win that moves no number did not happen.
- **Tighten the contracts.** Make illegal states impossible to represent. Push errors from runtime to compile time. Replace defensive runtime checks with types that cannot be wrong in the first place. Boundary: readability. A tight type exists to make the code more obvious, not less. If the type is harder to read than the bug it prevents, you compressed the wrong dimension and you should back it out.
- **Surface every failure.** No silent fallback, no soft-skip, no swallowed error, no test that passes green while testing nothing. A hidden failure is dark mass: weight the project carries that no one can see or account for, which is the single thing a dense project cannot tolerate, because density means everything present is visible and load-bearing. It is visible and it earns its place, or it is gone. This lever has no boundary and no off switch. Always.

## The loop

One lever, one module, fast, then again, and the rhythm never changes:

1. **Baseline.** Count folders, files, lines, dependencies, and the hot-path number. Run the tests and watch them pass. You cannot prove a collapse against a baseline you never took.
2. **Collapse.** Pull a single lever. Keep the diff small enough to read in one sitting, because a diff you cannot read is a diff you cannot trust.
3. **Conserve.** Tests still green, contract untouched, every platform still building, and any already-downloaded models or data still found and loaded. If any of those broke, the collapse was wrong, full stop. Revert it without sentiment and find a different cut.
4. **Measure.** Take the counts again and compute exactly what fell.
5. **Log one line.** The lever, the deltas, behavior conserved. For example: flattened the storage module, minus 9 folders, minus 31 files, minus 4.2k lines, minus 6 dependencies, minus 40ms p99, behavior conserved.

Report the running ledger of what fell, not a paragraph about how it felt. The numbers are the story, and they tell it better than prose.

## The order

1. **Delete across the whole repo first.** The dead code and dead dependencies are the largest and cheapest mass in the building, and clearing them shrinks the surface every later step has to fight through.
2. **Collapse the structure next.** Flatten the folders, fuse the files. Early, because this redraws the map that everything afterward navigates by, and you want the new map drawn before you start the detailed work.
3. **Crush the hottest and riskiest module while you are sharpest.** The piece most central to speed or most likely to break deserves your freshest attention and your steadiest hand.
4. **Then the long tail**, module by module, the same five-step loop on each one.
5. **Cosmetics last, and only if they move a number.** Renaming for taste is not compression unless it makes the code measurably clearer. If it moves no number, it waits.

## Invoke (paste per repo)

```
Apply BLACK HOLE to <repo>. Collapse it toward maximum density: fewest folders, fewest files, fewest lines, fewest dependencies, least indirection, fastest hot path. Aggressive on the representation, absolute on the boundaries below. You are restructuring the code, not the models or data it operates on.

CONSERVE, without exception: the public contract (<API / file formats / wire shapes>), behavior (tests must pass via <test command> before and after, identical results), the build (<platforms> must all still compile and run), and the assets (<downloaded models / weights / datasets / caches and their paths>: never deleted or moved, and existing downloads must still resolve and load after the refactor). Keep any seam a named, scheduled next feature depends on; collapse the rest.

ORDER: delete everything dead across the whole repo, then flatten folders and fuse files, then crush <hottest module>, then the long tail, one lever at a time.

For every collapse, log one line with the deltas (folders, files, LoC, deps, hot-path latency) and confirm behavior conserved. Revert on sight any collapse that breaks the contract, a test, or the build. A win you cannot measure did not happen.
```

The doctrine does not bend for language or domain. A Go runtime, a Rust engine, a Swift app, a Python tool: same philosophy, same targets, same levers, same sacred horizon. The only thing that changes from one black hole to the next is the contract you name before the collapse begins.
