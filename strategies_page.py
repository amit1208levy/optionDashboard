"""Configure page: browse template catalog, build strategies, manage history."""
import uuid
from datetime import datetime, timezone

from PyQt6.QtCore import Qt, pyqtSignal, QThread
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QFrame,
    QScrollArea, QListWidget, QListWidgetItem, QCheckBox, QDialog,
    QDialogButtonBox, QDoubleSpinBox, QSpinBox, QDateEdit, QComboBox,
    QMessageBox, QFormLayout, QGridLayout, QSizePolicy
)
from PyQt6.QtCore import QDate

import theme as T
import api
import strategies as tmpl_mod
from models import (
    StrategyInstance, unassigned_positions, strategy_performance,
    transactions_to_closed_lots, merge_history
)
from strategy_card import money, pnl_color


# ── Helpers ──────────────────────────────────────────────────────────────────

def _card_frame(radius=12, pad=(16, 14, 16, 14)):
    f = QFrame()
    f.setStyleSheet(
        f"QFrame {{ background: {T.CARD}; border: 1px solid {T.BORDER}; "
        f"border-radius: {radius}px; }}"
    )
    lay = QVBoxLayout(f)
    lay.setContentsMargins(*pad)
    lay.setSpacing(8)
    return f, lay


def _btn(text, primary=False, danger=False):
    b = QPushButton(text)
    b.setFixedHeight(30)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    if primary:
        b.setStyleSheet(
            f"QPushButton {{ background: {T.PURPLE}; color: white; border: none; "
            f"border-radius: 6px; font-size: 12px; font-weight: bold; padding: 0 14px; }}"
            f"QPushButton:hover {{ background: {T.PURPLE2}; }}"
            f"QPushButton:disabled {{ background: #374151; color: #6b7280; }}"
        )
    elif danger:
        b.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {T.RED}; "
            f"border: 1px solid {T.RED}; border-radius: 6px; font-size: 12px; padding: 0 12px; }}"
            f"QPushButton:hover {{ background: rgba(239,68,68,0.12); }}"
        )
    else:
        b.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {T.TEXT}; "
            f"border: 1px solid {T.BORDER}; border-radius: 6px; font-size: 12px; padding: 0 12px; }}"
            f"QPushButton:hover {{ border-color: {T.ACCENT}; color: {T.ACCENT}; }}"
        )
    return b


def _leg_summary(pos):
    if pos.is_option:
        k = f"{pos.strike:g}" if pos.strike else "—"
        exp = pos.expires_at.strftime("%b %d") if pos.expires_at else "—"
        return (f"{pos.direction_label} {int(pos.quantity)} "
                f"{pos.call_put} {k} {exp}  ·  {pos.root}")
    return f"{pos.direction_label} {int(pos.quantity)} {pos.root} Shares"


# ── Activity import worker ───────────────────────────────────────────────────

class ActivityImportWorker(QThread):
    done = pyqtSignal(list, str)  # lots, error

    def __init__(self, token, account_number):
        super().__init__()
        self.token = token
        self.account_number = account_number

    def run(self):
        try:
            txns = api.get_transactions(self.token, self.account_number)
            lots = transactions_to_closed_lots(txns)
            self.done.emit(lots, "")
        except Exception as e:
            self.done.emit([], str(e))


# ── Template catalog card ────────────────────────────────────────────────────

class TemplateCard(QFrame):
    use_clicked = pyqtSignal(str)  # template_key

    def __init__(self, tmpl, parent=None):
        super().__init__(parent)
        self.tmpl = tmpl
        self.setStyleSheet(
            f"QFrame {{ background: {T.CARD}; border: 1px solid {T.BORDER}; "
            f"border-radius: 12px; }}"
            f"QFrame:hover {{ border-color: {T.PURPLE}; }}"
        )
        self.setMinimumWidth(320)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(6)

        top = QHBoxLayout()
        name = QLabel(tmpl.name)
        name.setStyleSheet(
            f"color: {T.TEXT}; font-size: 15px; font-weight: bold; "
            f"border: none; background: transparent;"
        )
        top.addWidget(name)
        top.addStretch()
        outlook = QLabel(tmpl.outlook)
        outlook.setStyleSheet(
            f"color: {self._outlook_color(tmpl.outlook)}; font-size: 11px; "
            f"font-weight: bold; border: none; background: transparent;"
        )
        top.addWidget(outlook)
        lay.addLayout(top)

        cat = QLabel(f"{tmpl.category}  ·  {tmpl.risk} risk")
        cat.setStyleSheet(
            f"color: {T.MUTED}; font-size: 11px; border: none; background: transparent;"
        )
        lay.addWidget(cat)

        desc = QLabel(tmpl.description)
        desc.setWordWrap(True)
        desc.setStyleSheet(
            f"color: {T.TEXT_DIM}; font-size: 12px; border: none; "
            f"background: transparent; margin-top: 6px;"
        )
        lay.addWidget(desc)

        setup = QLabel(f"Setup: {tmpl.setup}")
        setup.setWordWrap(True)
        setup.setStyleSheet(
            f"color: {T.LABEL}; font-size: 11px; border: none; "
            f"background: transparent; margin-top: 6px;"
        )
        lay.addWidget(setup)

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(2)
        for i, (k, v) in enumerate([
            ("Max profit", tmpl.max_profit),
            ("Max loss",   tmpl.max_loss),
            ("Capital",    tmpl.capital_note),
            ("Ideal when", tmpl.ideal_when),
        ]):
            kl = QLabel(k)
            kl.setStyleSheet(
                f"color: {T.MUTED}; font-size: 10px; font-weight: bold; "
                f"letter-spacing: 0.5px; border: none; background: transparent;"
            )
            vl = QLabel(v)
            vl.setWordWrap(True)
            vl.setStyleSheet(
                f"color: {T.TEXT_DIM}; font-size: 11px; border: none; background: transparent;"
            )
            grid.addWidget(kl, i, 0)
            grid.addWidget(vl, i, 1)
        grid.setColumnStretch(1, 1)
        lay.addSpacing(4)
        lay.addLayout(grid)

        btn = _btn("＋ Create from this template", primary=True)
        btn.clicked.connect(lambda: self.use_clicked.emit(tmpl.key))
        lay.addSpacing(8)
        lay.addWidget(btn)

    @staticmethod
    def _outlook_color(outlook):
        o = (outlook or "").lower()
        if "bull" in o: return T.GREEN
        if "bear" in o: return T.RED
        if "volat" in o: return T.YELLOW
        if "income" in o or "neutral" in o: return T.TEAL
        return T.TEXT_DIM


