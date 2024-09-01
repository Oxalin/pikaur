# pylint: disable=invalid-name,line-too-long
"""
Pure-python alpm implementation backported from Pikaur v0.6
with compatibility layer added for easier integration with pyalpm interface.
"""
import abc
import multiprocessing
import os
import re
import sys
import tarfile
from pathlib import Path
from pprint import pformat
from typing import IO, TYPE_CHECKING, Any, Final, cast

if TYPE_CHECKING:
    from collections.abc import Iterable

NOT_FOUND_ATOM = object()


DB_NAME_LOCAL: Final = "local"


SUPPORTED_ALPM_VERSION: Final = "9"
# SUPPORTED_ALPM_VERSION: Final = "99999"  # used for testing only
FORCE_PACMAN_CLI_DB: Final = False
# FORCE_PACMAN_CLI_DB: Final = True
PACMAN_EXECUTABLE = "pacman"
PACMAN_CONF_EXECUTABLE = "pacman-conf"
PACMAN_ROOT = "/var/lib/pacman"


VERBOSE: Final = True


def debug(*args: Any) -> None:
    if VERBOSE:
        print(*args)


def error(*args: Any) -> None:
    print(*args, file=sys.stderr)


class DB:
    name: str

    def search(self, query: str) -> list["Package"]:
        pkgs: dict[str, PacmanPackageInfo]
        if self.name == DB_NAME_LOCAL:
            pkgs = PackageDB.get_local_dict()
        else:
            pkgs = PackageDB.get_repo_dict()
        if not query:
            return list(pkgs.values())
        return [
            pkg for pkg_name, pkg in pkgs.items()
            if query in pkg_name
        ]

    def get_pkg(self, name: str) -> "Package | None":
        if self.name == DB_NAME_LOCAL:
            # return PackageDB.get_local_dict()[name]
            return PackageDB.get_local_pkg_uncached(name)
        return PackageDB.get_repo_dict()[name]

    def __init__(self, name: str) -> None:
        self.name = name


class Package:
    db: DB

    # description properties
    name: str
    version: str
    desc: str
    url: str
    arch: str
    licenses: list[str]
    groups: list[str]

    # package properties
    packager: str
    md5sum: str
    sha256sum: str
    base64_sig: str
    filename: str
    base: str
    reason: int  # 0:explicit 1:installed_as_dependency
    builddate: int
    installdate: int
    # { "files",  (getter)pyalpm_package_get_files, 0, "list of installed files", NULL } ,
    # { "backup", (getter)_get_list_attribute, 0, "list of tuples (filename, md5sum)", &get_backup } ,  # noqa: E501,RUF100
    # { "deltas", (getter)_get_list_attribute, 0, "list of available deltas", &get_deltas } ,
    validation: str | None = None

    # /* dependency information */
    depends: list[str]
    optdepends: list[str]
    conflicts: list[str]
    provides: list[str]
    replaces: list[str]

    # /* miscellaneous information */
    has_scriptlet: bool
    # { "download_size", (getter)pyalpm_pkg_download_size, 0, "predicted download size for this package", NULL },  # noqa: E501,RUF100
    size: int
    isize: int

    def compute_requiredby(self) -> list[str]:
        return [
            pkg.name
            for pkg in PackageDB.get_local_list()
            for name in [
                self.name,
                *(get_package_name_from_depend_line(line) for line in self.provides),
            ]
            if name in [get_package_name_from_depend_line(line) for line in pkg.depends]
        ]

    def compute_optionalfor(self) -> list[str]:
        return [
            pkg.name
            for pkg in PackageDB.get_local_list()
            for name in [
                self.name,
                *(get_package_name_from_depend_line(line) for line in self.provides),
            ]
            if name in [get_package_name_from_depend_line(line) for line in pkg.optdepends]
        ]

    def __init__(self) -> None:
        for field in DB_INFO_TRANSLATION.values():
            if field in PACMAN_LIST_FIELDS:
                setattr(self, field, [])
            elif field in PACMAN_DICT_FIELDS:
                setattr(self, field, {})
            elif field in PACMAN_INT_FIELDS:
                setattr(self, field, 0)
            else:
                setattr(self, field, None)


