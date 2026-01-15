import os
import smbclient
from pathlib import Path
from typing import Optional, List

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


def env_list(name: str, default: Optional[List[str]] = None, sep: str = ",") -> List[str]:
    v = os.getenv(name)
    if not v:
        return default or []
    return [x.strip() for x in v.split(sep) if x.strip()]



SERVER = env_required("SMB_SERVER")
PASSWORD = env_required("SMB_PASSWORD")
DATA_DIR = Path(env_required("SAVE_PATH"))      
OUTPUT_DIR = Path(env_required("OUTPUT_ROOT"))  



REMOTE_BASE = env_required("REMOTE_BASE").replace("/", "\\")

OVERWRITE = env_bool("OVERWRITE", default=False)

candidates = env_list(
    "CANDIDATES",
    default=[
        "x.xafizov@uzspace.org",
        r"uzspace\x.xafizov",
        r"uzspace.org\x.xafizov",
    ],
)


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
    remote_dir = remote_dir.replace("/", "\\")
    if not remote_dir.startswith("\\\\"):
        raise ValueError(f"Remote dir must be UNC path, got: {remote_dir}")

    parts = remote_dir.split("\\")
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
        smbclient.stat(remote_path)
        return True
    except Exception:
        return False


def upload_file(local_path: Path, remote_path: str) -> None:
    remote_path = remote_path.replace("/", "\\")
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



def build_remote_path(local_path: Path, base_dir: Path) -> str:
    """
    data   -> \\...\Rasters\sentinel\<relpath>
    output -> \\...\Rasters\ads\<relpath>
    """
    rel = local_path.relative_to(base_dir)
    rel_str = str(rel).replace("/", "\\")


    if base_dir.resolve() == DATA_DIR.resolve():
        target = "sentinel"
    else:
        target = "ads"

    return f"{REMOTE_BASE}\\{target}\\{rel_str}"


def upload_tree(base_dir: Path) -> None:
    base_dir = Path(base_dir)
    if not base_dir.exists():
        print(f"⚠️ Not found: {base_dir}")
        return

    uploaded = 0
    skipped = 0

    for p in base_dir.rglob("*"):
        if not p.is_file():
            continue

        remote = build_remote_path(p, base_dir)

        if (not OVERWRITE) and remote_exists(remote):
            skipped += 1
            continue

        upload_file(p, remote)
        uploaded += 1

    print(f"✅ {base_dir} done. uploaded={uploaded}, skipped={skipped}")


def main() -> None:
    auth()
    upload_tree(DATA_DIR)

    upload_tree(OUTPUT_DIR)

    print("✅ All done")


if __name__ == "__main__":
    main()