# ── Builder dialog: pick legs for a template ─────────────────────────────────

class StrategyBuilderDialog(QDialog):
    """
    Two-section leg picker:
      1. Open positions   – checkboxes for live portfolio legs (optional)
      2. Planned legs     – type any symbol to plan the strategy before opening it

    Creating with zero legs is allowed; the strategy will show a "0/N legs" warning
    and can be fully assigned later from the main portfolio or by editing.
    """

    def __init__(self, template, available_legs, existing=None, parent=None):
        super().__init__(parent)
        self.template       = template
        self.available_legs = available_legs
        self.existing       = existing
        self._planned_syms: list[str] = []   # manually entered planned symbols

        self.setStyleSheet(T.BASE_STYLE)
        self.setWindowTitle(
            f"{'Edit' if existing else 'Create'} Strategy — {template.name}"
        )
        self.setMinimumSize(640, 580)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 20)
        root.setSpacing(8)

        # ── Header ────────────────────────────────────────────────────────────
        title = QLabel(template.name)
        title.setStyleSheet(
            f"color: {T.ACCENT}; font-size: 18px; font-weight: bold; "
            f"border: none; background: transparent;"
        )
        root.addWidget(title)

        sub = QLabel(
            f"{template.category}  ·  {template.outlook}  ·  {template.risk} risk"
        )
        sub.setStyleSheet(
            f"color: {T.MUTED}; font-size: 12px; border: none; background: transparent;"
        )
        root.addWidget(sub)

        if template.legs:
            spec = QLabel(
                "Expected legs: " + "  ·  ".join(leg.label for leg in template.legs)
            )
            spec.setWordWrap(True)
            spec.setStyleSheet(
                f"color: {T.LABEL}; font-size: 12px; border: none; background: transparent;"
            )
            root.addWidget(spec)

        root.addSpacing(4)

        # ── Name ──────────────────────────────────────────────────────────────
        name_hdr = QLabel("Strategy name")
        name_hdr.setStyleSheet(
            f"color: {T.LABEL}; font-size: 11px; font-weight: bold; border: none;"
        )
        root.addWidget(name_hdr)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText(f"e.g. {template.name} on SPY")
        if existing and existing.custom_name:
            self.name_edit.setText(existing.custom_name)
        root.addWidget(self.name_edit)

        root.addSpacing(6)

        # ── Work out which existing legs are live vs missing ──────────────────
        avail_syms    = {p.symbol for p in available_legs}
        pre_selected  = set()    # existing live legs to pre-check
        pre_planned   = []       # existing legs no longer in portfolio

        if existing:
            for s in existing.leg_symbols:
                if s in avail_syms:
                    pre_selected.add(s)
                else:
                    pre_planned.append(s)

        # ── Section 1: Open positions ─────────────────────────────────────────
        pos_hdr = QLabel(
            f"Assign open positions  "
            f"({'%d available' % len(available_legs) if available_legs else 'none in portfolio'})"
        )
        pos_hdr.setStyleSheet(
            f"color: {T.LABEL}; font-size: 12px; font-weight: bold; border: none;"
        )
        root.addWidget(pos_hdr)

        pos_scroll = QScrollArea()
        pos_scroll.setWidgetResizable(True)
        pos_scroll.setFrameShape(QFrame.Shape.NoFrame)
        pos_box = QWidget()
        pos_vl  = QVBoxLayout(pos_box)
        pos_vl.setContentsMargins(4, 4, 4, 4)
        pos_vl.setSpacing(4)

        self._checks: list[QCheckBox] = []
        for leg in available_legs:
            cb = QCheckBox(_leg_summary(leg))
            cb.setProperty("symbol", leg.symbol)
            cb.setStyleSheet(
                f"QCheckBox {{ color: {T.TEXT}; font-size: 12px; "
                f"padding: 6px 8px; border-radius: 6px; }}"
                f"QCheckBox:hover {{ background: #1a1f2e; }}"
            )
            if leg.symbol in pre_selected:
                cb.setChecked(True)
            self._checks.append(cb)
            pos_vl.addWidget(cb)

        if not available_legs:
            note = QLabel(
                "No open positions in portfolio right now.  "
                "You can still create this strategy and assign legs later — "
                "use the Planned legs section below, or come back and edit once "
                "you've opened trades."
            )
            note.setWordWrap(True)
            note.setStyleSheet(
                f"color: {T.MUTED}; font-size: 12px; padding: 12px; "
                f"border: 1px dashed {T.BORDER}; border-radius: 8px; "
                f"background: transparent;"
            )
            pos_vl.addWidget(note)

        pos_vl.addStretch()
        pos_scroll.setWidget(pos_box)
        h = min(160, max(60, len(available_legs) * 40 + 16))
        pos_scroll.setFixedHeight(h)
        root.addWidget(pos_scroll)

        root.addSpacing(6)

        # ── Separator ─────────────────────────────────────────────────────────
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"background: {T.BORDER}; max-height: 1px; border: none;")
        root.addWidget(sep)

        root.addSpacing(2)

        # ── Section 2: Planned legs ───────────────────────────────────────────
        plan_hdr_row = QHBoxLayout()
        plan_hdr = QLabel("Planned legs")
        plan_hdr.setStyleSheet(
            f"color: {T.LABEL}; font-size: 12px; font-weight: bold; border: none;"
        )
        plan_hdr_row.addWidget(plan_hdr)
        plan_hint = QLabel(
            "Add any symbol you plan to trade — they'll be linked automatically "
            "once you open the position."
        )
        plan_hint.setStyleSheet(
            f"color: {T.MUTED}; font-size: 11px; border: none;"
        )
        plan_hdr_row.addWidget(plan_hint, 1)
        root.addLayout(plan_hdr_row)

        add_row = QHBoxLayout()
        add_row.setSpacing(8)
        self._sym_input = QLineEdit()
        self._sym_input.setPlaceholderText(
            "Ticker or full symbol  (e.g. SPY, AAPL, /MES, AAPL  241220C00200000)"
        )
        self._sym_input.returnPressed.connect(self._add_planned)
        add_row.addWidget(self._sym_input)
        add_btn = _btn("＋ Add")
        add_btn.clicked.connect(self._add_planned)
        add_row.addWidget(add_btn)
        root.addLayout(add_row)

        self._planned_frame = QFrame()
        self._planned_frame.setStyleSheet("background: transparent; border: none;")
        self._planned_vl = QVBoxLayout(self._planned_frame)
        self._planned_vl.setContentsMargins(0, 2, 0, 0)
        self._planned_vl.setSpacing(4)
        root.addWidget(self._planned_frame)

        # Pre-populate from missing existing legs
        for sym in pre_planned:
            self._add_planned_sym(sym, missing=True)

        # ── Status + buttons ──────────────────────────────────────────────────
        self.status = QLabel("")
        self.status.setStyleSheet(
            f"color: {T.YELLOW}; font-size: 11px; border: none;"
        )
        root.addWidget(self.status)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self._on_ok)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

    # ── Planned leg management ─────────────────────────────────────────────────

    def _add_planned(self):
        sym = self._sym_input.text().strip().upper()
        if not sym:
            return
        self._sym_input.clear()
        self._add_planned_sym(sym)

    def _add_planned_sym(self, sym: str, missing: bool = False):
        if sym in self._planned_syms:
            return
        self._planned_syms.append(sym)

        row = QFrame()
        row.setStyleSheet(
            f"QFrame {{ background: #12151d; border: 1px solid {T.BORDER}; "
            f"border-radius: 6px; }}"
        )
        rl = QHBoxLayout(row)
        rl.setContentsMargins(10, 5, 6, 5)
        rl.setSpacing(8)

        lbl = QLabel(sym)
        lbl.setStyleSheet(
            f"color: {T.ACCENT}; font-size: 13px; font-weight: bold; "
            f"border: none; background: transparent;"
        )
        rl.addWidget(lbl)
        rl.addStretch()

        badge_text  = "was in portfolio" if missing else "planned"
        badge_color = T.YELLOW if missing else T.MUTED
        badge = QLabel(badge_text)
        badge.setStyleSheet(
            f"color: {badge_color}; font-size: 10px; border: none; background: transparent;"
        )
        rl.addWidget(badge)

        rm = QPushButton("✕")
        rm.setFixedSize(22, 22)
        rm.setCursor(Qt.CursorShape.PointingHandCursor)
        rm.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {T.MUTED}; "
            f"border: none; font-size: 13px; border-radius: 4px; }}"
            f"QPushButton:hover {{ color: {T.RED}; background: #2a1010; }}"
        )
        rm.clicked.connect(lambda _, s=sym, r=row: self._remove_planned(s, r))
        rl.addWidget(rm)

        self._planned_vl.addWidget(row)

    def _remove_planned(self, sym: str, row_widget: QFrame):
        if sym in self._planned_syms:
            self._planned_syms.remove(sym)
        row_widget.deleteLater()

    # ── Accept ─────────────────────────────────────────────────────────────────

    def _on_ok(self):
        pos_syms  = [cb.property("symbol") for cb in self._checks if cb.isChecked()]
        all_syms  = pos_syms + list(self._planned_syms)
        expected  = len(self.template.legs)

        if expected > 0 and len(all_syms) != expected:
            arrow = "fewer" if len(all_syms) < expected else "more"
            self.status.setText(
                f"Template expects {expected} leg{'s' if expected != 1 else ''}, "
                f"you have {len(all_syms)} ({arrow}) — saving anyway. "
                f"Edit anytime to complete the strategy."
            )
            # Not blocking — save with whatever legs are configured

        self._result = {
            "symbols": all_syms,
            "name":    self.name_edit.text().strip(),
        }
        self.accept()

    def result_data(self):
        return getattr(self, "_result", None)