################################################################################


PACMAN_LIST_FIELDS = (
    "conflicts",
    "replaces",
    "depends",
    "provides",
    "licenses",
    "groups",
    "makedepends",
    "checkdepends",

    # used only in fallback parser:
    "required_by",
    "optional_for",
)


PACMAN_INT_FIELDS = (
    "reason",
)


PACMAN_DICT_FIELDS = (
    "optdepends",
)


DB_INFO_TRANSLATION = {
    "%NAME%": "name",
    "%VERSION%": "version",
    "%PROVIDES%": "provides",
    "%DESC%": "desc",
    "%CONFLICTS%": "conflicts",
    "%DEPENDS%": "depends",
    "%MAKEDEPENDS%": "makedepends",
    "%CHECKDEPENDS%": "checkdepends",
    "%OPTDEPENDS%": "optdepends",
    "%REPLACES%": "replaces",
    "%LICENSE%": "licenses",
    "%REASON%": "reason",
    "%SIZE%": "size",
    "%BUILDDATE%": "builddate",
    "%INSTALLDATE%": "installdate",
    "%PACKAGER%": "packager",
    "%URL%": "url",
    "%BASE%": "base",
    "%GROUPS%": "groups",
    "%ARCH%": "arch",
    "%VALIDATION%": "validation",
    "%XDATA%": "data",
    "%SHA256SUM%": "sha256sum",
    "%PGPSIG%": "base64_sig",
    "%ISIZE%": "isize",
    "%CSIZE%": "size",
    "%MD5SUM%": "md5sum",
    "%FILENAME%": "filename",
}


class PacmanPackageInfo(Package):
    data = None

    def __repr__(self) -> str:
        return f'<{self.__class__.__name__} "{self.name}">'

    @property
    def all(self) -> str:
        return pformat(self.__dict__)

    @classmethod
    def _parse_pacman_db_info(  # pylint: disable=too-many-branches  # noqa: C901,E501,RUF100
        cls,
        db_file: IO[bytes],
    ) -> "Iterable[PacmanPackageInfo]":

        pkg = cls()
        value: str | list[str] | dict[str, str | None] | int | None
        line = field = real_field = value = None

        # while line != "":  # noqa: PLC1901,RUF100
        for line_b in db_file.readlines():
            # line = db_file.readline().strip().decode("utf-8")
            line = line_b.strip().decode("utf-8")
            if line.startswith("%"):

                # if field in DB_INFO_TRANSLATION:
                if real_field:
                    setattr(pkg, real_field, value)

                field = line
                real_field = DB_INFO_TRANSLATION.get(field)
                if not real_field:
                    error(f"Unknown field {field}")
                    continue

                if real_field == "name" and getattr(pkg, "name", None):
                    yield pkg
                    pkg = cls()

                if real_field in PACMAN_LIST_FIELDS:
                    value = []
                elif real_field in PACMAN_DICT_FIELDS:
                    value = {}
                else:
                    value = ""
            else:
                if field not in DB_INFO_TRANSLATION:
                    error(f"{field=} {line=}")
                    continue

                _value = line.strip()
                if not _value:
                    continue
                if real_field in PACMAN_LIST_FIELDS:
                    cast(list[str], value).append(_value)
                elif real_field in PACMAN_DICT_FIELDS:
                    subkey, *subvalue_parts = _value.split(": ")
                    subvalue = ": ".join(subvalue_parts)
                    # pylint: disable=unsupported-assignment-operation
                    cast(dict[str, str], value)[subkey] = subvalue
                elif real_field in PACMAN_INT_FIELDS:
                    value = int(_value)
                else:
                    value = cast(str, value) + _value

        if not real_field:
            raise RuntimeError(field)
        setattr(pkg, real_field, value)

        yield pkg

    @classmethod
    def parse_pacman_db_gzip_info(cls, file_name: str) -> "Iterable[PacmanPackageInfo]":
        with tarfile.open(file_name, mode="r|gz") as archive:
            while file := archive.next():
                if file.isfile() and (extracted := archive.extractfile(file)):
                    yield from cls._parse_pacman_db_info(extracted)

    @classmethod
    def parse_pacman_db_info(cls, file_name: str) -> "Iterable[PacmanPackageInfo]":
        with open(file_name, "rb") as fobj:  # noqa: PTH123
            yield from cls._parse_pacman_db_info(fobj)


