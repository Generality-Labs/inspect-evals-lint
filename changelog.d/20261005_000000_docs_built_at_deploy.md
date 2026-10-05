### Changed

- The pages that list every rule are built with the documentation site instead of committed: the rule index, the front page, `llms.txt`, `llms-full.txt` and `rules.json`. Every new rule changed them, so any two pull requests that added rules conflicted on them. `docs/rules/` and the README's configuration table are still generated and committed. `docs/CHECKS.md` now points at the site's [rule index](https://inspect-evals-lint.generality.org/CHECKS/), for the link earlier releases print.
- The CLI's "More info about these checks" link goes to the site's rule index instead of `docs/CHECKS.md` on GitHub.
