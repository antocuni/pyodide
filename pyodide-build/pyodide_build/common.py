import contextlib
import functools
import os
import subprocess
import sys
from pathlib import Path
from typing import Generator, Iterable, Iterator, Mapping

import tomli
from packaging.tags import Tag, compatible_tags, cpython_tags
from packaging.utils import parse_wheel_filename

from .io import parse_package_config


def emscripten_version() -> str:
    return get_make_flag("PYODIDE_EMSCRIPTEN_VERSION")


def platform() -> str:
    emscripten_version = get_make_flag("PYODIDE_EMSCRIPTEN_VERSION")
    version = emscripten_version.replace(".", "_")
    return f"emscripten_{version}_wasm32"


def pyodide_tags() -> Iterator[Tag]:
    """
    Returns the sequence of tag triples for the Pyodide interpreter.

    The sequence is ordered in decreasing specificity.
    """
    PYMAJOR = get_make_flag("PYMAJOR")
    PYMINOR = get_make_flag("PYMINOR")
    PLATFORM = platform()
    python_version = (int(PYMAJOR), int(PYMINOR))
    yield from cpython_tags(platforms=[PLATFORM], python_version=python_version)
    yield from compatible_tags(platforms=[PLATFORM], python_version=python_version)


def find_matching_wheels(wheel_paths: Iterable[Path]) -> Iterator[Path]:
    """
    Returns the sequence wheels whose tags match the Pyodide interpreter.

    Parameters
    ----------
    wheel_paths
        A list of paths to wheels

    Returns
    -------
    The subset of wheel_paths that have tags that match the Pyodide interpreter.
    """
    wheel_paths = list(wheel_paths)
    wheel_tags_list: list[frozenset[Tag]] = []
    for wheel in wheel_paths:
        _, _, _, tags = parse_wheel_filename(wheel.name)
        wheel_tags_list.append(tags)
    for supported_tag in pyodide_tags():
        for wheel_path, wheel_tags in zip(wheel_paths, wheel_tags_list):
            if supported_tag in wheel_tags:
                yield wheel_path


UNVENDORED_STDLIB_MODULES = {"test", "distutils"}

ALWAYS_PACKAGES = {
    "pyparsing",
    "packaging",
    "micropip",
}

CORE_PACKAGES = {
    "micropip",
    "pyparsing",
    "pytz",
    "packaging",
    "Jinja2",
    "regex",
    "fpcast-test",
    "sharedlib-test-py",
    "cpp-exceptions-test",
    "ssl",
    "pytest",
    "tblib",
}

CORE_SCIPY_PACKAGES = {
    "numpy",
    "scipy",
    "pandas",
    "matplotlib",
    "scikit-learn",
    "joblib",
    "pytest",
}


def _parse_package_subset(query: str | None) -> set[str]:
    """Parse the list of packages specified with PYODIDE_PACKAGES env var.

    Also add the list of mandatory packages: ["pyparsing", "packaging",
    "micropip"]

    Supports following meta-packages,
     - 'core': corresponds to packages needed to run the core test suite
       {"micropip", "pyparsing", "pytz", "packaging", "Jinja2", "fpcast-test"}. This is the default option
       if query is None.
     - 'min-scipy-stack': includes the "core" meta-package as well as some of the
       core packages from the scientific python stack and their dependencies:
       {"numpy", "scipy", "pandas", "matplotlib", "scikit-learn", "joblib", "pytest"}.
       This option is non exhaustive and is mainly intended to make build faster
       while testing a diverse set of scientific packages.
     - '*': corresponds to all packages (returns None)

    Note: None as input is equivalent to PYODIDE_PACKAGES being unset and leads
    to only the core packages being built.

    Returns:
      a set of package names to build or None (build all packages).
    """
    if query is None:
        query = "core"

    packages = {el.strip() for el in query.split(",")}
    packages.update(ALWAYS_PACKAGES)
    packages.update(UNVENDORED_STDLIB_MODULES)
    # handle meta-packages
    if "core" in packages:
        packages |= CORE_PACKAGES
        packages.discard("core")
    if "min-scipy-stack" in packages:
        packages |= CORE_PACKAGES | CORE_SCIPY_PACKAGES
        packages.discard("min-scipy-stack")

    # Hack to deal with the circular dependence between soupsieve and
    # beautifulsoup4
    if "beautifulsoup4" in packages:
        packages.add("soupsieve")
    packages.discard("")
    return packages


