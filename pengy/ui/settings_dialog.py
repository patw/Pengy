"""Settings dialog for Pengy."""
import json
import threading
import urllib.request

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QLineEdit,
    QTextEdit, QDialogButtonBox, QCheckBox, QLabel, QComboBox,
    QSpinBox, QPushButton, QHBoxLayout, QMessageBox,
)
from PySide6.QtCore import Qt, Signal

from pengy.ui.theme import ACCENT_NAMES, THEME_MODES


class SettingsDialog(QDialog):
    """Settings dialog for configuring API and system message."""

    _models_fetched = Signal(list)
    _models_fetch_failed = Signal(str)

    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        self._models_fetched.connect(self._on_models_fetched)
        self._models_fetch_failed.connect(self._on_models_fetch_failed)
        self.config = config
        self.setWindowTitle("Settings")
        self.setModal(True)
        self.resize(520, 500)

        layout = QVBoxLayout(self)

        # API Settings
        api_group = QFormLayout()
        api_group.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self.base_url_input = QLineEdit(config.get("base_url", "https://api.openai.com/v1"))
        self.api_key_input = QLineEdit(config.get("api_key", ""))
        self.api_key_input.setEchoMode(QLineEdit.EchoMode.Password)

        # Model: editable combo box + fetch button
        model_row = QHBoxLayout()
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        current_model = config.get("model", "gpt-4o")
        self.model_combo.addItem(current_model)
        self.model_combo.setCurrentText(current_model)
        model_row.addWidget(self.model_combo, 1)

        self.fetch_models_btn = QPushButton("↻ Fetch")
        self.fetch_models_btn.setToolTip("Fetch available models from the endpoint")
        self.fetch_models_btn.setFixedWidth(80)
        self.fetch_models_btn.clicked.connect(self._fetch_models)
        model_row.addWidget(self.fetch_models_btn)

        self.user_agent_input = QLineEdit(config.get("user_agent", "PengyAgent/1.0"))

        api_group.addRow("Base URL:", self.base_url_input)
        api_group.addRow("API Key:", self.api_key_input)
        api_group.addRow("Model:", model_row)
        api_group.addRow("User Agent:", self.user_agent_input)

        layout.addLayout(api_group)

        # System Message
        sys_label = QLabel("System Message:")
        layout.addWidget(sys_label)

        self.system_message_input = QTextEdit(config.get("system_message", "You are a helpful assistant."))
        self.system_message_input.setMaximumHeight(100)
        layout.addWidget(self.system_message_input)

        # Tool Confirmation
        confirm_layout = QFormLayout()
        confirm_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.confirm_combo = QComboBox()
        self.confirm_combo.addItem("YOLO (All) — execute everything, no questions asked", "all")
        self.confirm_combo.addItem("Safe Only — auto-approve read-only tools; confirm write/execute", "safe")
        self.confirm_combo.addItem("None — confirm every tool before execution", "none")
        current_confirm = config.get("tool_confirmation", "none")
        for i in range(self.confirm_combo.count()):
            if self.confirm_combo.itemData(i) == current_confirm:
                self.confirm_combo.setCurrentIndex(i)
                break
        confirm_layout.addRow("Tool Confirmation:", self.confirm_combo)

        # Reasoning options (safe defaults: provider default / off)
        self.reasoning_effort_combo = QComboBox()
        reasoning_options = [
            ("Provider default — do not send reasoning option", ""),
            ("Off / none", "none"),
            ("Minimal", "minimal"),
            ("Low", "low"),
            ("Medium", "medium"),
            ("High", "high"),
            ("Extra high", "xhigh"),
            ("Max", "max"),
        ]
        for label, value in reasoning_options:
            self.reasoning_effort_combo.addItem(label, value)
        current_effort = config.get("reasoning_effort", "")
        for i in range(self.reasoning_effort_combo.count()):
            if self.reasoning_effort_combo.itemData(i) == current_effort:
                self.reasoning_effort_combo.setCurrentIndex(i)
                break
        self.reasoning_effort_combo.setToolTip(
            "Optional best-effort reasoning depth. Provider default omits the parameter. "
            "Some proxies/providers reject unsupported values."
        )
        confirm_layout.addRow("Reasoning effort:", self.reasoning_effort_combo)

        self.preserve_reasoning_checkbox = QCheckBox(
            "Preserve returned reasoning fields in conversation history"
        )
        self.preserve_reasoning_checkbox.setChecked(bool(config.get("preserve_reasoning", False)))
        self.preserve_reasoning_checkbox.setToolTip(
            "Best effort: keeps fields like reasoning_content/reasoning/reasoning_details "
            "when providers return them. Leave off if your proxy rejects unknown message fields."
        )
        confirm_layout.addRow("Reasoning preservation:", self.preserve_reasoning_checkbox)
        layout.addLayout(confirm_layout)

        # Context keep turns
        ctx_row = QFormLayout()
        ctx_row.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.context_keep_turns_spinbox = QSpinBox()
        self.context_keep_turns_spinbox.setRange(0, 999)
        self.context_keep_turns_spinbox.setSpecialValueText("Keep all")
        self.context_keep_turns_spinbox.setSuffix(" turns")
        self.context_keep_turns_spinbox.setToolTip(
            "Tool results older than this many turns are elided to save context window. "
            "0 = keep all."
        )
        self.context_keep_turns_spinbox.setValue(config.get("context_keep_turns", 0))
        ctx_row.addRow("Keep tool results:", self.context_keep_turns_spinbox)
        layout.addLayout(ctx_row)

        # Appearance
        appearance_row = QFormLayout()
        appearance_row.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self.theme_mode_combo = QComboBox()
        theme_mode_labels = {"system": "System", "light": "Light", "dark": "Dark"}
        for mode in THEME_MODES:
            self.theme_mode_combo.addItem(theme_mode_labels.get(mode, mode.title()), mode)
        current_theme_mode = config.get("theme_mode", "system")
        for i in range(self.theme_mode_combo.count()):
            if self.theme_mode_combo.itemData(i) == current_theme_mode:
                self.theme_mode_combo.setCurrentIndex(i)
                break
        appearance_row.addRow("Theme mode:", self.theme_mode_combo)

        self.theme_accent_combo = QComboBox()
        for accent in ACCENT_NAMES:
            self.theme_accent_combo.addItem(accent.title(), accent)
        current_accent = config.get("theme_accent", "default")
        for i in range(self.theme_accent_combo.count()):
            if self.theme_accent_combo.itemData(i) == current_accent:
                self.theme_accent_combo.setCurrentIndex(i)
                break
        appearance_row.addRow("Accent color:", self.theme_accent_combo)
        layout.addLayout(appearance_row)

        # UI Scale
        scale_row = QFormLayout()
        scale_row.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.scale_combo = QComboBox()
        self._scale_values = [75, 100, 125, 150, 175, 200]
        for v in self._scale_values:
            self.scale_combo.addItem(f"{v}%", v)
        current_scale = config.get("ui_scale", 100)
        idx = self._scale_values.index(current_scale) if current_scale in self._scale_values else 1
        self.scale_combo.setCurrentIndex(idx)
        scale_row.addRow("UI Scale (restart to apply):", self.scale_combo)
        layout.addLayout(scale_row)

        # Tool Timeout
        timeout_row = QFormLayout()
        timeout_row.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.timeout_spinbox = QSpinBox()
        self.timeout_spinbox.setRange(-1, 3600)
        self.timeout_spinbox.setSpecialValueText("No timeout")
        self.timeout_spinbox.setSuffix(" sec")
        self.timeout_spinbox.setValue(config.get("tool_timeout", 60))
        timeout_row.addRow("Tool timeout:", self.timeout_spinbox)
        layout.addLayout(timeout_row)

        layout.addStretch()

        # Buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _fetch_models(self):
        """Fetch available models from the endpoint's /v1/models (non-blocking)."""
        base_url = self.base_url_input.text().strip().rstrip("/")
        api_key = self.api_key_input.text()
        models_url = f"{base_url}/models"

        self.fetch_models_btn.setEnabled(False)
        self.fetch_models_btn.setText("...")

        def do_fetch():
            try:
                req = urllib.request.Request(models_url)
                req.add_header("Authorization", f"Bearer {api_key}")
                req.add_header("api-key", api_key)
                req.add_header("User-Agent", "PengyAgent/1.0")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode())
                model_ids = sorted(
                    m.get("id", "") for m in data.get("data", [])
                    if m.get("id")
                )
                self._models_fetched.emit(model_ids)
            except urllib.error.HTTPError as e:
                self._models_fetch_failed.emit(
                    f"HTTP {e.code} from {models_url}\n\nCheck your Base URL and API Key."
                )
            except Exception as e:
                self._models_fetch_failed.emit(str(e))

        threading.Thread(target=do_fetch, daemon=True).start()

    def _on_models_fetched(self, model_ids: list):
        self.fetch_models_btn.setEnabled(True)
        self.fetch_models_btn.setText("↻ Fetch")
        if not model_ids:
            QMessageBox.information(self, "No Models", "The endpoint returned an empty model list.")
            return
        current = self.model_combo.currentText()
        self.model_combo.clear()
        self.model_combo.addItems(model_ids)
        if current in model_ids:
            self.model_combo.setCurrentText(current)
        elif model_ids:
            self.model_combo.setCurrentText(model_ids[0])

    def _on_models_fetch_failed(self, error: str):
        self.fetch_models_btn.setEnabled(True)
        self.fetch_models_btn.setText("↻ Fetch")
        QMessageBox.warning(self, "Fetch Failed", error)

    def get_config(self) -> dict:
        """Return updated configuration."""
        self.config["base_url"] = self.base_url_input.text()
        self.config["api_key"] = self.api_key_input.text()
        self.config["model"] = self.model_combo.currentText()
        self.config["system_message"] = self.system_message_input.toPlainText()
        self.config["tool_confirmation"] = self.confirm_combo.currentData()
        self.config["reasoning_effort"] = self.reasoning_effort_combo.currentData()
        self.config["preserve_reasoning"] = self.preserve_reasoning_checkbox.isChecked()
        self.config["context_keep_turns"] = self.context_keep_turns_spinbox.value()
        self.config["theme_mode"] = self.theme_mode_combo.currentData()
        self.config["theme_accent"] = self.theme_accent_combo.currentData()
        self.config["ui_scale"] = self.scale_combo.currentData()
        self.config["tool_timeout"] = self.timeout_spinbox.value()
        self.config["user_agent"] = self.user_agent_input.text() or "PengyAgent/1.0"
        return self.config
