### Fixed

- `dockerfile_locking` (IEBP007) no longer warns about a `git clone` that the same `RUN` then pins to a full commit with `git checkout`, `git switch`, `git reset` or `git fetch` (including `git -C <dir> ...`), or a `git clone --revision=<commit>`. Before, only a commit among the clone's own arguments counted, so the usual `git clone <url> && cd <dir> && git checkout <commit>` was reported as unpinned. A branch, tag or short hash still warns.
