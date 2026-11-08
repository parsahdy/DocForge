"""One-time setup of the bucket and the vector collection: python -m scripts.init_infra"""

from app.core.config import get_settings
from app.services.storage import Storage
from app.services.vectors import VectorStore


def main() -> None:
    settings = get_settings()
    Storage(settings).ensure_bucket()
    VectorStore.from_settings(settings).ensure_collection()
    print("bucket and collection are ready")


if __name__ == "__main__":
    main()