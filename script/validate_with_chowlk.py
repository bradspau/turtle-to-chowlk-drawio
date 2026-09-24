#!/usr/bin/env python3
"""Validate a generated Chowlk-notation draw.io diagram against the real
Chowlk parser (https://github.com/oeg-upm/Chowlk). Well-formed XML from
gen_drawio.py is not the same as a diagram Chowlk's classifier actually
accepts -- see references/chowlk-notation-gotchas.md for why the two can
diverge and how to read a failure here.

Clones (once) and caches a venv'd checkout of Chowlk, patches its one
Python-3.11-incompatible f-string (a container-only workaround -- see
_patch_pep701_fstrings below), and runs its converter.py against the given
diagram, reporting PASS/FAIL.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import venv

DEFAULT_CACHE_DIR = os.path.expanduser("~/.cache/ontology-to-drawio/chowlk")
CHOWLK_REPO = "https://github.com/oeg-upm/Chowlk.git"

# psycopg2 is only needed for Chowlk's Flask web app, not its converter.py
# CLI, and its build regularly fails on containers with no postgres headers
# installed -- so it's dropped from a filtered copy of requirements.txt
# rather than installed. greenlet is pulled in transitively (converter.py
# imports the app package, whose __init__.py imports flask_sqlalchemy) but
# is only needed for SQLAlchemy's async engine, which converter.py's code
# path never uses -- confirmed flask_sqlalchemy imports fine without it,
# and greenlet's pinned old version has no prebuilt wheel for newer
# CPython/architectures, making it a real build-fragility risk to keep.
SKIP_REQUIREMENTS = {"psycopg2", "psycopg2-binary", "greenlet"}

# Chowlk's chowlk/resources/utils.py contains f-strings with nested quotes
# of the same kind (e.g. f"...{d['x']}..." inside an already-double-quoted
# f-string) that are only legal under PEP 701 (Python 3.12+). This patches
# them to a 3.8-3.11-compatible form by swapping the outer f-string's quote
# character. Container-only workaround -- if Chowlk itself fixes this
# upstream, this patch becomes a no-op find (the regex simply won't match).
_PEP701_PATTERN = re.compile(r'f"([^"\n]*\'[^\'\n]*\'[^"\n]*)"')


def _patch_pep701_fstrings(chowlk_dir):
    target = os.path.join(chowlk_dir, "chowlk", "resources", "utils.py")
    if not os.path.exists(target):
        return
    with open(target) as f:
        src = f.read()
    patched = _PEP701_PATTERN.sub(lambda m: "f'" + m.group(1).replace("'", '"') + "'", src)
    if patched != src:
        with open(target, "w") as f:
            f.write(patched)


def _ensure_chowlk(cache_dir, refresh):
    chowlk_dir = os.path.join(cache_dir, "Chowlk")
    if refresh and os.path.isdir(chowlk_dir):
        shutil.rmtree(chowlk_dir)
    if os.path.isdir(chowlk_dir) and not os.path.isdir(os.path.join(chowlk_dir, ".git")):
        shutil.rmtree(chowlk_dir)  # partial/interrupted clone from a previous run
    if not os.path.isdir(chowlk_dir):
        os.makedirs(cache_dir, exist_ok=True)
        result = subprocess.run(["git", "clone", CHOWLK_REPO, chowlk_dir], capture_output=True, text=True)
        if result.returncode != 0:
            sys.exit(f"git clone of {CHOWLK_REPO} failed:\n{result.stderr}")
    _patch_pep701_fstrings(chowlk_dir)
    return chowlk_dir


def _ensure_venv(chowlk_dir):
    venv_dir = os.path.join(chowlk_dir, ".validate-venv")
    py = os.path.join(venv_dir, "bin", "python3")
    if not os.path.exists(py):
        venv.EnvBuilder(with_pip=True).create(venv_dir)
        req_path = os.path.join(chowlk_dir, "requirements.txt")
        filtered = []
        if os.path.exists(req_path):
            with open(req_path) as f:
                for line in f:
                    name = re.split(r"[=<>!~]", line.strip())[0].strip().lower()
                    if name and name not in SKIP_REQUIREMENTS:
                        filtered.append(line.strip())
        if filtered:
            result = subprocess.run(
                [py, "-m", "pip", "install", "-q"] + filtered,
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                sys.exit(f"Installing Chowlk's requirements failed:\n{result.stderr}")
            # pip has been observed to exit 0 while silently installing
            # nothing (a transient environment issue, seen twice in
            # practice) -- verify the venv actually works before trusting
            # it, so a broken cache fails clearly here (and self-heals by
            # deleting the broken venv) instead of being misdiagnosed as
            # an invalid diagram later.
            check = subprocess.run(
                [py, "-c", "import flask_sqlalchemy"],
                capture_output=True, text=True,
            )
            if check.returncode != 0:
                shutil.rmtree(venv_dir)
                sys.exit(
                    "Chowlk's dependencies were installed but the venv "
                    "doesn't actually work (pip likely reported success "
                    "without installing anything). Removed the broken "
                    f"venv -- rerun this command to try again.\n{check.stderr}"
                )
    return py


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("diagram", help="Path to the .xml file to validate")
    ap.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR,
                     help=f"Where to keep the cached Chowlk checkout (default: {DEFAULT_CACHE_DIR})")
    ap.add_argument("--refresh", action="store_true",
                     help="Delete and re-clone the cached Chowlk checkout before validating")
    args = ap.parse_args()

    diagram_path = os.path.abspath(args.diagram)
    if not os.path.exists(diagram_path):
        sys.exit(f"No such file: {diagram_path}")

    chowlk_dir = _ensure_chowlk(args.cache_dir, args.refresh)
    py = _ensure_venv(chowlk_dir)

    out_ttl = diagram_path.rsplit(".", 1)[0] + ".chowlk-out.ttl"
    errors_xml = diagram_path.rsplit(".", 1)[0] + ".chowlk-errors.xml"
    result = subprocess.run(
        [py, "converter.py", diagram_path, out_ttl, "--xml_error_path", errors_xml],
        cwd=chowlk_dir, capture_output=True, text=True,
    )
    output = result.stdout + result.stderr
    if result.returncode != 0:
        sys.exit(
            f"validate_with_chowlk: Chowlk's converter.py crashed (exit "
            f"{result.returncode}) instead of reporting a diagram "
            f"validation result -- this means the cached Chowlk "
            f"environment is broken (e.g. a partial dependency install), "
            f"not that {diagram_path} is an invalid diagram. Rerun with "
            f"--refresh to force a clean re-clone/re-install.\n\n{output}"
        )
    # Chowlk's own regex strings trigger a Python 3.12+ SyntaxWarning at
    # import time -- noise from Chowlk's code, not a diagram problem, and
    # not evidence of a real error on its own.
    meaningful = [
        line for line in output.splitlines()
        if not ("SyntaxWarning" in line and "escape sequence" in line)
    ]
    text = "\n".join(meaningful)

    # Precise clean-pass check: only the benign base-not-declared warning
    # (if any) may accompany "There is no errors".
    has_no_errors = "There is no errors" in text
    other_warning_or_error = any(
        ("warning" in line.lower() or "error" in line.lower())
        and "there is no errors" not in line.lower()
        and "a base has not been declared" not in line.lower()
        for line in meaningful
    )
    passed = has_no_errors and not other_warning_or_error

    print(text)
    if passed:
        print(f"\nPASS -- {diagram_path} is a valid Chowlk diagram.")
        sys.exit(0)
    else:
        note = f" See {errors_xml} if Chowlk wrote one." if os.path.exists(errors_xml) else ""
        print(f"\nFAIL -- {diagram_path} did not pass Chowlk validation.{note}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
