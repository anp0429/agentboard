"""TS import matching (resolver step 3.5), born from gauntlet run 1:
unjs/ufo tests src/utils.ts in test/utilities.test.ts — basename
conventions cannot survive human naming. Path matching catches tests
that import ".../<base>" outright (defu style); name matching catches
imports of the target's exported identifiers through a barrel (ufo
style). A tie is ambiguity, and ambiguity asks instead of guessing."""

import os

from agentboard.verifiers.harness import VitestHarness as V


def _mk(tmp_path, rel, content=""):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return str(p)


def test_name_matching_through_a_barrel_ufo_shape(tmp_path):
    _mk(tmp_path, "src/utils.ts",
        "export function hasProtocol(x: string) {}\n"
        "export function parsePath(x: string) {}\n"
        "export const isRelative = (x: string) => true\n")
    _mk(tmp_path, "src/index.ts", "export * from './utils'\n")
    _mk(tmp_path, "test/utilities.test.ts",
        "import { hasProtocol, parsePath } from '../src'\n"
        "test('x', () => {})\n")
    _mk(tmp_path, "test/other.test.ts",
        "import { somethingElse } from '../src'\n")
    got = V.default_tests_for(str(tmp_path), "src/utils.ts")
    assert got == os.path.join("test", "utilities.test.ts")


def test_path_matching_defu_shape(tmp_path):
    _mk(tmp_path, "src/defu.ts", "export function defu() {}\n")
    _mk(tmp_path, "test/merge.test.ts",
        "import { defu } from '../src/defu'\n")
    got = V.default_tests_for(str(tmp_path), "src/defu.ts")
    assert got == os.path.join("test", "merge.test.ts")


def test_single_name_overlap_is_too_weak_to_guess(tmp_path):
    _mk(tmp_path, "src/utils.ts", "export function alpha() {}\n")
    _mk(tmp_path, "test/a.test.ts", "import { alpha } from '../src'\n")
    _mk(tmp_path, "test/b.test.ts", "import { alpha } from '../src'\n")
    assert V.default_tests_for(str(tmp_path), "src/utils.ts") \
        .endswith("utils.test.ts")  # falls through to the best-guess ask


def test_colocated_still_wins_before_import_matching(tmp_path):
    _mk(tmp_path, "src/thing.ts", "export const a = 1\n")
    _mk(tmp_path, "src/thing.test.ts", "import { a } from './thing'\n")
    _mk(tmp_path, "test/thing.test.ts", "import { a } from '../src/thing'\n")
    assert V.default_tests_for(str(tmp_path), "src/thing.ts") \
        == os.path.join("src", "thing.test.ts")


def test_spec_suffix_colocated_and_dir(tmp_path):
    # gauntlet catch 3: pathe's entire suite is *.spec.ts
    _mk(tmp_path, "src/a.ts", "export const a = 1\n")
    _mk(tmp_path, "src/a.spec.ts", "import { a } from './a'\n")
    assert V.default_tests_for(str(tmp_path), "src/a.ts") \
        == os.path.join("src", "a.spec.ts")
    _mk(tmp_path, "src/b.ts", "export const b = 1\n")
    _mk(tmp_path, "test/b.spec.ts", "import { b } from '../src/b'\n")
    assert V.default_tests_for(str(tmp_path), "src/b.ts") \
        == os.path.join("test", "b.spec.ts")
