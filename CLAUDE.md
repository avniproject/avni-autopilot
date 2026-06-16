# Code conventions
1. Always find the root cause before suggesting a fix for an issue.
2. Provide production quality code that is robust, maintainable, and fault-tolerant.
3. Follow good coding practices and design patterns wherever applicable.
4. Use production quality, meaningful, maintainable variable, class, and function names.
5. Provide production quality comments & docstrings that are brief, wherever necessary and relevant.
6. Do not use `you / yours / me / us / we` in code comments or docstrings.
7. Use Python f-string for logging.
8. Provide high-quality, comprehensive, and unambiguous prompts.
9. 
9. Don't add comments when evident - dont mention phases of implementation in comments
10. Make sure the code is properly segregated based on the projects design - nodes, domain, pipeline
11. Use good design practices when implementing

# Development workflow
9. Designs live in `specs/` as SDDs. Run `/implement-spec specs/<NAME>.md` to implement one. The SDD is the contract — don't silently expand or contract scope, and "Out of scope" sections are binding. When the SDD is unclear, stop and quote the specific section back before guessing.
10. Don't pre-build for hypothetical future requirements. Don't add abstractions, fallback paths, or validation for scenarios that can't happen. Trust internal code; validate only at system boundaries (user input, external APIs, ingested data).
11. Trust-but-verify sub-agent output. Labels (file / function / form names, a header string, one method call) hint at intent but the body is truth. When dispatching agents to interpret code or data, require evidence-tied claims (line numbers, quoted operations, AST nodes). Read the source before recording an agent's claim as fact.
12. Normalize external inputs at the ingestion boundary. Strip provenance / sample comments, repair known syntax typos, drop broken or no-op samples. Don't propagate known-defective third-party content into the pipeline or corpus.
13. For refactors that touch generators, parsers, or catalog code, verify by regenerating the on-disk artefacts and diffing against a pre-change snapshot. Byte-identical means behaviour is preserved; any drift needs an explanation.
14. Curated knowledge artefacts (rule corpora, helper catalogs, prompt examples) live under `resources/`. Source data lives under `requirements/`. Implementation code lives under `src/`. Don't cross those layers — derived outputs should be regenerable from source via documented CLI commands, never hand-edited under `resources/` if they have a generator.