# ── Past closed-leg picker ───────────────────────────────────────────────────

def _history_label(h):
    side = "Long" if (h.get("sign") or 0) > 0 else "Short"
    cp = {"C": "Call", "P": "Put"}.get(h.get("call_put"), "Stock")
    k  = f"{h.get('strike', 0):g}" if h.get("strike") else ""
    # Format date as "Jan 15, 2025" instead of raw ISO string
    raw_date = h.get("closed_at") or ""
    try:
        dt = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
        date_str = dt.strftime("%b %d, %Y")
    except Exception:
        date_str = raw_date[:10] if raw_date else "—"
    return (f"{date_str}  ·  {side} {int(h.get('qty') or 0)} "
            f"{h.get('root') or ''} {cp} {k}")


class PastLegPickerDialog(QDialog):
    def __init__(self, strategy_id, history, strategies_raw, parent=None):
        super().__init__(parent)
        self.strategy_id = strategy_id
        self.history = history
        self.setStyleSheet(T.BASE_STYLE)
        self.setWindowTitle("Add past closed legs")
        self.setMinimumSize(600, 560)

        strat_names = {s["id"]: (s.get("name") or "") for s in strategies_raw}

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 20)
        root.setSpacing(10)

        title = QLabel("Pick closed legs to add to this strategy")
        title.setStyleSheet(
            f"color: {T.ACCENT}; font-size: 15px; font-weight: bold; border: none;"
        )
        root.addWidget(title)

        sub = QLabel("Legs already assigned to this strategy are hidden. "
                     "Picking a leg currently assigned elsewhere will move it here.")
        sub.setWordWrap(True)
        sub.setStyleSheet(f"color: {T.MUTED}; font-size: 11px; border: none;")
        root.addWidget(sub)

        # Search bar
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search by ticker, type, date…")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._filter)
        root.addWidget(self._search)

        # Count label
        self._count_lbl = QLabel("")
        self._count_lbl.setStyleSheet(
            f"color: {T.MUTED}; font-size: 11px; border: none;"
        )
        root.addWidget(self._count_lbl)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self._box = QWidget()
        self._vl = QVBoxLayout(self._box)
        self._vl.setContentsMargins(4, 4, 4, 4)
        self._vl.setSpacing(4)

        self._rows = []       # list of (QFrame row_w, QCheckBox cb, str search_text)
        self._pool = sorted(
            [h for h in history if h.get("strategy_id") != strategy_id],
            key=lambda e: e.get("closed_at") or "",
            reverse=True,
        )

        if not self._pool:
            note = QLabel("No closed legs recorded yet. Legs auto-log here when they "
                          "leave the portfolio (e.g. expired, closed, or rolled).")
            note.setWordWrap(True)
            note.setStyleSheet(f"color: {T.MUTED}; font-size: 12px; padding: 18px;")
            self._vl.addWidget(note)
        else:
            for entry in self._pool:
                label_text = _history_label(entry)
                search_text = " ".join([
                    entry.get("root") or "",
                    entry.get("symbol") or "",
                    entry.get("call_put") or "",
                    {"C": "Call", "P": "Put"}.get(entry.get("call_put"), "Stock"),
                    str(entry.get("strike") or ""),
                    (entry.get("closed_at") or "")[:10],
                ]).lower()

                row_w = QFrame()
                row_w.setStyleSheet(
                    f"QFrame {{ background: #12151d; border: 1px solid {T.BORDER}; "
                    f"border-radius: 6px; }}"
                    f"QFrame:hover {{ border-color: {T.PURPLE}; }}"
                )
                hl = QHBoxLayout(row_w)
                hl.setContentsMargins(10, 6, 10, 6)

                cb = QCheckBox(label_text)
                cb.setStyleSheet(
                    f"QCheckBox {{ color: {T.TEXT}; font-size: 12px; background: transparent; }}"
                )
                cb.setProperty("symbol", entry["symbol"])
                hl.addWidget(cb)
                hl.addStretch()

                assigned = entry.get("strategy_id")
                if assigned:
                    badge = QLabel(f"→ {strat_names.get(assigned, assigned)[:24]}")
                    badge.setStyleSheet(
                        f"color: {T.YELLOW}; font-size: 10px; border: none;"
                    )
                    hl.addWidget(badge)
                else:
                    badge = QLabel("unassigned")
                    badge.setStyleSheet(
                        f"color: {T.MUTED}; font-size: 10px; border: none;"
                    )
                    hl.addWidget(badge)

                pnl = entry.get("pnl") or 0
                pl = QLabel(money(pnl, signed=True))
                pl.setStyleSheet(
                    f"color: {pnl_color(pnl)}; font-size: 12px; font-weight: bold; "
                    f"border: none; background: transparent;"
                )
                hl.addWidget(pl)

                self._vl.addWidget(row_w)
                self._rows.append((row_w, cb, search_text))

        self._vl.addStretch()
        scroll.setWidget(self._box)
        root.addWidget(scroll, 1)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

        self._update_count()

    def _filter(self, text):
        q = text.strip().lower()
        for row_w, cb, search_text in self._rows:
            visible = not q or q in search_text
            row_w.setVisible(visible)
        self._update_count()

    def _update_count(self):
        q = self._search.text().strip().lower()
        visible = sum(1 for row_w, _, st in self._rows if not q or q in st)
        total   = len(self._rows)
        if total == 0:
            self._count_lbl.setText("")
        elif q:
            self._count_lbl.setText(f"{visible} of {total} legs match")
        else:
            self._count_lbl.setText(f"{total} closed leg{'s' if total != 1 else ''}")

    def selected_symbols(self):
        return [cb.property("symbol") for _, cb, _ in self._rows if cb.isChecked()]


