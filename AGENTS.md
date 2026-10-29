# pio2 Agent and Contributor Rules

pio2 follows the AbstractX SpecTrace rules (AbstractX `AGENTS.md`, `docs/SPECTRACE.md`). In short:

1. **Spec first.** Requirements live in `*SPECIFICATION.md` files as `### \`[SPEC-AREA-NN]\` Title`
   blocks with `* **Requirement**:`, `* **Verified by**:` (skill names from `.agents/skills/`) and
   `* **Implementation Target**:` lines. Change the requirement before the code.
   * `docs/SPECIFICATION.md`: the PIO tile (`[SPEC-PIO-*]`)
   * `docs/INTERCONNECT_SPECIFICATION.md`: host link, Wishbone, DMA, trace, host adapters
     (`[SPEC-WB-*]`, `[SPEC-LINK-*]`, `[SPEC-DMA-*]`, `[SPEC-TRC-*]`, `[SPEC-ICN-*]`)
   * `docs/DEBUGGER_SPECIFICATION.md`: simulator, debugger, GUI (`[SPEC-DBG-*]`)
2. **Tags in code.** Implementing code carries `// @impl [SPEC-*] <spec file>` (SystemVerilog, C++) or
   `# @impl [SPEC-*] <spec file>` (Python); tests carry `@verifies [SPEC-*] <spec file>` and must call
   the code the tag names.
3. **Check with the skills** named in `Verified by` (`.agents/skills/<name>/SKILL.md`; Claude Code reads
   them through `.claude/skills`). SpecTrace runs from an AbstractX checkout:
   ```bash
   A=../AbstractX   # or $ABSTRACTX_DIR
   python3 $A/tools/spectrace_matrix.py --root . --matrix
   python3 $A/tools/spectrace_matrix.py --root . --check-drift
   python3 $A/tools/spectrace_matrix.py --root . --check-skills
   python3 $A/tools/spectrace_matrix.py --root . --check-verifies
   ```
4. **Record results and lessons.** Commands and their output go in the commit message; every lesson
   goes in `engineering_log.md` with a category (`product-bug`, `tool-bug`, `false-alarm`,
   `process-gap`, `hardware-fact`). A fix comes with a test that fails on the old code.
5. **Never claim done without evidence**; end every report with what was not checked.
6. **Licensing.** GPL-3.0-or-later. Raspberry Pi documentation is linked (`docs/references.md`),
   never copied; third-party code keeps its license file.
7. **Git.** Commits use `Tim Michals <tcmichals@gmail.com>` and carry no `Co-Authored-By` or other AI
   metadata. Commit and push `main` when a piece of work is done.
