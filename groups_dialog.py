"""Dialog for manually assigning legs to strategies + renaming groups."""
import uuid
from collections import defaultdict

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox,
    QScrollArea, QWidget, QFrame, QInputDialog, QLineEdit
)
from PyQt6.QtCore import Qt

import theme as T
from models import auto_group_key


NEW_GROUP_SENTINEL = "__new__"


class ManageGroupsDialog(QDialog):
    """
    Lets the user:
      * Assign each leg to a strategy (custom or auto)
      * Create new groups
      * Rename / delete groups
    Result saved to api.save_groups when user accepts.
    """

    def __init__(self, positions, assignments, names, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Organize Strategies")
        self.resize(760, 640)
        self.setStyleSheet(T.BASE_STYLE + f"""
            QDialog {{ background: {T.BG}; }}
            QLabel  {{ background: transparent; border: none; }}
        """)

        self.positions   = positions
        self.assignments = dict(assignments)   # symbol -> group_id
        self.names       = dict(names)         # group_id -> name
        self.combos      = {}                  # symbol -> QComboBox

        self._build_ui()

    # ── UI ──────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(14)

        title = QLabel("Organize Strategies")
        title.setStyleSheet(
            f"color: {T.TEXT}; font-size: 18px; font-weight: bold;"
        )
        root.addWidget(title)

        sub = QLabel(
            "Assign each leg to a strategy. Changes are saved permanently."
        )
        sub.setStyleSheet(f"color: {T.MUTED}; font-size: 12px;")
        root.addWidget(sub)

        # ── Groups section ──
        groups_header = QHBoxLayout()
        groups_header.addWidget(self._section_label("Custom groups"))
        groups_header.addStretch()
        new_btn = QPushButton("+  New group")
        new_btn.clicked.connect(self._create_group)
        new_btn.setFixedHeight(30)
        groups_header.addWidget(new_btn)
        root.addLayout(groups_header)

        self.groups_scroll = QScrollArea()
        self.groups_scroll.setWidgetResizable(True)
        self.groups_scroll.setFixedHeight(160)
        self.groups_scroll.setStyleSheet(
            f"QScrollArea {{ background: {T.CARD}; border: 1px solid {T.BORDER}; "
            f"border-radius: 10px; }}"
        )
        self._refresh_groups_list()
        root.addWidget(self.groups_scroll)

        # ── Legs section ──
        root.addWidget(self._section_label("Legs"))
        legs_scroll = QScrollArea()
        legs_scroll.setWidgetResizable(True)
        legs_scroll.setStyleSheet(
            f"QScrollArea {{ background: {T.CARD}; border: 1px solid {T.BORDER}; "
            f"border-radius: 10px; }}"
        )
        legs_w = QWidget()
        legs_w.setStyleSheet(f"background: {T.CARD};")
        legs_lay = QVBoxLayout(legs_w)
        legs_lay.setContentsMargins(14, 10, 14, 10)
        legs_lay.setSpacing(6)

        for p in sorted(self.positions, key=lambda x: (x.root, x.expires_at or 0, x.strike or 0)):
            legs_lay.addWidget(self._leg_row(p))
        legs_lay.addStretch()
        legs_scroll.setWidget(legs_w)
        root.addWidget(legs_scroll, 1)

        # ── Footer ──
        footer = QHBoxLayout()
        footer.addStretch()
        cancel = QPushButton("Cancel")
        cancel.setProperty("flat", True)
        cancel.setFixedHeight(34)
        cancel.clicked.connect(self.reject)
        cancel.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {T.MUTED}; "
            f"border: 1px solid {T.BORDER}; border-radius: 7px; padding: 0 16px; "
            f"font-weight: normal; }}"
            f"QPushButton:hover {{ color: {T.TEXT}; border-color: {T.BORDER_H}; }}"
        )
        footer.addWidget(cancel)

        save = QPushButton("Save")
        save.setFixedHeight(34)
        save.clicked.connect(self.accept)
        footer.addWidget(save)
        root.addLayout(footer)

    def _section_label(self, text):
        l = QLabel(text.upper())
        l.setStyleSheet(
            f"color: {T.MUTED}; font-size: 11px; font-weight: bold; letter-spacing: 0.7px;"
        )
        return l

    # ── Groups list ─────────────────────────────────────────────────────────

    def _refresh_groups_list(self):
        w = QWidget()
        w.setStyleSheet(f"background: {T.CARD};")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(6)

        custom_group_ids = self._custom_group_ids_in_use()
        if not custom_group_ids:
            empty = QLabel("No custom groups yet. Auto-groups are listed by (root ticker + expiration).")
            empty.setStyleSheet(f"color: {T.MUTED}; font-size: 12px; padding: 10px;")
            empty.setWordWrap(True)
            lay.addWidget(empty)
        else:
            for gid in sorted(custom_group_ids):
                lay.addWidget(self._group_row(gid))

        lay.addStretch()
        self.groups_scroll.setWidget(w)

    def _group_row(self, gid):
        row = QFrame()
        row.setStyleSheet(
            f"QFrame {{ background: {T.BG_ALT}; border: 1px solid {T.BORDER}; "
            f"border-radius: 8px; }}"
        )
        h = QHBoxLayout(row)
        h.setContentsMargins(12, 6, 12, 6)
        h.setSpacing(10)

        leg_count = sum(1 for v in self.assignments.values() if v == gid)

        name = QLabel(self.names.get(gid, "(unnamed)"))
        name.setStyleSheet(f"color: {T.TEXT}; font-size: 13px; font-weight: 600;")
        h.addWidget(name)

        count = QLabel(f"· {leg_count} legs")
        count.setStyleSheet(f"color: {T.MUTED}; font-size: 12px;")
        h.addWidget(count)

        h.addStretch()

        rename = QPushButton("Rename")
        rename.setProperty("flat", True)
        rename.setFixedHeight(26)
        rename.clicked.connect(lambda: self._rename_group(gid))
        rename.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {T.MUTED}; "
            f"border: 1px solid {T.BORDER}; border-radius: 5px; padding: 0 10px; "
            f"font-size: 11px; font-weight: normal; }}"
            f"QPushButton:hover {{ color: {T.ACCENT}; border-color: {T.ACCENT}; }}"
        )
        h.addWidget(rename)

        delete = QPushButton("Delete")
        delete.setProperty("flat", True)
        delete.setFixedHeight(26)
        delete.clicked.connect(lambda: self._delete_group(gid))
        delete.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {T.MUTED}; "
            f"border: 1px solid {T.BORDER}; border-radius: 5px; padding: 0 10px; "
            f"font-size: 11px; font-weight: normal; }}"
            f"QPushButton:hover {{ color: {T.RED}; border-color: {T.RED}; }}"
        )
        h.addWidget(delete)

        return row

    def _custom_group_ids_in_use(self):
        """Return set of custom group ids that either have legs or have a name defined."""
        ids = {v for v in self.assignments.values() if v and not v.startswith("auto:")}
        ids |= {gid for gid in self.names if not gid.startswith("auto:")}
        return ids

    # ── Leg rows ────────────────────────────────────────────────────────────

    def _leg_row(self, p):
        row = QFrame()
        row.setStyleSheet(
            f"QFrame {{ background: {T.BG_ALT}; border: 1px solid {T.BORDER}; "
            f"border-radius: 7px; }}"
        )
        h = QHBoxLayout(row)
        h.setContentsMargins(12, 6, 12, 6)
        h.setSpacing(12)

        side_color = T.TEAL if p.is_long else T.YELLOW
        side = QLabel(p.direction_label.upper())
        side.setStyleSheet(f"color: {side_color}; font-weight: 700; font-size: 11px;")
        side.setFixedWidth(50)
        h.addWidget(side)

        type_color = T.GREEN if p.call_put == "C" else (T.RED if p.call_put == "P" else T.MUTED)
        type_lbl = QLabel(p.type_label)
        type_lbl.setStyleSheet(f"color: {type_color}; font-weight: 600; font-size: 12px;")
        type_lbl.setFixedWidth(50)
        h.addWidget(type_lbl)

        desc_bits = []
        if p.strike:
            desc_bits.append(f"${p.strike:g}")
        if p.expires_at:
            desc_bits.append(p.expires_at.strftime("%b %d %y"))
        desc_bits.append(f"× {p.quantity:g}")
        desc = QLabel(f"{p.root}  " + "  ".join(desc_bits))
        desc.setStyleSheet(f"color: {T.TEXT_DIM}; font-size: 12px;")
        h.addWidget(desc, 1)

        sym = QLabel(p.symbol)
        sym.setStyleSheet(f"color: {T.MUTED}; font-size: 10px;")
        sym.setFixedWidth(260)
        h.addWidget(sym)

        combo = QComboBox()
        combo.setFixedWidth(180)
        self._populate_combo(combo, p)
        combo.currentIndexChanged.connect(lambda _i, sym=p.symbol, c=combo: self._on_combo_changed(sym, c))
        self.combos[p.symbol] = combo
        h.addWidget(combo)

        return row

    def _populate_combo(self, combo, p):
        combo.blockSignals(True)
        combo.clear()

        # Always-present: Auto (root + exp)
        auto_id = auto_group_key(p)
        auto_label = self._auto_group_label(p)
        combo.addItem(f"● {auto_label}  (auto)", auto_id)

        # Custom groups
        for gid in sorted(self._custom_group_ids_in_use()):
            combo.addItem(f"◆ {self.names.get(gid, '(unnamed)')}", gid)

        combo.addItem("＋  New group…", NEW_GROUP_SENTINEL)

        # Set current selection
        current = self.assignments.get(p.symbol) or auto_id
        idx = combo.findData(current)
        if idx >= 0:
            combo.setCurrentIndex(idx)
        combo.blockSignals(False)

    def _auto_group_label(self, p):
        exp = p.expires_at.strftime("%b %d, %Y") if p.expires_at else "No expiry"
        return f"{p.root} · {exp}"

    # ── Handlers ────────────────────────────────────────────────────────────

    def _on_combo_changed(self, symbol, combo):
        data = combo.currentData()
        if data == NEW_GROUP_SENTINEL:
            gid = self._prompt_new_group()
            if gid:
                self.assignments[symbol] = gid
            else:
                pass  # restore below
            # Rebuild all combos to reflect new group
            self._refresh_all_combos()
            self._refresh_groups_list()
            return

        # Auto: remove override
        p = next((x for x in self.positions if x.symbol == symbol), None)
        if p and data == auto_group_key(p):
            self.assignments.pop(symbol, None)
        else:
            self.assignments[symbol] = data

        self._refresh_groups_list()
        self._refresh_all_combos(skip=symbol)

    def _create_group(self):
        self._prompt_new_group()
        self._refresh_groups_list()
        self._refresh_all_combos()

    def _prompt_new_group(self):
        name, ok = QInputDialog.getText(self, "New group", "Name:")
        if not ok:
            return None
        name = name.strip()
        if not name:
            return None
        gid = "g_" + uuid.uuid4().hex[:10]
        self.names[gid] = name
        return gid

    def _rename_group(self, gid):
        new, ok = QInputDialog.getText(
            self, "Rename group", "New name:", text=self.names.get(gid, "")
        )
        if ok and new.strip():
            self.names[gid] = new.strip()
            self._refresh_groups_list()
            self._refresh_all_combos()

    def _delete_group(self, gid):
        # Remove all assignments to this group + the name
        self.assignments = {k: v for k, v in self.assignments.items() if v != gid}
        self.names.pop(gid, None)
        self._refresh_groups_list()
        self._refresh_all_combos()

    def _refresh_all_combos(self, skip=None):
        for sym, combo in self.combos.items():
            if sym == skip:
                continue
            p = next((x for x in self.positions if x.symbol == sym), None)
            if p:
                self._populate_combo(combo, p)

    # ── Result ──────────────────────────────────────────────────────────────

    def result_payload(self):
        # Clean up empty group names with no legs
        used_ids = {v for v in self.assignments.values() if v}
        cleaned_names = {
            gid: name for gid, name in self.names.items()
            if gid in used_ids or gid.startswith("auto:")
        }
        return self.assignments, cleaned_names
