# Changelog

## [0.7.0](https://github.com/syntropy-systems-oss/windtunnel/compare/v0.6.0...v0.7.0) (2026-07-08)


### Features

* **cli:** wt surface — record/diff/check prompt-surface goldens ([#35](https://github.com/syntropy-systems-oss/windtunnel/issues/35)) ([b469343](https://github.com/syntropy-systems-oss/windtunnel/commit/b4693430ee8ea594ec17af50f7bb6f5fa32633e7))
* **hooks:** lifecycle hooks — scoped context, sidecar artifacts, debrief reference hook ([#37](https://github.com/syntropy-systems-oss/windtunnel/issues/37)) ([429bf65](https://github.com/syntropy-systems-oss/windtunnel/commit/429bf65675a10c55d4dba9fa813c9bfa0539aef9))
* **trace:** capture the prompt surface into run artifacts ([#36](https://github.com/syntropy-systems-oss/windtunnel/issues/36)) ([6b706b5](https://github.com/syntropy-systems-oss/windtunnel/commit/6b706b5c57fab14a6d7d82692efa44f68f01f857))


### Bug Fixes

* **trace:** tool_schema_hash hashes the real tool manifest, not scenario.name ([#33](https://github.com/syntropy-systems-oss/windtunnel/issues/33)) ([115b756](https://github.com/syntropy-systems-oss/windtunnel/commit/115b756feded49848a8cf16c0b13a7548c5e000a))


### Documentation

* **design:** 0002 — optional surface-introspection route, amended while draft ([#32](https://github.com/syntropy-systems-oss/windtunnel/issues/32)) ([15b66b2](https://github.com/syntropy-systems-oss/windtunnel/commit/15b66b2b06e46c1a5b062165d0989b43e96d1652))
* **skill-eval:** first live results — the matrix run on release day ([#29](https://github.com/syntropy-systems-oss/windtunnel/issues/29)) ([9b3c0bd](https://github.com/syntropy-systems-oss/windtunnel/commit/9b3c0bde688f3a1b6ee77b9f9276e078df132592))

## [0.6.0](https://github.com/syntropy-systems-oss/windtunnel/compare/v0.5.0...v0.6.0) (2026-07-06)


### Features

* **runtime:** terminus driver — bench Harbor's Terminus-2 terminal agent ([#26](https://github.com/syntropy-systems-oss/windtunnel/issues/26)) ([f32d626](https://github.com/syntropy-systems-oss/windtunnel/commit/f32d6265a85243fa9ce5e21b59a964fe679b3c7e))
* skill-eval example pack + docker-default isolation for terminus ([#28](https://github.com/syntropy-systems-oss/windtunnel/issues/28)) ([b8fd974](https://github.com/syntropy-systems-oss/windtunnel/commit/b8fd974f474a53317b3a972067b07a74d1e8b23f))
* **skill:** generate agent skill pipeline ([#24](https://github.com/syntropy-systems-oss/windtunnel/issues/24)) ([6ce4740](https://github.com/syntropy-systems-oss/windtunnel/commit/6ce4740b7caf849325ae98956e4bb663b4181486))
* world preconditions, wt rescore, and the reset ordering contract ([#25](https://github.com/syntropy-systems-oss/windtunnel/issues/25)) ([6533a4d](https://github.com/syntropy-systems-oss/windtunnel/commit/6533a4dcb89bf859b6713ac213bf8bc68f310293))


### Documentation

* reorganize user guides and CLI reference ([#22](https://github.com/syntropy-systems-oss/windtunnel/issues/22)) ([ded4212](https://github.com/syntropy-systems-oss/windtunnel/commit/ded421238793289c61bd6efa8d44bab852c64428))
* **terminus:** state the Harbor/Terminus-2 relationship; cap harbor &lt;0.18 ([#27](https://github.com/syntropy-systems-oss/windtunnel/issues/27)) ([41cd011](https://github.com/syntropy-systems-oss/windtunnel/commit/41cd011251923a0f1275b708cd0e488761ad22db))

## [0.5.0](https://github.com/syntropy-systems-oss/windtunnel/compare/v0.4.0...v0.5.0) (2026-07-06)


### Features

* **canary:** reset-isolation canary as a library-level conformance helper ([#18](https://github.com/syntropy-systems-oss/windtunnel/issues/18)) ([09cda97](https://github.com/syntropy-systems-oss/windtunnel/commit/09cda97a355b234f7694f77764b2040bb237eab5))
* **interchange:** Contract A conformance kit — wt validate, golden fixtures, reference emitter ([#17](https://github.com/syntropy-systems-oss/windtunnel/issues/17)) ([e41c710](https://github.com/syntropy-systems-oss/windtunnel/commit/e41c710a989901c38d3b2e37849a54aab7987ef7))
* **runtime:** http_inject built-in speaking the Contract C inject protocol ([#21](https://github.com/syntropy-systems-oss/windtunnel/issues/21)) ([ec90579](https://github.com/syntropy-systems-oss/windtunnel/commit/ec9057995908e2865c50de26356ed7e73e6acf20))


### Bug Fixes

* **ci:** publish to PyPI from the release-please run, not a release event ([#15](https://github.com/syntropy-systems-oss/windtunnel/issues/15)) ([50d6290](https://github.com/syntropy-systems-oss/windtunnel/commit/50d6290b9f6ca08305ff70406561b7fed4ca2863))


### Documentation

* **design:** 0002 — the inject protocol (Contract C) ([#16](https://github.com/syntropy-systems-oss/windtunnel/issues/16)) ([cecce26](https://github.com/syntropy-systems-oss/windtunnel/commit/cecce26de68062bba80b1d229ba3246439af5456))

## [0.4.0](https://github.com/syntropy-systems-oss/windtunnel/compare/v0.3.0...v0.4.0) (2026-07-03)


### Features

* **ci:** junit/json run output and tag/pack/owner/glob selection ([#12](https://github.com/syntropy-systems-oss/windtunnel/issues/12)) ([52c08ae](https://github.com/syntropy-systems-oss/windtunnel/commit/52c08ae5a84b297e626e6f28fd09127a69f5c203))
* **import:** Contract A interchange format and the wt import command ([#14](https://github.com/syntropy-systems-oss/windtunnel/issues/14)) ([67331af](https://github.com/syntropy-systems-oss/windtunnel/commit/67331af85664e7111a8c0280b8679f5434e6f402))
* **ledger:** pack ownership and the append-only run ledger ([#10](https://github.com/syntropy-systems-oss/windtunnel/issues/10)) ([139384f](https://github.com/syntropy-systems-oss/windtunnel/commit/139384f25d2ab402808a974cb33a7f5d231b6059))
* **scorers:** outcome scorer library for Scenario.outcome_fn ([#11](https://github.com/syntropy-systems-oss/windtunnel/issues/11)) ([b0b4753](https://github.com/syntropy-systems-oss/windtunnel/commit/b0b4753aa975de384db4c3160577afa4fd1ae315))
* **universe:** recorded tool-universe fixture (Contract B) ([#9](https://github.com/syntropy-systems-oss/windtunnel/issues/9)) ([9c1da96](https://github.com/syntropy-systems-oss/windtunnel/commit/9c1da96ff82182751c469002ddab9847bd494f94))


### Documentation

* **design:** trace re-seeding spine — interchange + universe-fixture contracts ([#6](https://github.com/syntropy-systems-oss/windtunnel/issues/6)) ([c48665d](https://github.com/syntropy-systems-oss/windtunnel/commit/c48665dafdf7b42eee9cedd7cad0fbceeea04759))
* user guides for universe fixtures, scorers, ledger, CI output ([#13](https://github.com/syntropy-systems-oss/windtunnel/issues/13)) ([2a60796](https://github.com/syntropy-systems-oss/windtunnel/commit/2a6079618697615c56cb099ce89922f6b4819641))

## [0.3.0](https://github.com/syntropy-systems-oss/windtunnel/compare/v0.2.0...v0.3.0) (2026-06-15)


### Features

* **scoring:** Scenario.outcome_fn for custom outcome evaluation ([#4](https://github.com/syntropy-systems-oss/windtunnel/issues/4)) ([82efa95](https://github.com/syntropy-systems-oss/windtunnel/commit/82efa952e050f7cd3ddbee15d1b5a389f33f65f0))

## [0.2.0](https://github.com/syntropy-systems-oss/windtunnel/compare/v0.1.0...v0.2.0) (2026-06-10)


### Features

* first-class external-state evidence — Trace.observations + StateProbe SPI ([#3](https://github.com/syntropy-systems-oss/windtunnel/issues/3)) ([f33ea0e](https://github.com/syntropy-systems-oss/windtunnel/commit/f33ea0edd64c8bb67ad9d1c29be13e71652c9bd2))
