import re
import sys

from pathlib import Path
from posixpath import expandvars
from typing import TYPE_CHECKING, Dict, List, Optional
from urllib.parse import urldefrag, urlparse, urlsplit, urlunsplit

from clikit.api.io.flags import VERY_VERBOSE
from clikit.io import ConsoleIO, NullIO
from packaging.tags import compatible_tags, cpython_tags, mac_platforms

from conda_lock.interfaces.vendored_poetry import (
    Chooser,
    Env,
    Factory,
    PoetryDependency,
    PoetryPackage,
    PoetryProjectPackage,
    PoetrySolver,
    PoetryURLDependency,
    PoetryVCSDependency,
    Pool,
    PyPiRepository,
    Repository,
    Uninstall,
)
from conda_lock.lockfile import apply_categories
from conda_lock.lockfile.v2prelim.models import (
    DependencySource,
    HashModel,
    LockedDependency,
)
from conda_lock.lookup import conda_name_to_pypi_name
from conda_lock.models import lock_spec
from conda_lock.models.pip_repository import PipRepository


if TYPE_CHECKING:
    from packaging.tags import Tag


# NB: in principle these depend on the glibc on the machine creating the conda env.
# We use tags supported by manylinux Docker images, which are likely the most common
# in practice, see https://github.com/pypa/manylinux/blob/main/README.rst#docker-images.
MANYLINUX_TAGS = ["1", "2010", "2014", "_2_17", "_2_24", "_2_28"]
# This needs to be updated periodically as new macOS versions are released.
MACOS_VERSION = (13, 4)


class PlatformEnv(Env):
    """
    Fake poetry Env to match PyPI distributions to the target conda environment
    """

    def __init__(self, python_version: str, platform: str):
        super().__init__(path=Path(sys.prefix))
        system, arch = platform.split("-")
        if arch == "64":
            arch = "x86_64"

        if system == "linux":
            self._platforms = [
                f"manylinux{tag}_{arch}" for tag in reversed(MANYLINUX_TAGS)
            ]
            self._platforms.append(f"linux_{arch}")
        elif system == "osx":
            self._platforms = list(mac_platforms(MACOS_VERSION, arch))
        elif platform == "win-64":
            self._platforms = ["win_amd64"]
        else:
            raise ValueError(f"Unsupported platform '{platform}'")
        self._python_version = tuple(map(int, python_version.split(".")))

        if system == "osx":
            self._sys_platform = "darwin"
            self._platform_system = "Darwin"
            self._os_name = "posix"
        elif system == "linux":
            self._sys_platform = "linux"
            self._platform_system = "Linux"
            self._os_name = "posix"
        elif system == "win":
            self._sys_platform = "win32"
            self._platform_system = "Windows"
            self._os_name = "nt"
        else:
            raise ValueError(f"Unsupported platform '{platform}'")

    def get_supported_tags(self) -> List["Tag"]:
        """
        Mimic the output of packaging.tags.sys_tags() on the given platform
        """
        return list(
            cpython_tags(python_version=self._python_version, platforms=self._platforms)
        ) + list(
            compatible_tags(
                python_version=self._python_version, platforms=self._platforms
            )
        )

    def get_marker_env(self) -> Dict[str, str]:
        """Return the subset of info needed to match common markers"""
        return {
            "python_full_version": ".".join([str(c) for c in self._python_version]),
            "python_version": ".".join([str(c) for c in self._python_version[:2]]),
            "sys_platform": self._sys_platform,
            "platform_system": self._platform_system,
            "os_name": self._os_name,
        }


REQUIREMENT_PATTERN = re.compile(
    r"""
    ^
    (?P<name>[a-zA-Z0-9_-]+) # package name
    (?:\[(?P<extras>(?:\s?[a-zA-Z0-9_-]+(?:\s?\,\s?)?)+)\])? # extras
    (?:
        (?: # a direct reference
            \s?@\s?(?P<url>.*)
        )
        |
        (?: # one or more PEP440 version specifiers
            \s?(?P<constraint>
                (?:\s?
                    (?:
                        (?:=|[><~=!])?=
                        |
                        [<>]
                    )?
                    \s?
                    (?:
                        [A-Za-z0-9\.-_\*]+ # a version tuple, e.g. x.y.z
                        (?:-[A-Za-z]+(?:\.[0-9]+)?)? # a post-release tag, e.g. -alpha.2
                        (?:\s?\,\s?)?
                    )
                )+
            )
        )
    )?
    $
    """,
    re.VERBOSE,
)