def get_package_name_from_depend_line(depend_line: str) -> str:
    return depend_line.split("=", maxsplit=1)[0]


class PackageDBCommon(abc.ABC):

    _repo_cache: list[PacmanPackageInfo] | None = None
    _local_cache: list[PacmanPackageInfo] | None = None
    _repo_dict_cache: dict[str, PacmanPackageInfo] | None = None
    _local_dict_cache: dict[str, PacmanPackageInfo] | None = None
    _repo_provided_cache: list[str] | None = None
    _local_provided_cache: list[str] | None = None

    repo = "repo"
    local = "local"

    @classmethod
    @abc.abstractmethod
    def get_local_pkg_uncached(cls, name: str) -> PacmanPackageInfo | None:
        raise NotImplementedError

    @classmethod
    def get_repo_list(cls) -> list[PacmanPackageInfo]:
        if not cls._repo_cache:
            cls._repo_cache = list(cls.get_repo_dict().values())
        return cls._repo_cache

    @classmethod
    def get_local_list(cls) -> list[PacmanPackageInfo]:
        if not cls._local_cache:
            cls._local_cache = list(cls.get_local_dict().values())
        return cls._local_cache

    @classmethod
    def get_repo_dict(cls) -> dict[str, PacmanPackageInfo]:
        if not cls._repo_dict_cache:
            cls._repo_dict_cache = {
                pkg.name: pkg
                for pkg in cls.get_repo_list()
            }
        return cls._repo_dict_cache

    @classmethod
    def get_local_dict(cls) -> dict[str, PacmanPackageInfo]:
        if not cls._local_dict_cache:
            cls._local_dict_cache = {
                pkg.name: pkg
                for pkg in cls.get_local_list()
            }
        return cls._local_dict_cache

    @classmethod
    def _get_provided(cls, local: str) -> list[str]:
        pkgs: list[PacmanPackageInfo] = (
            cls.get_local_list()
            if local == cls.local
            else cls.get_repo_list()
        )
        return [
            get_package_name_from_depend_line(provided_pkg)
            for pkg in pkgs
            for provided_pkg in pkg.provides
            if pkg.provides
        ]

    @classmethod
    def get_repo_provided(cls) -> list[str]:
        if not cls._repo_provided_cache:
            cls._repo_provided_cache = cls._get_provided(cls.repo)
        return cls._repo_provided_cache

    @classmethod
    def get_local_provided(cls) -> list[str]:
        if not cls._local_provided_cache:
            cls._local_provided_cache = cls._get_provided(cls.local)
        return cls._local_provided_cache


