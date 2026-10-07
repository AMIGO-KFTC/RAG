import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "samples"))

import make_samples  # noqa: E402

from amigo_rag import KnowledgeBase, RAGSettings  # noqa: E402


@pytest.fixture(scope="session")
def sample_dir(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("samples")
    make_samples.make_all(out)
    return out


@pytest.fixture()
def kb() -> KnowledgeBase:
    """테스트마다 새 메모리 지식베이스."""
    return KnowledgeBase(f"test-{uuid.uuid4().hex[:8]}", settings=RAGSettings.in_memory())
