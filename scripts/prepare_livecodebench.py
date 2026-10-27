"""Download and verify the pinned public benchmark; never calls a model."""

from coding_eval.livecodebench import catalog_counts, prepare_catalog
from coding_eval.storage import AppPaths

if __name__ == "__main__":
    root = AppPaths.from_env().root
    print(prepare_catalog(root, lambda message: print(message, flush=True)))
    print(catalog_counts(root))
