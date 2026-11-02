### Fixed

- `dockerfile_locking` (IEBP007) no longer warns about a `git clone` that the same `RUN` then pins to a full commit with `git checkout`, `git switch` or `git reset` on the cloned directory (through `cd <dir>` or `git -C <dir>`), with `git checkout FETCH_HEAD` after a `git fetch` of the commit, or with `git clone --revision=<commit>`. Before, only a commit among the clone's own arguments counted, so the usual `git clone <url> && cd <dir> && git checkout <commit>` was reported as unpinned. A branch, tag or short hash, a checkout of some other directory and a `fetch` alone still warn.

### Added

- `dockerfile_locking` (IEBP007) accepts `ADD <url>.git#<commit> <dir>`, where BuildKit clones the repository at a full commit without git in the image, and `--checksum=<commit>` on such an `ADD`. An `ADD` of a Git repository at a branch, tag or short hash now warns as a moving input instead of being reported as an unverifiable remote source. The hint on an unpinned `git clone` names both accepted forms.
