from __future__ import annotations

import argparse
import json
import os

from huggingface_hub import snapshot_download

from .config import load_config, resolve_path


def download_models(config_path: str | None = None) -> dict[str, str]:
    """Download every model in the config to its `local_path`.

    Reads `HF_TOKEN` (or `HUGGING_FACE_HUB_TOKEN`) from env for private repos.
    Entries without `repo_id` are skipped with a warning — useful if you've
    already side-loaded a model and only want to verify the others.
    """
    cfg = load_config(config_path)
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    out: dict[str, str] = {}
    skipped: list[str] = []
    for name, spec in cfg["models"].items():
        repo_id = spec.get("repo_id")
        local = resolve_path(spec["local_path"])
        if not repo_id:
            print(f"[download] WARNING: skipping {name} — no repo_id in config. "
                  f"This model will be missing at runtime.")
            skipped.append(name)
            continue
        local.parent.mkdir(parents=True, exist_ok=True)
        snapshot_download(repo_id=repo_id, local_dir=local, token=token)
        out[name] = str(local)
        print(f"[download] {name}: {repo_id} -> {local}")
    if skipped:
        print(f"\n[download] WARNING: {len(skipped)} model(s) skipped: {skipped}")
        print("[download] These will be missing when the worker tries to load them.")
        print("[download] Either add repo_id to runtime.yaml or remove the entry entirely.")
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=None)
    args = parser.parse_args()
    print(json.dumps(download_models(args.config), indent=2))


if __name__ == "__main__":
    main()