def parse_pip_requirement(requirement: str) -> Optional[Dict[str, str]]:
    match = REQUIREMENT_PATTERN.match(requirement)
    if not match:
        return None
    return match.groupdict()


def get_dependency(dep: lock_spec.Dependency) -> PoetryDependency:
    # FIXME: how do deal with extras?
    extras: List[str] = []
    if isinstance(dep, lock_spec.VersionedDependency):
        return PoetryDependency(
            name=dep.name, constraint=dep.version or "*", extras=dep.extras
        )
    elif isinstance(dep, lock_spec.URLDependency):
        return PoetryURLDependency(
            name=dep.name,
            url=f"{dep.url}#{dep.hashes[0].replace(':','=')}",
            extras=extras,
        )
    elif isinstance(dep, lock_spec.VCSDependency):
        return PoetryVCSDependency(
            name=dep.name,
            vcs=dep.vcs,
            source=dep.source,
            rev=dep.rev,
        )
    else:
        raise ValueError(f"Unknown requirement {dep}")


def get_package(locked: LockedDependency) -> PoetryPackage:
    if locked.source is not None:
        return PoetryPackage(
            locked.name,
            source_type="url",
            source_url=locked.source.url,
            version="0.0.0",
        )
    else:
        return PoetryPackage(locked.name, version=locked.version)


def get_requirements(
    result: List,
    platform: str,
    pool: Pool,
    env: Env,
    pip_repositories: Optional[List[PipRepository]] = None,
    strip_auth: bool = False,
) -> List[LockedDependency]:
    """Extract distributions from Poetry package plan, ignoring uninstalls
    (usually: conda package with no pypi equivalent) and skipped ops
    (already installed)
    """
    chooser = Chooser(pool, env=env)
    requirements: List[LockedDependency] = []

    repositories_by_name = {
        repository.name: repository for repository in pip_repositories or []
    }

    for op in result:
        if not isinstance(op, Uninstall) and not op.skipped:
            # Take direct references verbatim
            source: Optional[DependencySource] = None
            source_repository = repositories_by_name.get(op.package.source_reference)

            if op.package.source_type == "url":
                url, fragment = urldefrag(op.package.source_url)
                hash_type, hash = fragment.split("=")
                hash = HashModel(**{hash_type: hash})
                source = DependencySource(type="url", url=op.package.source_url)
            elif op.package.source_type == "git":
                url = f"{op.package.source_type}+{op.package.source_url}@{op.package.source_resolved_reference}"
                # TODO: FIXME git ls-remoet
                hash = HashModel(**{"sha256": op.package.source_resolved_reference})
                source = DependencySource(type="url", url=url)
            # Choose the most specific distribution for the target
            # TODO: need to handle  git here
            # https://github.com/conda/conda-lock/blob/ac31f5ddf2951ed4819295238ccf062fb2beb33c/conda_lock/_vendor/poetry/installation/executor.py#L557
            else:
                link = chooser.choose_for(op.package)
                url = link.url_without_fragment
                hashes: Dict[str, str] = {}
                if link.hash_name is not None and link.hash is not None:
                    hashes[link.hash_name] = link.hash
                hash = HashModel.parse_obj(hashes)

            if source_repository:
                url = source_repository.normalize_solver_url(url)

            requirements.append(
                LockedDependency(
                    name=op.package.name,
                    version=str(op.package.version),
                    manager="pip",
                    source=source,
                    platform=platform,
                    dependencies={
                        dep.name: str(dep.constraint) for dep in op.package.requires
                    },
                    url=url if not strip_auth else _strip_auth(url),
                    hash=hash,
                )
            )
    return requirements