class PackageDB_ALPM9(PackageDBCommon):  # pylint: disable=invalid-name  # noqa: N801

    # ~2.7 seconds (was ~2.2 seconds with gzip)

    _repo_db_names: list[str] | None = None
    sync_dir = f"{PACMAN_ROOT}/sync/"
    local_dir = f"{PACMAN_ROOT}/local/"

    @classmethod
    def get_db_names(cls) -> list[str]:
        if not cls._repo_db_names:
            cls._repo_db_names = [
                repo_name.rsplit(".db", maxsplit=1)[0]
                for repo_name in os.listdir(cls.sync_dir)
                if repo_name and repo_name.endswith(".db")
            ]
        return cls._repo_db_names

    @classmethod
    def _get_repo_dict_for_repo(cls, repo_name: str) -> dict[str, PacmanPackageInfo]:
        result = {}
        debug(f" -------<<- {os.getpid()} {repo_name}")
        repo_path = os.path.join(cls.sync_dir, f"{repo_name}.db")
        for pkg in PacmanPackageInfo.parse_pacman_db_gzip_info(repo_path):
            pkg.db = DB(name=repo_name)
            result[pkg.name] = pkg
        debug(f" ------->>- {os.getpid()} {repo_name}")
        return result

    @classmethod
    def get_repo_dict(cls) -> dict[str, PacmanPackageInfo]:
        if not cls._repo_dict_cache:
            debug(f" <<<<<<<<<< {os.getpid()} REPO_NOT_CACHED")

            result = {}
            with multiprocessing.pool.Pool() as pool:
                jobs = [
                    pool.apply_async(cls._get_repo_dict_for_repo, (repo_name, ))
                    for repo_name in cls.get_db_names()
                ]
                pool.close()
                for job in jobs:
                    result.update(job.get())
                pool.join()

            cls._repo_dict_cache = result
            debug(f" >>>>>>>>>> {os.getpid()} REPO_DONE")
        return cls._repo_dict_cache

    @classmethod
    def get_local_pkg_uncached(cls, name: str) -> PacmanPackageInfo | None:
        for dir_name in os.listdir(cls.local_dir):
            if name == dir_name.rsplit("-", maxsplit=2)[0]:
                result = list(PacmanPackageInfo.parse_pacman_db_info(
                    os.path.join(cls.local_dir, dir_name, "desc"),
                ))
                if result:
                    return result[0]
        return None

    @classmethod
    def get_local_dict(cls) -> dict[str, PacmanPackageInfo]:
        if not cls._local_dict_cache:
            debug(" <<<<<<<<<< LOCAL_NOT_CACHED")

            result: dict[str, PacmanPackageInfo] = {}
            for pkg_dir_name in os.listdir(cls.local_dir):
                if not os.path.isdir(os.path.join(cls.local_dir, pkg_dir_name)):
                    continue

                for pkg in PacmanPackageInfo.parse_pacman_db_info(
                        os.path.join(cls.local_dir, pkg_dir_name, "desc"),
                ):
                    result[pkg.name] = pkg

            cls._local_dict_cache = result
            debug(" >>>>>>>>>> LOCAL_DONE")
        return cls._local_dict_cache


error(
    "\n"
    " !!! Pikaur-static (Python-only ALPM DB reader) activated\n"
    "     (consider using it only in recovery situations)\n",
)
with Path(f"{PACMAN_ROOT}/local/ALPM_DB_VERSION").open(encoding="utf-8") as version_file:
    ALPM_DB_VER = version_file.read().strip()
    PackageDB: type[PackageDBCommon]
    # VANILLA pikaur + cpython + pyalpm: -Qu --repo: ~ T1: 1.2..1.4s, T2: 1.3..1.5s
    if (ALPM_DB_VER == SUPPORTED_ALPM_VERSION) and not FORCE_PACMAN_CLI_DB:
        # CPYTHON: -Qu --repo: ~ T1: 2.6..3.1s, T2: 3.9..4.8
        # NUITKA: -Qu --repo: ~ 3.2..3.7 s
        # NUITKA_static: -Qu --repo: ~ T1, 3.1..3.9s, T2: 4.6..5.4
        PackageDB = PackageDB_ALPM9
    else:
        if FORCE_PACMAN_CLI_DB:
            error(" >>> User forced to use pacman output")
        else:
            error(
                f"\n !!! Current ALPM DB version={ALPM_DB_VER}."
                f" Pikaur-static supports only {SUPPORTED_ALPM_VERSION}\n",
            )
        # raise RuntimeError(ALPM_DB_VER)
        error(
            " >>> Switching to pure pacman-only mode"
            " (pacman CLI output will be used instead of ALPM DB)...\n\n",
        )
        from pacman_fallback import get_pacman_cli_package_db
        # CPYTHON: -Qu --repo: ~ T1: 2.8..3.3s, T2: 3.5..3.7
        PackageDB = get_pacman_cli_package_db(
            PackageDBCommon=PackageDBCommon,  # type: ignore[type-abstract]
            PacmanPackageInfo=PacmanPackageInfo,
            PACMAN_DICT_FIELDS=PACMAN_DICT_FIELDS,
            PACMAN_LIST_FIELDS=PACMAN_LIST_FIELDS,
            PACMAN_INT_FIELDS=PACMAN_INT_FIELDS,
            PACMAN_EXECUTABLE=PACMAN_EXECUTABLE,
            PACMAN_CONF_EXECUTABLE=PACMAN_CONF_EXECUTABLE,
        )