# ── Active strategy row ──────────────────────────────────────────────────────

class ActiveStrategyRow(QFrame):
    edit_requested    = pyqtSignal(str)
    delete_requested  = pyqtSignal(str)
    history_requested = pyqtSignal(str)

    def __init__(self, instance, perf, parent=None):
        super().__init__(parent)
        self.instance = instance
        self.setStyleSheet(
            f"QFrame {{ background: {T.CARD}; border: 1px solid {T.BORDER}; "
            f"border-radius: 10px; }}"
        )

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(6)

        top = QHBoxLayout()
        name = QLabel(instance.name)
        name.setStyleSheet(
            f"color: {T.TEXT}; font-size: 14px; font-weight: bold; "
            f"border: none; background: transparent;"
        )
        top.addWidget(name)

        tmpl_name = instance.template.name if instance.template else "Custom"
        badge = QLabel(tmpl_name)
        badge.setStyleSheet(
            f"color: {T.PURPLE}; font-size: 10px; font-weight: bold; "
            f"border: 1px solid {T.PURPLE}; border-radius: 4px; "
            f"padding: 2px 6px; background: transparent;"
        )
        top.addWidget(badge)

        top.addStretch()

        pnl = instance.pnl
        pl = QLabel(money(pnl, signed=True))
        pl.setStyleSheet(
            f"color: {pnl_color(pnl)}; font-size: 14px; font-weight: bold; "
            f"border: none; background: transparent;"
        )
        top.addWidget(pl)

        lay.addLayout(top)

        # Legs summary
        if instance.legs:
            for leg in instance.legs:
                ll = QLabel("  · " + _leg_summary(leg))
                ll.setStyleSheet(
                    f"color: {T.TEXT_DIM}; font-size: 11px; border: none; "
                    f"background: transparent;"
                )
                lay.addWidget(ll)
        if instance.missing_legs:
            ml = QLabel(f"  · {len(instance.missing_legs)} leg(s) no longer in portfolio")
            ml.setStyleSheet(
                f"color: {T.RED}; font-size: 11px; border: none; background: transparent;"
            )
            lay.addWidget(ml)

        # Performance stats
        if perf:
            stats = (f"Closed legs: {perf['closed_legs']}   "
                     f"Total P&L: {money(perf['total_pnl'], signed=True)}   "
                     f"Avg weekly: {money(perf['avg_weekly'], signed=True) if perf['avg_weekly'] is not None else '—'}   "
                     f"Win rate: {perf['win_rate']:.0f}%   "
                     f"Avg DIT: {perf['avg_dit']:.0f}d" if perf['avg_dit'] is not None else "")
            sl = QLabel(stats)
            sl.setStyleSheet(
                f"color: {T.TEAL}; font-size: 11px; border: none; "
                f"background: transparent; margin-top: 4px;"
            )
            lay.addWidget(sl)

        # Actions
        row = QHBoxLayout()
        row.addStretch()
        b_hist = _btn("History")
        b_hist.clicked.connect(lambda: self.history_requested.emit(instance.id))
        row.addWidget(b_hist)
        b_edit = _btn("Edit legs")
        b_edit.clicked.connect(lambda: self.edit_requested.emit(instance.id))
        row.addWidget(b_edit)
        b_del = _btn("Delete", danger=True)
        b_del.clicked.connect(lambda: self.delete_requested.emit(instance.id))
        row.addWidget(b_del)
        lay.addLayout(row)