def solve_pypi(
    pip_specs: Dict[str, lock_spec.Dependency],
    use_latest: List[str],
    pip_locked: Dict[str, LockedDependency],
    conda_locked: Dict[str, LockedDependency],
    python_version: str,
    platform: str,
    pip_repositories: Optional[List[PipRepository]] = None,
    allow_pypi_requests: bool = True,
    verbose: bool = False,
    strip_auth: bool = False,
) -> Dict[str, LockedDependency]:
    """
    Solve pip dependencies for the given platform

    Parameters
    ----------
    conda :
        Path to conda, mamba, or micromamba
    use_latest :
        Names of packages to update to the latest version compatible with pip_specs
    pip_specs :
        PEP440 package specifications
    pip_locked :
        Previous solution for the given platform (pip packages only)
    conda_locked :
        Current solution of conda-only specs for the given platform
    python_version :
        Version of Python in conda_locked
    platform :
        Target platform
    allow_pypi_requests :
        Add pypi.org to the list of repositories (pip packages only)
    verbose :
        Print chatter from solver
    strip_auth :
        Whether to strip HTTP Basic auth from URLs.

    """
    dummy_package = PoetryProjectPackage("_dummy_package_", "0.0.0")
    dependencies: List[PoetryDependency] = [
        get_dependency(spec) for spec in pip_specs.values()
    ]
    for dep in dependencies:
        dummy_package.add_dependency(dep)

    pool = _prepare_repositories_pool(
        allow_pypi_requests, pip_repositories=pip_repositories
    )

    installed = Repository()
    locked = Repository()

    python_packages = dict()
    locked_dep: LockedDependency
    for locked_dep in conda_locked.values():
        if locked_dep.name.startswith("__"):
            continue
        # ignore packages that don't depend on Python
        if locked_dep.manager != "pip" and "python" not in locked_dep.dependencies:
            continue
        try:
            pypi_name = conda_name_to_pypi_name(locked_dep.name).lower()
        except KeyError:
            continue
        # Prefer the Python package when its name collides with the Conda package
        # for the underlying library, e.g. python-xxhash (pypi: xxhash) over xxhash
        # (pypi: no equivalent)
        if pypi_name not in python_packages or pypi_name != locked_dep.name:
            python_packages[pypi_name] = locked_dep.version
    # treat conda packages as both locked and installed
    for name, version in python_packages.items():
        for repo in (locked, installed):
            repo.add_package(PoetryPackage(name=name, version=version))
    # treat pip packages as locked only
    for spec in pip_locked.values():
        locked.add_package(get_package(spec))

    if verbose:
        io = ConsoleIO()
        io.set_verbosity(VERY_VERBOSE)
    else:
        io = NullIO()
    s = PoetrySolver(
        dummy_package,
        pool=pool,
        installed=installed,
        locked=locked,
        # ConsoleIO type is expected, but NullIO may be given:
        io=io,  # type: ignore
    )
    to_update = list(
        {spec.name for spec in pip_locked.values()}.intersection(use_latest)
    )
    env = PlatformEnv(python_version, platform)
    # find platform-specific solution (e.g. dependencies conditioned on markers)
    with s.use_environment(env):
        result = s.solve(use_latest=to_update)

    requirements = get_requirements(
        result,
        platform,
        pool,
        env,
        pip_repositories=pip_repositories,
        strip_auth=strip_auth,
    )

    # use PyPI names of conda packages to walking the dependency tree and propagate
    # categories from explicit to transitive dependencies
    planned = {
        **{dep.name: [dep] for dep in requirements},
    }

    # We add the conda packages here -- note that for a given pip package, we
    # may have multiple conda packages that map to it. One example is the `dask`
    # pip package; on the Conda side, there are two packages `dask` and `dask-core`
    # that map to it.
    # We use the pip names for the packages for everything so that planned
    # is essentially a dictionary of:
    #  - pip package name -> list of LockedDependency that are needed for this package
    for conda_name, locked_dep in conda_locked.items():
        pypi_name = conda_name_to_pypi_name(conda_name)
        if pypi_name in planned:
            planned[pypi_name].append(locked_dep)
        else:
            planned[pypi_name] = [locked_dep]

    apply_categories(requested=pip_specs, planned=planned, convert_to_pip_names=True)

    return {dep.name: dep for dep in requirements}


def _prepare_repositories_pool(
    allow_pypi_requests: bool, pip_repositories: Optional[List[PipRepository]] = None
) -> Pool:
    """
    Prepare the pool of repositories to solve pip dependencies

    Parameters
    ----------
    allow_pypi_requests :
            Add pypi.org to the list of repositories
    """
    factory = Factory()
    config = factory.create_config()
    repos = [
        factory.create_legacy_repository(
            {"name": pip_repository.name, "url": expandvars(pip_repository.url)},
            config,
        )
        for pip_repository in pip_repositories or []
    ] + [
        factory.create_legacy_repository(
            {"name": source[0], "url": source[1]["url"]}, config
        )
        for source in config.get("repositories", {}).items()
    ]
    if allow_pypi_requests:
        repos.append(PyPiRepository())
    return Pool(repositories=[*repos])


def _strip_auth(url: str) -> str:
    """Strip HTTP Basic authentication from a URL."""
    parts = urlsplit(url, allow_fragments=True)
    # Remove everything before and including the last '@' character in the part
    # between 'scheme://' and the subsequent '/'.
    netloc = parts.netloc.split("@")[-1]
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