################################################################################


class LooseVersion:

    component_re = re.compile(r"(\d+ | r | [a-z0-9]+ | \.)", re.VERBOSE)

    def __init__(self, vstring: str | None = None) -> None:
        if vstring:
            self.parse(vstring)

    def parse(self, vstring: str) -> None:
        self.vstring = vstring
        components = [x for x in self.component_re.split(vstring) if x and x != "."]
        self.version = components

    def __str__(self) -> str:
        return self.vstring

    def __repr__(self) -> str:
        return f"LooseVersion ('{self}')"

    def _cmp(self, other: "str | LooseVersion") -> int:
        other = self._coerce(other)
        if other is NotImplemented:
            return NotImplemented

        components = self.version
        components_other = other.version
        components_len = len(components)
        components_other_len = len(components_other)
        max_len = max(components_len, components_other_len)
        for i in range(max_len):
            error_counter = 0
            component_parsed: int | str
            component_other_parsed: int | str
            try:
                component_parsed = int(components[i]) if i < components_len else 0
            except ValueError:
                component_parsed = 0
                error_counter += 1
            try:
                component_other_parsed = int(components_other[i]) if i < components_other_len else 0
            except ValueError:
                component_other_parsed = 0
                error_counter += 1
            if error_counter == 2:  # noqa: PLR2004
                component_parsed = components[i]
                component_other_parsed = components_other[i]
            if i < components_len:
                components[i] = component_parsed
            else:
                components.append(component_parsed)
            if i < components_other_len:
                components_other[i] = component_other_parsed
            else:
                components_other.append(component_other_parsed)

        if components == components_other:
            return 0
        if components < components_other:
            # debug(f"-1 {components=} {components_other=}")
            return -1
        if components > components_other:
            # debug(f"1 {components=} {components_other=}")
            return 1
        return NotImplemented

    def cmp(self, other: "str | LooseVersion") -> int:
        return self._cmp(other)

    @classmethod
    def _coerce(cls, other: "str | LooseVersion") -> "LooseVersion":
        if isinstance(other, cls):
            return other
        if isinstance(other, str):
            return cls(other)
        return NotImplemented


def compare_versions(current_version: str, new_version: str) -> int:
    for separator in ("+", ):
        current_version = current_version.replace(separator, ".")
        new_version = new_version.replace(separator, ".")
    current_base_version = new_base_version = None
    for separator in (":", "-"):
        if separator in current_version:
            current_base_version, _current_version = \
                current_version.split(separator)[:2]
        if separator in new_version:
            new_base_version, _new_version = \
                new_version.split(separator)[:2]
        if (
                current_base_version and new_base_version
        ) and (
            current_base_version != new_base_version
        ):
            current_version = current_base_version
            new_version = new_base_version
            break

    return LooseVersion(current_version).cmp(new_version)


################################################################################
