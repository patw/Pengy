"""Settings dialog for Pengy — tabbed UI / LLM / Tools."""
import json
import threading
import urllib.request

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QLineEdit,
    QTextEdit, QDialogButtonBox, QCheckBox, QLabel, QComboBox,
    QSpinBox, QPushButton, QHBoxLayout, QMessageBox, QTabWidget, QWidget,
)
from PySide6.QtCore import Qt, Signal

from pengy.ui.theme import ACCENT_NAMES, THEME_MODES


def _label(text: str, tooltip: str) -> QLabel:
    """Create a QLabel with a hover tooltip."""
    lbl = QLabel(text)
    lbl.setToolTip(tooltip)
    return lbl


class SettingsDialog(QDialog):
    """Settings dialog with three tabs: UI, LLM, Tools."""

    _models_fetched = Signal(list)
    _models_fetch_failed = Signal(str)

    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._models_fetched.connect(self._on_models_fetched)
        self._models_fetch_failed.connect(self._on_models_fetch_failed)
        self.config = config
        self.setWindowTitle("Settings")
        self.setModal(True)

        layout = QVBoxLayout(self)

        # ── tabs ──────────────────────────────────────────────────
        tabs = QTabWidget()

        # ── UI tab ────────────────────────────────────────────────
        ui_tab = QWidget()
        ui_layout = QFormLayout(ui_tab)
        ui_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self.scale_combo = QComboBox()
        self._scale_values = [75, 100, 125, 150, 175, 200]
        for v in self._scale_values:
            self.scale_combo.addItem(f"{v}%", v)
        current_scale = config.get("ui_scale", 100)
        idx = self._scale_values.index(current_scale) if current_scale in self._scale_values else 1
        self.scale_combo.setCurrentIndex(idx)
        self.scale_combo.setToolTip("Scales the entire UI. A restart is needed for the change to take full effect.")
        ui_layout.addRow(_label("UI Scale:", "Scales the entire UI. A restart is needed for the change to take full effect."), self.scale_combo)

        self.theme_mode_combo = QComboBox()
        theme_mode_labels = {"system": "System", "light": "Light", "dark": "Dark"}
        for mode in THEME_MODES:
            self.theme_mode_combo.addItem(theme_mode_labels.get(mode, mode.title()), mode)
        current_theme_mode = config.get("theme_mode", "system")
        for i in range(self.theme_mode_combo.count()):
            if self.theme_mode_combo.itemData(i) == current_theme_mode:
                self.theme_mode_combo.setCurrentIndex(i)
                break
        self.theme_mode_combo.setToolTip("System follows your OS theme; Light and Dark override it.")
        ui_layout.addRow(_label("Theme mode:", "System follows your OS theme; Light and Dark override it."), self.theme_mode_combo)

        self.theme_accent_combo = QComboBox()
        for accent in ACCENT_NAMES:
            self.theme_accent_combo.addItem(accent.title(), accent)
        current_accent = config.get("theme_accent", "default")
        for i in range(self.theme_accent_combo.count()):
            if self.theme_accent_combo.itemData(i) == current_accent:
                self.theme_accent_combo.setCurrentIndex(i)
                break
        self.theme_accent_combo.setToolTip("Highlight color for buttons, links, and selection highlights.")
        ui_layout.addRow(_label("Accent color:", "Highlight color for buttons, links, and selection highlights."), self.theme_accent_combo)

        tabs.addTab(ui_tab, "UI")

        # ── LLM tab ───────────────────────────────────────────────
        llm_tab = QWidget()
        llm_layout = QFormLayout(llm_tab)
        llm_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self.base_url_input = QLineEdit(config.get("base_url", "https://api.openai.com/v1"))
        self.base_url_input.setToolTip("OpenAI-compatible API endpoint, e.g. https://api.openai.com/v1 or a local llama.cpp server.")
        llm_layout.addRow(_label("Base URL:", "OpenAI-compatible API endpoint, e.g. https://api.openai.com/v1 or a local llama.cpp server."), self.base_url_input)

        self.api_key_input = QLineEdit(config.get("api_key", ""))
        self.api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_input.setToolTip("Bearer token sent in the Authorization header to the LLM provider.")
        llm_layout.addRow(_label("API Key:", "Bearer token sent in the Authorization header to the LLM provider."), self.api_key_input)

        model_row = QHBoxLayout()
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        current_model = config.get("model", "gpt-4o")
        self.model_combo.addItem(current_model)
        self.model_combo.setCurrentText(current_model)
        self.model_combo.setToolTip("Model name sent in chat completion requests. Use Fetch to list available models from the endpoint.")
        model_row.addWidget(self.model_combo, 1)

        self.fetch_models_btn = QPushButton("↻ Fetch")
        self.fetch_models_btn.setToolTip("Fetch available models from the /models endpoint")
        self.fetch_models_btn.setFixedWidth(80)
        self.fetch_models_btn.clicked.connect(self._fetch_models)
        model_row.addWidget(self.fetch_models_btn)

        llm_layout.addRow(_label("Model:", "Model name sent in chat completion requests. Use Fetch to list available models from the endpoint."), model_row)

        self.system_message_input = QTextEdit(config.get("system_message", "You are a helpful assistant."))
        self.system_message_input.setMaximumHeight(100)
        self.system_message_input.setToolTip("The system prompt that sets the assistant's behavior, tone, and constraints.")
        llm_layout.addRow(_label("System Message:", "The system prompt that sets the assistant's behavior, tone, and constraints."), self.system_message_input)

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
        for lbl, value in reasoning_options:
            self.reasoning_effort_combo.addItem(lbl, value)
        current_effort = config.get("reasoning_effort", "")
        for i in range(self.reasoning_effort_combo.count()):
            if self.reasoning_effort_combo.itemData(i) == current_effort:
                self.reasoning_effort_combo.setCurrentIndex(i)
                break
        self.reasoning_effort_combo.setToolTip("Optional best-effort reasoning depth hint. Only supported by some models/providers; others may reject unknown values.")
        llm_layout.addRow(_label("Reasoning effort:", "Optional best-effort reasoning depth hint. Only supported by some models/providers."), self.reasoning_effort_combo)

        self.preserve_reasoning_checkbox = QCheckBox("Keep reasoning fields in conversation history")
        self.preserve_reasoning_checkbox.setChecked(bool(config.get("preserve_reasoning", False)))
        self.preserve_reasoning_checkbox.setToolTip("When checked, reasoning_content / reasoning / reasoning_details fields returned by the provider are kept. Leave off if your proxy rejects unknown message fields.")
        llm_layout.addRow(_label("Preserve reasoning:", "When checked, reasoning_content / reasoning / reasoning_details fields returned by the provider are kept."), self.preserve_reasoning_checkbox)

        self.llm_timeout_spinbox = QSpinBox()
        self.llm_timeout_spinbox.setRange(1, 3600)
        self.llm_timeout_spinbox.setSuffix(" sec")
        self.llm_timeout_spinbox.setToolTip("HTTP timeout in seconds for each LLM API request. Increase if your model is slow to respond.")
        self.llm_timeout_spinbox.setValue(config.get("llm_timeout", 300))
        llm_layout.addRow(_label("LLM timeout:", "HTTP timeout in seconds for each LLM API request. Increase if your model is slow to respond."), self.llm_timeout_spinbox)

        tabs.addTab(llm_tab, "LLM")

        # ── Tools tab ─────────────────────────────────────────────
        tools_tab = QWidget()
        tools_layout = QFormLayout(tools_tab)
        tools_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self.confirm_combo = QComboBox()
        self.confirm_combo.addItem("YOLO (All) — execute everything, no questions asked", "all")
        self.confirm_combo.addItem("Safe Only — auto-approve read-only tools; confirm write/execute", "safe")
        self.confirm_combo.addItem("None — confirm every tool before execution", "none")
        current_confirm = config.get("tool_confirmation", "none")
        for i in range(self.confirm_combo.count()):
            if self.confirm_combo.itemData(i) == current_confirm:
                self.confirm_combo.setCurrentIndex(i)
                break
        self.confirm_combo.setToolTip("YOLO runs everything without asking. Safe only confirms write/execute tools. None confirms every tool call.")
        tools_layout.addRow(_label("Tool Confirmation:", "YOLO runs everything without asking. Safe auto-approves read-only tools. None confirms every tool call."), self.confirm_combo)

        self.context_keep_turns_spinbox = QSpinBox()
        self.context_keep_turns_spinbox.setRange(0, 999)
        self.context_keep_turns_spinbox.setSpecialValueText("Keep all")
        self.context_keep_turns_spinbox.setSuffix(" turns")
        self.context_keep_turns_spinbox.setToolTip("Tool results older than N turns are elided to save context window. 0 = keep everything.")
        self.context_keep_turns_spinbox.setValue(config.get("context_keep_turns", 0))
        tools_layout.addRow(_label("Keep tool results:", "Tool results older than N turns are elided to save context window. 0 = keep everything."), self.context_keep_turns_spinbox)

        self.timeout_spinbox = QSpinBox()
        self.timeout_spinbox.setRange(-1, 3600)
        self.timeout_spinbox.setSpecialValueText("No timeout")
        self.timeout_spinbox.setSuffix(" sec")
        self.timeout_spinbox.setToolTip("Maximum wall-clock time a single tool invocation can run before being killed. -1 = no timeout.")
        self.timeout_spinbox.setValue(config.get("tool_timeout", 60))
        tools_layout.addRow(_label("Tool timeout:", "Maximum wall-clock time a single tool invocation can run before being killed. -1 = no timeout."), self.timeout_spinbox)

        self.tool_output_max_spinbox = QSpinBox()
        self.tool_output_max_spinbox.setRange(0, 500_000)
        self.tool_output_max_spinbox.setSpecialValueText("No limit")
        self.tool_output_max_spinbox.setSuffix(" chars")
        self.tool_output_max_spinbox.setToolTip("Tool output longer than this is snipped (head+tail) to avoid blowing up the context window. 0 = no limit.")
        self.tool_output_max_spinbox.setValue(config.get("tool_output_max_chars", 250000))
        tools_layout.addRow(_label("Max tool output:", "Tool output longer than this is snipped (head+tail) to avoid blowing up the context window. 0 = no limit."), self.tool_output_max_spinbox)

        self.user_agent_input = QLineEdit(config.get("user_agent", "PengyAgent/1.0"))
        self.user_agent_input.setToolTip("HTTP User-Agent header sent with LLM API requests and any HTTP-based tool calls.")
        tools_layout.addRow(_label("User Agent:", "HTTP User-Agent header sent with LLM API requests and any HTTP-based tool calls."), self.user_agent_input)

        tabs.addTab(tools_tab, "Tools")

        layout.addWidget(tabs)
        layout.addStretch()

        # ── buttons ──────────────────────────────────────────────
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.adjustSize()

    # ── model fetch (unchanged logic) ─────────────────────────
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
        self.config["llm_timeout"] = self.llm_timeout_spinbox.value()
        self.config["tool_timeout"] = self.timeout_spinbox.value()
        self.config["tool_output_max_chars"] = self.tool_output_max_spinbox.value()
        self.config["user_agent"] = self.user_agent_input.text() or "PengyAgent/1.0"
        return self.config
