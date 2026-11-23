### Fixed

- Python files are now decoded the way the interpreter decodes them. A file saved with a UTF-8 byte order mark was reported as `Could not parse file: invalid non-printable character U+FEFF` by every rule that parses the package, although Python imports such a file. A file with a PEP 263 encoding declaration, such as `# -*- coding: latin-1 -*-`, is now read in the declared encoding ([#116](https://github.com/Generality-Labs/inspect-evals-lint/pull/116)).