# ── History dialog ───────────────────────────────────────────────────────────

class HistoryDialog(QDialog):
    def __init__(self, instance, portfolio, parent=None):
        super().__init__(parent)
        self.instance  = instance
        self.portfolio = portfolio
        self.history   = portfolio.history
        self.setStyleSheet(T.BASE_STYLE)
        self.setWindowTitle(f"History — {instance.name}")
        self.setMinimumSize(640, 520)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 20)
        root.setSpacing(10)

        title = QLabel(instance.name)
        title.setStyleSheet(
            f"color: {T.ACCENT}; font-size: 17px; font-weight: bold; border: none;"
        )
        root.addWidget(title)

        entries = [h for h in self.history if h.get("strategy_id") == instance.id]
        perf = strategy_performance(instance.id, self.history)

        if perf:
            grid = QGridLayout()
            grid.setHorizontalSpacing(18)
            grid.setVerticalSpacing(4)
            items = [
                ("Closed legs",  str(perf["closed_legs"])),
                ("Total P&L",    money(perf["total_pnl"], signed=True)),
                ("Avg weekly",   money(perf["avg_weekly"], signed=True)
                                 if perf["avg_weekly"] is not None else "—"),
                ("Avg monthly",  money(perf["avg_monthly"], signed=True)
                                 if perf["avg_monthly"] is not None else "—"),
                ("Yearly pace",  money(perf["yearly"], signed=True)
                                 if perf["yearly"] is not None else "—"),
                ("Win rate",     f"{perf['win_rate']:.0f}%"),
                ("Avg DIT",      f"{perf['avg_dit']:.0f}d"
                                 if perf["avg_dit"] is not None else "—"),
            ]
            for i, (k, v) in enumerate(items):
                kl = QLabel(k)
                kl.setStyleSheet(
                    f"color: {T.MUTED}; font-size: 10px; font-weight: bold; "
                    f"border: none; background: transparent;"
                )
                vl = QLabel(v)
                vl.setStyleSheet(
                    f"color: {T.TEXT}; font-size: 14px; font-weight: bold; "
                    f"border: none; background: transparent;"
                )
                grid.addWidget(kl, (i//4)*2,     i%4)
                grid.addWidget(vl, (i//4)*2 + 1, i%4)
            root.addLayout(grid)
        else:
            ne = QLabel("No closed legs recorded yet.")
            ne.setStyleSheet(f"color: {T.MUTED}; font-size: 12px; border: none;")
            root.addWidget(ne)

        root.addSpacing(6)
        hdr = QLabel("Closed legs")
        hdr.setStyleSheet(
            f"color: {T.LABEL}; font-size: 12px; font-weight: bold; border: none;"
        )
        root.addWidget(hdr)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        vl = QVBoxLayout(body)
        vl.setContentsMargins(4, 4, 4, 4)
        vl.setSpacing(4)
        for h in sorted(entries, key=lambda e: e.get("closed_at") or "", reverse=True):
            side = "Long" if (h.get("sign") or 0) > 0 else "Short"
            cp = {"C": "Call", "P": "Put"}.get(h.get("call_put"), "Stock")
            k  = f"{h.get('strike', 0):g}" if h.get("strike") else ""
            src = "manual" if h.get("source") == "manual" else "auto"
            pnl = h.get("pnl") or 0.0
            row = QFrame()
            row.setStyleSheet(
                f"QFrame {{ background: #12151d; border: 1px solid {T.BORDER}; "
                f"border-radius: 6px; }}"
            )
            hl = QHBoxLayout(row)
            hl.setContentsMargins(10, 6, 10, 6)
            label = QLabel(
                f"{h.get('closed_at', '—')}  ·  {side} {int(h.get('qty') or 0)} "
                f"{h.get('root') or ''} {cp} {k}  ·  {src}"
            )
            label.setStyleSheet(
                f"color: {T.TEXT_DIM}; font-size: 11px; border: none; background: transparent;"
            )
            hl.addWidget(label)
            hl.addStretch()
            pl = QLabel(money(pnl, signed=True))
            pl.setStyleSheet(
                f"color: {pnl_color(pnl)}; font-size: 12px; font-weight: bold; "
                f"border: none; background: transparent;"
            )
            hl.addWidget(pl)
            vl.addWidget(row)
        vl.addStretch()
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        bar = QHBoxLayout()
        add_btn = _btn("＋ Add past closed legs", primary=True)
        add_btn.clicked.connect(self._add_from_pool)
        bar.addWidget(add_btn)
        bar.addStretch()
        close_btn = _btn("Close")
        close_btn.clicked.connect(self.accept)
        bar.addWidget(close_btn)
        root.addLayout(bar)

    def _add_from_pool(self):
        dlg = PastLegPickerDialog(
            self.instance.id, self.history, self.portfolio.strategies_raw, parent=self
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        syms = set(dlg.selected_symbols())
        if not syms:
            return
        for h in self.history:
            if h["symbol"] in syms:
                h["strategy_id"] = self.instance.id
        self.portfolio.save_history()
        QMessageBox.information(
            self, "Saved", f"Reassigned {len(syms)} closed leg(s) to this strategy."
        )
        self.accept()


# ── Configure page (main template browser + active strategies) ───────────────

class ConfigurePage(QWidget):
    back_requested      = pyqtSignal()
    strategies_changed  = pyqtSignal()

    def __init__(self, portfolio, parent=None):
        super().__init__(parent)
        self.portfolio = portfolio
        self.setStyleSheet(T.BASE_STYLE)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_header())

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body_w = QWidget()
        body_w.setStyleSheet(f"background: {T.BG};")
        self.body = QVBoxLayout(body_w)
        self.body.setContentsMargins(32, 24, 32, 40)
        self.body.setSpacing(20)
        scroll.setWidget(body_w)
        root.addWidget(scroll)

        self._build_sections()
        self.refresh()

    def _build_header(self):
        header = QFrame()
        header.setFixedHeight(60)
        header.setStyleSheet(
            f"QFrame {{ background: {T.CARD}; border-bottom: 1px solid {T.BORDER}; border-radius: 0; }}"
        )
        hl = QHBoxLayout(header)
        hl.setContentsMargins(28, 0, 28, 0)
        hl.setSpacing(16)

        self.back_btn = QPushButton("←  Back to portfolio")
        self.back_btn.setFixedHeight(32)
        self.back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.back_btn.clicked.connect(self._on_back_clicked)
        self.back_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {T.MUTED}; "
            f"border: 1px solid {T.BORDER}; border-radius: 6px; padding: 0 12px; "
            f"font-size: 12px; }}"
            f"QPushButton:hover {{ color: {T.TEXT}; border-color: {T.ACCENT}; }}"
            f"QPushButton:disabled {{ color: #4b5163; border-color: {T.BORDER}; }}"
        )
        hl.addWidget(self.back_btn)

        title = QLabel("Configure Strategies")
        title.setStyleSheet(
            f"color: {T.ACCENT}; font-size: 17px; font-weight: bold; "
            f"border: none; background: transparent;"
        )
        hl.addWidget(title)
        hl.addStretch()

        self.unassigned_note = QLabel("")
        self.unassigned_note.setStyleSheet(
            f"color: {T.YELLOW}; font-size: 12px; font-weight: bold; "
            f"border: none; background: transparent;"
        )
        hl.addWidget(self.unassigned_note)
        return header

    def _build_sections(self):
        # Activity import banner
        self.import_section = _card_frame(radius=14, pad=(20, 18, 20, 18))[0]
        self._build_import_banner()
        self.body.addWidget(self.import_section)

        # Unassigned legs section (prominent — must be emptied to go back)
        self.unassigned_section = _card_frame(radius=14, pad=(20, 18, 20, 18))[0]
        self.body.addWidget(self.unassigned_section)

        # Active strategies section
        self.active_section = _card_frame(radius=14, pad=(20, 18, 20, 18))[0]
        self.body.addWidget(self.active_section)

        # Catalog section
        cat_frame, cat_lay = _card_frame(radius=14, pad=(20, 18, 20, 18))
        title = QLabel("Strategy Catalog")
        title.setStyleSheet(
            f"color: {T.ACCENT}; font-size: 16px; font-weight: bold; "
            f"border: none; background: transparent;"
        )
        cat_lay.addWidget(title)
        sub = QLabel("Browse templates and create new strategies from your open legs.")
        sub.setStyleSheet(
            f"color: {T.MUTED}; font-size: 12px; border: none; background: transparent;"
        )
        cat_lay.addWidget(sub)
        cat_lay.addSpacing(8)

        search_row = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search templates (name, outlook, type)…")
        self.search_edit.textChanged.connect(self._rebuild_catalog)
        search_row.addWidget(self.search_edit)
        cat_lay.addLayout(search_row)
        cat_lay.addSpacing(6)

        self.catalog_grid_host = QWidget()
        self.catalog_grid = QGridLayout(self.catalog_grid_host)
        self.catalog_grid.setHorizontalSpacing(14)
        self.catalog_grid.setVerticalSpacing(14)
        cat_lay.addWidget(self.catalog_grid_host)

        self.body.addWidget(cat_frame)
        self.body.addStretch()

    # ── Rebuild UI ──────────────────────────────────────────────────────────

    def _build_import_banner(self):
        lay = self.import_section.layout()
        title = QLabel("Activity Import")
        title.setStyleSheet(
            f"color: {T.ACCENT}; font-size: 16px; font-weight: bold; "
            f"border: none; background: transparent;"
        )
        lay.addWidget(title)

        desc = QLabel(
            "Pull past closed positions from your TastyTrade activity history "
            "to backfill performance. After importing, use each strategy's "
            "History button to assign them."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color: {T.MUTED}; font-size: 12px; border: none;")
        lay.addWidget(desc)

        row = QHBoxLayout()
        self.import_btn = _btn("⬇  Import from TastyTrade activity", primary=True)
        self.import_btn.clicked.connect(self._run_import)
        row.addWidget(self.import_btn)
        row.addStretch()
        self.import_status = QLabel("")
        self.import_status.setStyleSheet(
            f"color: {T.MUTED}; font-size: 11px; border: none; background: transparent;"
        )
        row.addWidget(self.import_status)
        lay.addLayout(row)

    def _run_import(self):
        acct = self.portfolio.current_account()
        if not acct:
            return
        self.import_btn.setEnabled(False)
        self.import_btn.setText("Importing…")
        self.import_status.setStyleSheet(
            f"color: {T.MUTED}; font-size: 11px; border: none;"
        )
        self.import_status.setText(
            f"Fetching transactions for {self.portfolio._display_name(acct)}…"
        )
        self._import_worker = ActivityImportWorker(self.portfolio.token, acct["number"])
        self._import_worker.done.connect(self._on_import_done)
        self._import_worker.start()

    def _on_import_done(self, lots, error):
        self.import_btn.setEnabled(True)
        self.import_btn.setText("⬇  Import from TastyTrade activity")
        if error:
            self.import_status.setStyleSheet(f"color: {T.RED}; font-size: 11px; border: none;")
            self.import_status.setText(f"Error: {error}")
            return
        added = merge_history(self.portfolio.history, lots)
        self.portfolio.save_history()
        self.import_status.setStyleSheet(f"color: {T.GREEN}; font-size: 11px; border: none;")
        if added:
            self.import_status.setText(
                f"Imported {added} closed leg(s). Open a strategy's History → "
                f"“Add past closed legs” to assign them."
            )
        else:
            self.import_status.setText(
                f"Found {len(lots)} closed lots — all already in history."
            )
        self.strategies_changed.emit()
        self.refresh()

    def refresh(self):
        self._rebuild_unassigned()
        self._rebuild_active()
        self._rebuild_catalog()
        self._update_back_state()

    def _rebuild_unassigned(self):
        lay = self.unassigned_section.layout()
        self._clear_layout(lay)

        positions = self.portfolio.current_positions()
        assigned = self._all_assigned_symbols()
        leftover = [p for p in positions if p.symbol not in assigned]

        title = QLabel(f"Unassigned Legs ({len(leftover)})")
        title.setStyleSheet(
            f"color: {T.YELLOW if leftover else T.ACCENT}; font-size: 16px; "
            f"font-weight: bold; border: none; background: transparent;"
        )
        lay.addWidget(title)

        if not leftover:
            ok = QLabel("✓  All legs are assigned to a strategy.")
            ok.setStyleSheet(
                f"color: {T.GREEN}; font-size: 12px; border: none; background: transparent;"
            )
            lay.addWidget(ok)
            return

        sub = QLabel("Pick a template below and assign these legs. "
                     "You'll be warned if you try to leave with unassigned legs.")
        sub.setWordWrap(True)
        sub.setStyleSheet(
            f"color: {T.MUTED}; font-size: 11px; border: none; background: transparent;"
        )
        lay.addWidget(sub)

        for p in leftover:
            row = QFrame()
            row.setStyleSheet(
                f"QFrame {{ background: #12151d; border: 1px solid {T.BORDER}; "
                f"border-radius: 6px; }}"
            )
            hl = QHBoxLayout(row)
            hl.setContentsMargins(10, 6, 10, 6)
            label = QLabel(_leg_summary(p))
            label.setStyleSheet(
                f"color: {T.TEXT}; font-size: 12px; border: none; background: transparent;"
            )
            hl.addWidget(label)
            hl.addStretch()
            pl = QLabel(money(p.pnl, signed=True))
            pl.setStyleSheet(
                f"color: {pnl_color(p.pnl)}; font-size: 12px; font-weight: bold; "
                f"border: none; background: transparent;"
            )
            hl.addWidget(pl)
            lay.addWidget(row)

    def _update_back_state(self):
        positions = self.portfolio.current_positions()
        assigned = self._all_assigned_symbols()
        unassigned = [p for p in positions if p.symbol not in assigned]
        if unassigned:
            self.unassigned_note.setText(
                f"⚠  {len(unassigned)} leg(s) still unassigned"
            )
        else:
            self.unassigned_note.setText("")

    def _on_back_clicked(self):
        positions = self.portfolio.current_positions()
        assigned = self._all_assigned_symbols()
        unassigned = [p for p in positions if p.symbol not in assigned]
        if unassigned:
            resp = QMessageBox.question(
                self, "Unassigned legs",
                f"{len(unassigned)} leg(s) are still unassigned.\n"
                "Return to portfolio anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if resp != QMessageBox.StandardButton.Yes:
                return
        self.back_requested.emit()

    def _clear_layout(self, lay):
        while lay.count():
            it = lay.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()
            elif it.layout():
                self._clear_layout(it.layout())

    def _rebuild_active(self):
        lay = self.active_section.layout()
        self._clear_layout(lay)

        title = QLabel("My Strategies")
        title.setStyleSheet(
            f"color: {T.ACCENT}; font-size: 16px; font-weight: bold; "
            f"border: none; background: transparent;"
        )
        lay.addWidget(title)

        instances = self.portfolio.current_instances()
        if not instances:
            empty = QLabel("No strategies yet — scroll down to pick a template.")
            empty.setStyleSheet(
                f"color: {T.MUTED}; font-size: 12px; padding: 18px; border: 1px dashed "
                f"{T.BORDER}; border-radius: 10px; background: transparent;"
            )
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lay.addWidget(empty)
            return

        history = self.portfolio.history
        for inst in instances:
            perf = strategy_performance(inst.id, history)
            row = ActiveStrategyRow(inst, perf)
            row.edit_requested.connect(self._edit)
            row.delete_requested.connect(self._delete)
            row.history_requested.connect(self._show_history)
            lay.addWidget(row)

    def _rebuild_catalog(self):
        self._clear_layout(self.catalog_grid)
        query = self.search_edit.text() if hasattr(self, "search_edit") else ""
        templates = tmpl_mod.search_templates(query)
        cols = 2
        for i, t in enumerate(templates):
            card = TemplateCard(t)
            card.use_clicked.connect(self._start_new)
            self.catalog_grid.addWidget(card, i // cols, i % cols)

    # ── Actions ─────────────────────────────────────────────────────────────

    def _start_new(self, template_key):
        tmpl = tmpl_mod.get_template(template_key)
        if not tmpl:
            return
        positions = self.portfolio.current_positions()
        taken = {s for s in self._all_assigned_symbols()}
        available = [p for p in positions if p.symbol not in taken]

        dlg = StrategyBuilderDialog(tmpl, available, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        data = dlg.result_data()
        new_entry = {
            "id":         uuid.uuid4().hex[:10],
            "template":   tmpl.key,
            "name":       data["name"],
            "legs":       data["symbols"],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "notes":      "",
        }
        self.portfolio.strategies_raw.append(new_entry)
        self.portfolio.save_strategies()
        self.strategies_changed.emit()
        self.refresh()

    def _edit(self, strategy_id):
        raw = next((s for s in self.portfolio.strategies_raw if s["id"] == strategy_id), None)
        if not raw:
            return
        tmpl = tmpl_mod.get_template(raw["template"])
        positions = self.portfolio.current_positions()
        taken = {s for s in self._all_assigned_symbols() if s not in raw["legs"]}
        available = [p for p in positions if p.symbol not in taken]

        existing = next((i for i in self.portfolio.current_instances() if i.id == strategy_id), None)
        dlg = StrategyBuilderDialog(tmpl, available, existing=existing, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        data = dlg.result_data()
        raw["legs"] = data["symbols"]
        raw["name"] = data["name"]
        self.portfolio.save_strategies()
        self.strategies_changed.emit()
        self.refresh()

    def _delete(self, strategy_id):
        resp = QMessageBox.question(
            self, "Delete strategy",
            "Delete this strategy? History will be preserved.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if resp != QMessageBox.StandardButton.Yes:
            return
        self.portfolio.strategies_raw = [
            s for s in self.portfolio.strategies_raw if s["id"] != strategy_id
        ]
        self.portfolio.save_strategies()
        self.strategies_changed.emit()
        self.refresh()

    def _show_history(self, strategy_id):
        inst = next((i for i in self.portfolio.current_instances() if i.id == strategy_id), None)
        if not inst:
            return
        dlg = HistoryDialog(inst, self.portfolio, parent=self)
        dlg.exec()
        # history may have been mutated in-place and saved
        self.refresh()

    def _all_assigned_symbols(self):
        out = set()
        for s in self.portfolio.strategies_raw:
            for sym in s.get("legs", []):
                out.add(sym)
        return out