def get_make_flag(name: str) -> str:
    """Get flags from makefile.envs.

    For building packages we currently use:
        SIDE_MODULE_LDFLAGS
        SIDE_MODULE_CFLAGS
        SIDE_MODULE_CXXFLAGS
        TOOLSDIR
    """
    return get_make_environment_vars()[name]


def get_pyversion() -> str:
    PYMAJOR = get_make_flag("PYMAJOR")
    PYMINOR = get_make_flag("PYMINOR")
    return f"python{PYMAJOR}.{PYMINOR}"


def get_hostsitepackages() -> str:
    return get_make_flag("HOSTSITEPACKAGES")


@functools.cache
def get_make_environment_vars() -> dict[str, str]:
    """Load environment variables from Makefile.envs

    This allows us to set all build vars in one place"""

    PYODIDE_ROOT = get_pyodide_root()
    environment = {}
    result = subprocess.run(
        ["make", "-f", str(PYODIDE_ROOT / "Makefile.envs"), ".output_vars"],
        capture_output=True,
        text=True,
    )
    for line in result.stdout.splitlines():
        equalPos = line.find("=")
        if equalPos != -1:
            varname = line[0:equalPos]
            value = line[equalPos + 1 :]
            value = value.strip("'").strip()
            environment[varname] = value
    return environment


def search_pyodide_root(curdir: str | Path, *, max_depth: int = 5) -> Path:
    """
    Recursively search for the root of the Pyodide repository,
    by looking for the pyproject.toml file in the parent directories
    which contains [tool.pyodide] section.
    """

    # We want to include "curdir" in parent_dirs, so add a garbage suffix
    parent_dirs = (Path(curdir) / "garbage").parents[:max_depth]

    for base in parent_dirs:
        pyproject_file = base / "pyproject.toml"

        if not pyproject_file.is_file():
            continue

        try:
            with pyproject_file.open("rb") as f:
                configs = tomli.load(f)
        except tomli.TOMLDecodeError:
            raise ValueError(f"Could not parse {pyproject_file}.")

        if "tool" in configs and "pyodide" in configs["tool"]:
            return base

    raise FileNotFoundError(
        "Could not find Pyodide root directory. If you are not in the Pyodide directory, set `PYODIDE_ROOT=<pyodide-root-directory>`."
    )


def init_environment() -> None:
    if os.environ.get("__LOADED_PYODIDE_ENV"):
        return
    os.environ["__LOADED_PYODIDE_ENV"] = "1"
    # If we are building docs, we don't need to know the PYODIDE_ROOT
    if "sphinx" in sys.modules:
        os.environ["PYODIDE_ROOT"] = ""

    if "PYODIDE_ROOT" in os.environ:
        os.environ["PYODIDE_ROOT"] = str(Path(os.environ["PYODIDE_ROOT"]).resolve())
    else:
        os.environ["PYODIDE_ROOT"] = str(search_pyodide_root(os.getcwd()))

    os.environ.update(get_make_environment_vars())
    hostsitepackages = get_hostsitepackages()
    pythonpath = [
        hostsitepackages,
    ]
    os.environ["PYTHONPATH"] = ":".join(pythonpath)
    os.environ["BASH_ENV"] = ""
    get_unisolated_packages()


@functools.cache
def get_pyodide_root() -> Path:
    init_environment()
    return Path(os.environ["PYODIDE_ROOT"])


@functools.cache
def get_unisolated_packages() -> list[str]:
    import json

    if "UNISOLATED_PACKAGES" in os.environ:
        return json.loads(os.environ["UNISOLATED_PACKAGES"])
    PYODIDE_ROOT = get_pyodide_root()
    unisolated_packages = []
    for pkg in (PYODIDE_ROOT / "packages").glob("**/meta.yaml"):
        config = parse_package_config(pkg, check=False)
        if config.get("build", {}).get("cross-build-env", False):
            unisolated_packages.append(config["package"]["name"])
    # TODO: remove setuptools_rust from this when they release the next version.
    unisolated_packages.append("setuptools_rust")
    os.environ["UNISOLATED_PACKAGES"] = json.dumps(unisolated_packages)
    return unisolated_packages


@contextlib.contextmanager
def replace_env(build_env: Mapping[str, str]) -> Generator[None, None, None]:
    old_environ = dict(os.environ)
    os.environ.clear()
    os.environ.update(build_env)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(old_environ)
