"""Settings/CLI parity, model isolation, safe Off/Auto behavior, and cancel semantics."""
import copy
import json
import pytest
from pengy.core.config import DEFAULTS, compaction_policy, set_compaction_policy
from pengy.core import context_windows


def test_policy_off_auto_explicit_preserves_windows_and_reactive_recovery():
    config=copy.deepcopy(DEFAULTS);config['context_windows']={'fast':1048576}
    set_compaction_policy(config,'local','explicit',262144,80)
    assert compaction_policy(config,'local')==('explicit',262144)
    set_compaction_policy(config,'local','off')
    assert config['context_windows']=={'fast':1048576,'local':262144}
    assert config['auto_context_recovery'] and not config['context_window_auto']
    assert context_windows.resolve('http://host/v1','local','',windows=config['context_windows'],modes=config['context_window_modes']) is None
    set_compaction_policy(config,'local','auto')
    assert compaction_policy(config,'local')[0]=='auto'
    assert compaction_policy(config,'fast')==('explicit',1048576)
    original=copy.deepcopy(config)
    for args in [('', 'auto',0,None),('local','explicit',0,None),('local','bad',0,None),('local','off',0,101)]:
        with pytest.raises(ValueError):set_compaction_policy(config,*args)
    assert config==original


def test_auto_mode_overrides_global_without_changing_it(tmp_path,monkeypatch):
    monkeypatch.setattr('pengy.core.config._config_dir_override',str(tmp_path))
    context_windows.remember('http://host/v1','local',{'tokens':262144,'input_only':False},'models')
    assert context_windows.resolve('http://host/v1','local','',global_window=1048576,windows={'local':0},modes={'local':'auto'})['tokens']==262144
    assert context_windows.resolve('http://host/v1','fast','',global_window=1048576,modes={'local':'auto'})['tokens']==1048576


def test_qt_controls_switch_models_without_leaking_or_cancel_mutation(tmp_path,monkeypatch):
    pytest.importorskip('PySide6')
    from PySide6.QtWidgets import QApplication
    from pengy.ui.settings_dialog import SettingsDialog
    monkeypatch.setattr('pengy.core.config._config_dir_override',str(tmp_path))
    app=QApplication.instance() or QApplication([])
    config={**copy.deepcopy(DEFAULTS),'model':'local','context_windows':{'local':262144,'fast':1048576}}
    original=copy.deepcopy(config)
    dialog=SettingsDialog(config)
    assert dialog.compaction_mode.currentData()=='explicit'
    dialog.compaction_mode.setCurrentIndex(dialog.compaction_mode.findData('off'))
    dialog.model_combo.setCurrentText('fast')
    assert dialog.compaction_window.value()==1048576 and dialog.compaction_mode.currentData()=='explicit'
    dialog.reject();assert config==original
    dialog=SettingsDialog(config)
    dialog.compaction_mode.setCurrentIndex(dialog.compaction_mode.findData('auto'))
    dialog.compaction_percent.setValue(80)
    updated=dialog.get_config()
    assert updated['context_window_modes']['local']=='auto'
    assert updated['context_windows']==original['context_windows']
    assert updated['compact_at_percent']==80 and updated['auto_context_recovery']


def test_cli_command_persists_selected_model_without_provider_request(tmp_path,monkeypatch):
    from pengy.cli.main import PengyCLI,SLASH_COMMANDS
    from rich.console import Console
    from io import StringIO
    cli=PengyCLI.__new__(PengyCLI);cli.config={**copy.deepcopy(DEFAULTS),'model':'local','context_windows':{'fast':1048576}};cli.current_chat=None
    stream=StringIO();cli.console=Console(file=stream);cli._save_config=lambda:None
    cli._cmd_auto_compact(['262144','80']);cli._cmd_auto_compact(['off']);cli._cmd_auto_compact(['auto'])
    assert cli.config['context_window_modes']['local']=='auto' and cli.config['context_windows']['local']==262144
    assert cli.config['context_windows']['fast']==1048576 and cli.config['compact_at_percent']==80
    before=copy.deepcopy(cli.config);cli._cmd_auto_compact(['0']);assert cli.config==before
    assert '/auto-compact' in SLASH_COMMANDS


def test_web_form_persists_and_validates_without_partial_save(tmp_path,monkeypatch):
    from pengy.core.config import save_config,load_config
    from pengy.web.app import app
    monkeypatch.setattr('pengy.core.config._config_dir_override',str(tmp_path))
    save_config({**copy.deepcopy(DEFAULTS),'model':'local','context_windows':{'fast':1048576},'future_setting':True})
    app.config['SERVER_NAME']='localhost'
    with app.test_client() as client:
        page=client.get('/settings');assert b'compaction_mode' in page.data and b'Auto-discover' in page.data
        data={'model':'local','compaction_model':'local','compaction_mode':'explicit','compaction_window':'262144','compact_at_percent':'80'}
        response=client.post('/settings',data=data);assert response.status_code==200
        config=load_config();assert config['context_window_modes']['local']=='explicit' and config['context_windows']['fast']==1048576
        assert config['compact_at_percent']==80 and config['future_setting']
        before=copy.deepcopy(config)
        response=client.post('/settings',data={**data,'compact_at_percent':'101'})
        assert response.status_code==400 and load_config()==before
        response=client.post('/settings',data={**data,'compaction_model':'fast'})
        assert response.status_code==400 and load_config()==before
