import os
from pathlib import Path

from typing import List, Optional, Iterable, Set
import smbclient
from dotenv import load_dotenv

load_dotenv()


def env_required(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Missing required env var: {name}")
    return v


def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_list(
    name: str, default: Optional[List[str]] = None, sep: str = ","
) -> List[str]:
    v = os.getenv(name)
    if not v:
        return default or []
    return [x.strip() for x in v.split(sep) if x.strip()]


SERVER = env_required("SMB_SERVER")
PASSWORD = os.getenv("SMB_PASSWORD", "")


DATA_DIR = Path(env_required("DATA_DIR"))
OUTPUT_DIR = Path(env_required("OUTPUT_DIR"))

REMOTE_BASE = env_required("REMOTE_BASE").replace("/", "\\")
OVERWRITE = env_bool("OVERWRITE", default=False)

candidates = env_list(
    "CANDIDATES",
    default=[
        r"x.xafizov@uzspace.org",
        r"uzspace\x.xafizov",
        r"uzspace.org\x.xafizov",
    ],
)
SENTINEL_TYPES: Set[str] = {
    "AERAI",
    "CH4",
    "CO",
    "HCHO",
    "NO2",
    "O3",
    "SO2",
    "temperature",
    "wind",
}
ADS_TYPES: Set[str] = {"CO", "HCHO", "NO2", "O3", "SO2"}


def _norm_unc(p: str) -> str:
    p = p.replace("/", "\\")
    while "\\\\" in p[2:]:
        p = p[:2] + p[2:].replace("\\\\", "\\")
    return p


def auth() -> None:
    last_err = None
    for u in candidates:
        try:
            smbclient.register_session(SERVER, username=u, password=PASSWORD)
            print(f"✅ Auth OK with: {u}")
            return
        except Exception as e:
            print(f"❌ Auth failed with: {u} -> {e}")
            last_err = e
    raise last_err


def ensure_remote_dir(remote_dir: str) -> None:
    remote_dir = _norm_unc(remote_dir)
    if not remote_dir.startswith("\\\\"):
        raise ValueError(f"Remote dir must be UNC path, got: {remote_dir}")

    parts = remote_dir.split("\\")
    if len(parts) < 4:
        raise ValueError(f"Bad UNC path: {remote_dir}")

    base = "\\\\" + parts[2] + "\\" + parts[3]
    cur = base

    for p in parts[4:]:
        if not p:
            continue
        cur = cur + "\\" + p
        try:
            smbclient.listdir(cur)
        except Exception:
            try:
                smbclient.mkdir(cur)
            except Exception:
                pass


def remote_exists(remote_path: str) -> bool:
    try:
        smbclient.stat(_norm_unc(remote_path))
        return True
    except Exception:
        return False


def upload_file(local_path: Path, remote_path: str) -> None:
    remote_path = _norm_unc(remote_path)
    remote_dir = remote_path.rsplit("\\", 1)[0]
    ensure_remote_dir(remote_dir)

    if (not OVERWRITE) and remote_exists(remote_path):
        print(f"⏭️  Exists, skip: {remote_path}")
        return

    with open(local_path, "rb") as src:
        with smbclient.open_file(remote_path, mode="wb") as dst:
            for chunk in iter(lambda: src.read(1024 * 1024), b""):
                dst.write(chunk)

    print(f"✅ Uploaded: {local_path} -> {remote_path}")


def _first_level_folder(local_path: Path, base_dir: Path) -> Optional[str]:
    rel = local_path.relative_to(base_dir)
    parts = rel.parts
    if len(parts) < 2:
        return None
    return parts[0]


def build_remote_path(
    local_path: Path, base_dir: Path, target_root: str
) -> Optional[str]:
    rel = local_path.relative_to(base_dir)
    parts = rel.parts
    if len(parts) < 2:
        return None

    type_folder = parts[0]
    filename = local_path.name

    return _norm_unc(f"{REMOTE_BASE}\\{target_root}\\{type_folder}\\{filename}")


def iter_files(base_dir: Path) -> Iterable[Path]:
    for p in base_dir.rglob("*"):
        if p.is_file():
            yield p


def upload_tree_mapped(
    base_dir: Path, target_root: str, allowed_types: Set[str]
) -> None:
    base_dir = Path(base_dir)
    if not base_dir.exists():
        print(f"⚠️ Not found: {base_dir}")
        return

    uploaded = 0
    skipped = 0
    ignored = 0

    for p in iter_files(base_dir):
        t = _first_level_folder(p, base_dir)
        if not t:
            ignored += 1
            print(f"⚠️ Ignored (no type folder): {p}")
            continue

        if t not in allowed_types:
            ignored += 1
            print(f"⚠️ Ignored (type not allowed for {target_root}): {t} -> {p}")
            continue

        remote = build_remote_path(p, base_dir, target_root)
        if remote is None:
            ignored += 1
            continue

        if (not OVERWRITE) and remote_exists(remote):
            skipped += 1
            continue

        upload_file(p, remote)
        uploaded += 1

    print(
        f"✅ {base_dir} -> {target_root} done. uploaded={uploaded}, skipped={skipped}, ignored={ignored}"
    )


def main() -> None:
    auth()
    upload_tree_mapped(DATA_DIR, target_root="sentinel", allowed_types=SENTINEL_TYPES)
    upload_tree_mapped(OUTPUT_DIR, target_root="ads", allowed_types=ADS_TYPES)

    print("All done")


if __name__ == "__main__":
    main()
