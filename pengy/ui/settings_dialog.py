"""Settings dialog for Pengy."""
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QLineEdit,
    QTextEdit, QDialogButtonBox, QCheckBox, QLabel, QComboBox,
    QSpinBox,
)


class SettingsDialog(QDialog):
    """Settings dialog for configuring API and system message."""

    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("Settings")
        self.setModal(True)
        self.resize(500, 400)

        layout = QVBoxLayout(self)

        # API Settings
        api_group = QFormLayout()
        api_group.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self.base_url_input = QLineEdit(config.get("base_url", "https://api.openai.com/v1"))
        self.api_key_input = QLineEdit(config.get("api_key", ""))
        self.api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.model_input = QLineEdit(config.get("model", "gpt-4o"))
        self.user_agent_input = QLineEdit(config.get("user_agent", "PengyAgent/1.0"))

        api_group.addRow("Base URL:", self.base_url_input)
        api_group.addRow("API Key:", self.api_key_input)
        api_group.addRow("Model:", self.model_input)
        api_group.addRow("User Agent:", self.user_agent_input)

        layout.addLayout(api_group)

        # System Message
        sys_label = QLabel("System Message:")
        layout.addWidget(sys_label)

        self.system_message_input = QTextEdit(config.get("system_message", "You are a helpful assistant."))
        self.system_message_input.setMaximumHeight(100)
        layout.addWidget(self.system_message_input)

        # YOLO Mode
        self.yolo_checkbox = QCheckBox("YOLO Mode (execute tools without confirmation)")
        self.yolo_checkbox.setChecked(config.get("yolo_mode", False))
        layout.addWidget(self.yolo_checkbox)

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

    def get_config(self) -> dict:
        """Return updated configuration."""
        self.config["base_url"] = self.base_url_input.text()
        self.config["api_key"] = self.api_key_input.text()
        self.config["model"] = self.model_input.text()
        self.config["system_message"] = self.system_message_input.toPlainText()
        self.config["yolo_mode"] = self.yolo_checkbox.isChecked()
        self.config["ui_scale"] = self.scale_combo.currentData()
        self.config["tool_timeout"] = self.timeout_spinbox.value()
        self.config["user_agent"] = self.user_agent_input.text() or "PengyAgent/1.0"
        return self.config
