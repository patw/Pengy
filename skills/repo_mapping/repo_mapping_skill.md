# Repository Mapping Skill

Builds a concise, evidence-based map of a code repository before implementation work. Use it to understand project boundaries, entry points, build systems, tests, feature ownership, and safe places to edit.

## Workflow

1. **Read the repository root.** Inspect `README`, contributor/developer documentation, manifests, lockfiles, CI workflows, and the working-tree status.
2. **Inventory source and project files.** Use directory-tree, glob, and content-search tools. Exclude generated/build/vendor directories unless they are directly relevant.
3. **Identify project boundaries.** Detect separate applications, libraries, packages, services, examples, tools, and test projects. Do not assume one repository equals one application.
4. **Identify languages and build systems.** Recognize manifests and common entry points, including:
   - Python: `pyproject.toml`, `setup.py`, `requirements.txt`
   - Rust: `Cargo.toml`, workspace members
   - JavaScript/TypeScript: `package.json`, lockfiles, workspace files
   - Go: `go.mod`
   - C/C++: `CMakeLists.txt`, Makefiles, Meson, Bazel, Conan, vcpkg
   - Java/Kotlin: Gradle or Maven files
5. **Locate entry points.** Find GUI, CLI, web, service, library, and script entry points from manifests and source references—not filenames alone.
6. **Locate tests and validation.** Map test directories/files to the code they exercise and identify commands from CI or documentation.
7. **Map features to files.** For a requested feature, search for user-visible labels, config keys, API names, classes/functions, and related tests. Record the likely implementation files and parallel implementations if present.
8. **Separate tracked source from generated artifacts.** Mark build directories, binaries, caches, generated code, packaged artifacts, and vendored dependencies. Avoid editing them unless explicitly required.
9. **Record constraints.** Note platform assumptions, required dependencies, code-generation steps, environment variables, network/service requirements, and uncommitted user changes.
10. **Produce the map before editing.** Keep it short enough to use as a working plan, but include exact paths and commands where confidence is high.

## Suggested output

```text
Repository map: <name>

Boundaries
- <path>: <application/library/service> — <purpose>

Languages/build systems
- <language>: <manifest/build command>

Entry points
- GUI: <path>
- CLI: <path>
- Web/service: <path>
- Library: <path>

Feature map: <requested feature>
- Implementation: <paths>
- Configuration/schema: <paths>
- Tests: <paths>
- Parallel implementations: <paths, if any>

Validation
- Focused: <commands>
- Full: <commands>

Constraints and cautions
- <platform/dependency/generated-file/uncommitted-change note>
```

## General rules

- Prefer project documentation and CI over guessed commands.
- Use exact paths, not vague descriptions.
- Treat generated files and build outputs as read-only evidence by default.
- Preserve unrelated working-tree modifications.
- When mapping a feature across languages or packages, explicitly state which files are equivalent and which are merely analogous.
- Do not claim a file owns a feature until a symbol, config key, UI label, or test confirms the relationship.
- If evidence is incomplete, mark the mapping as tentative and identify the next search needed.
