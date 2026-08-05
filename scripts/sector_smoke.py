"""Verify sectors are selectable on a brand-new database, before any fetch.

Reproduces the first-run path: empty DB -> bootstrap -> type "politics" ->
Select matching -> confirm the filters carry the tag ids.

Run:  .venv/Scripts/python.exe scripts/sector_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from polyclusters.config import AppSettings  # noqa: E402
from polyclusters.core.db import Database  # noqa: E402
from polyclusters.ui.main_window import MainWindow  # noqa: E402

SHOTS = ROOT / "screenshots"


def main() -> int:
    db_file = ROOT / "scripts" / "_sector.duckdb"
    for p in db_file.parent.glob("_sector.duckdb*"):
        p.unlink()

    app = QApplication(sys.argv)
    db = Database(db_file)
    settings = AppSettings()
    win = MainWindow(db, settings)
    win.show()
    controls = win.controls

    print(f"1. Fresh DB — tags in catalogue: {db.stats()['tags']}")
    assert db.stats()["tags"] == 0, "expected an empty catalogue"
    assert controls.tag_list.count() == 0, "picker should start empty"

    print("2. Bootstrapping sector catalogue (this is what first launch does)...")
    worker = win.bootstrap_tags() or win._tag_worker
    worker.wait(180_000)
    app.processEvents()
    controls.reload_tags()

    n_tags = db.stats()["tags"]
    print(f"   catalogue now holds {n_tags:,} sectors")
    assert n_tags > 100, f"bootstrap stored only {n_tags} tags"

    pinned = [
        controls.tag_list.item(i).text()
        for i in range(min(8, controls.tag_list.count()))
    ]
    print(f"3. Pinned at top: {', '.join(t.split('  (')[0] for t in pinned)}")
    assert any("Politics" in t for t in pinned), "Politics should be pinned"
    assert any("Geopolitics" in t for t in pinned), "Geopolitics should be pinned"

    print("4. Typing 'politics' and pressing Select matching...")
    controls.tag_search.setText("politics")
    app.processEvents()
    controls._select_matching()
    app.processEvents()

    ids = controls.selected_tag_ids()
    labels = controls.selected_tag_labels()
    print(f"   selected {len(ids)} sector(s): {labels}")
    assert ids, "Select matching ticked nothing"

    f = controls.filters()
    print(f"5. filters().tag_ids -> {f.tag_ids}")
    assert f.tag_ids == ids, "filters did not carry the ticked sectors"
    assert f.describe().startswith(f"{len(ids)} sector"), f.describe()

    # "geopolitics" is already a substring match for "politics", so a second
    # distinct term is needed to prove selections accumulate.
    print("6. Adding 'economy' as well (accumulates, not replaces)...")
    controls.tag_search.setText("economy")
    app.processEvents()
    controls._select_matching()
    app.processEvents()
    both = controls.selected_tag_labels()
    print(f"   now {len(both)} selected, incl. {[b for b in both if 'conom' in b]}")
    assert len(controls.selected_tag_ids()) > len(ids), "second sector not added"
    assert any("Politics" == b for b in both), "earlier selection was dropped"

    print("7. Selection survives a reload (as after an ingest)...")
    before = set(controls.selected_tag_ids())
    controls.reload_tags()
    app.processEvents()
    assert set(controls.selected_tag_ids()) == before, "selection lost on reload"
    print("   preserved")

    print("8. Selection persists to settings...")
    saved = controls.apply_to_settings()
    assert set(saved.selected_tag_ids) == before, "not written to settings"
    print(f"   settings.selected_tag_ids = {sorted(saved.selected_tag_ids)}")

    print("9. Scope guard: unscoped run prompts, scoped run does not...")
    assert win._confirm_scope(controls.filters()) is True, "scoped run must not prompt"
    controls._clear_tags()
    app.processEvents()
    print(f"   after Clear: '{controls.tag_count.text()}'")
    assert "every sector" in controls.tag_count.text()

    SHOTS.mkdir(exist_ok=True)
    controls.tag_search.setText("")
    controls.reload_tags(keep_selection=False)
    for tag in ("politics", "geopolitics"):
        controls.tag_search.setText(tag)
        controls._select_matching()
    controls.tag_search.setText("")
    app.processEvents()
    win.grab().save(str(SHOTS / "07_sectors.png"))
    print(f"   saved {(SHOTS / '07_sectors.png').relative_to(ROOT)}")

    print("\nSector selection smoke test passed.")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
