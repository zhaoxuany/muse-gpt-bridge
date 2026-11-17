# Third-party licenses

This project depends on the following third-party packages. All are
permissively licensed; none is GPL, AGPL, or LGPL. License texts are
included in each package's distribution; this file summarizes them.

## Direct dependencies

| Package | Pinned version | License |
|---|---|---|
| fastapi | 0.115.6 | MIT |
| uvicorn | 0.34.0 | BSD-3-Clause |
| pydantic | 2.14.3 | MIT |
| openai | 3.52.0 | Apache-2.0 |
| PyJWT | 2.10.1 | MIT |
| pytest | 8.3.4 | MIT |
| httpx | 0.28.1 | BSD-3-Clause |

## Notable indirect dependencies

| Package | License | Note |
|---|---|---|
| starlette | BSD-3-Clause | FastAPI dependency |
| anyio | MIT | FastAPI / OpenAI dependency |
| pydantic-core | MIT | Pydantic dependency |
| jiter | MIT | OpenAI dependency |
| certifi | MPL-2.0 | Weak copyleft, file-level only; used unmodified |
| tqdm | MPL-2.0 AND MIT | Dual-licensed; used unmodified, MIT terms apply |
| sniffio | MIT OR Apache-2.0 | Dual-licensed |
| packaging | Apache-2.0 AND BSD-3-Clause | Dual-licensed |
| distro | Apache-2.0 | OpenAI dependency |
| annotated-types | MIT | Pydantic dependency |
| h11 | MIT | HTTP dependency |
| pluggy, iniconfig, tomli, exceptiongroup | MIT | Pytest dependencies |

## License obligations when redistributing

- MIT / BSD-3-Clause: retain the copyright and license notices.
- Apache-2.0 (openai, distro, packaging): include a copy of the Apache
  License, retain NOTICE attributions where present, and mark modified
  files if you change them.
- MPL-2.0 (certifi, tqdm): only the files themselves are covered; using
  them unmodified does not impose further conditions on this project.

License conclusions were checked against the pinned versions on PyPI.
Before a public release, reinstall from the pinned versions in a clean
environment and re-verify (see the pre-release audit report).
