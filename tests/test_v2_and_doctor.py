"""memory_v2 dedup + caps, voice doctor report."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _v2(tmp_path):
    from core.config import Config
    from core.memory_v2 import MemoryV2

    cfg = Config.__new__(Config)
    cfg.db_path = tmp_path / "state.db"
    return MemoryV2(cfg)


def test_promote_no_duplicates(tmp_path):
    v2 = _v2(tmp_path)
    v2.add_episodic(
        event_type="fact", content="Paris is the capital of France", context={}, importance=0.95
    )
    v2.consolidate()
    v2.consolidate()
    n = v2.conn.execute("SELECT COUNT(*) FROM semantic WHERE subject LIKE '%Paris%'").fetchone()[0]
    assert n == 1, f"duplicated {n}x"


def test_prune_caps(tmp_path):
    v2 = _v2(tmp_path)
    v2.importance_threshold = 0.99  # prune almost everything episodic
    for i in range(5):
        v2.add_episodic(event_type="chat", content=f"msg {i}", context={}, importance=0.01)
    v2.consolidate()
    left = v2.conn.execute("SELECT COUNT(*) FROM episodic").fetchone()[0]
    assert left == 0


async def test_doctor_report():
    from core.tools import ToolBox
    from skills.voice_doctor import register_voice_doctor

    box = ToolBox()
    register_voice_doctor(box)
    out = await box.execute("voice_doctor", "{}")
    assert "Voice doctor" in out and "Kokoro" in out


def test_voice_pack_registered():
    from core.tools import PACK_KEYWORDS, PACK_PREFIXES

    assert "voice_doctor" in str(PACK_PREFIXES.get("voice"))
    assert "voice" in PACK_KEYWORDS
